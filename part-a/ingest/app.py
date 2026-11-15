"""Lambda de ingesta de telemetría (IA-2). Python 3.12.

Disparada por SQS. Cada mensaje trae una lectura o un lote ({"readings": [...]}).
Por cada lectura:
  1. Valida contra el contrato (secc. 2 de /contract/README.md).
  2. Escribe en DynamoDB (tabla Telemetry) con una condición que la hace
     idempotente por clave natural (machine_id + timestamp).
  3. Guarda las lecturas nuevas en S3 como Parquet particionado por
     telemetry/year=YYYY/month=MM/day=DD/zone=ZONA-X/.
  4. Actualiza el estado actual de la máquina en la tabla Machines.

Errores:
  - Lectura inválida: se rechaza con el formato de error de la secc. 7, se
    registra en el log y NO detiene el lote ni se reintenta.
  - Falla transitoria (DynamoDB, S3, etc.): el mensaje se devuelve en
    batchItemFailures para que SQS lo reintente y, agotados los reintentos, lo
    mande a la DLQ. Requiere FunctionResponseTypes: [ReportBatchItemFailures].

Idempotencia (reenviar nunca duplica):
  - Telemetry: PutItem con condición "la clave no existe O la escribió este mismo
    mensaje SQS". Un reenvío de otro mensaje se detecta y se omite; el reintento
    del mismo mensaje se vuelve a procesar completo, así que un fallo a medias
    nunca deja una lectura sin su copia en S3.
  - S3: la clave del objeto es determinista (id del mensaje + partición) y se
    escribe con If-None-Match: "*"; si ya existe, no se reescribe.

Variables de entorno (los nombres salen de SSM /arquifusion/{env}/data/):
  TELEMETRY_TABLE   tabla Telemetry
  MACHINES_TABLE    tabla Machines
  TELEMETRY_BUCKET  bucket S3 de datos
  TTL_DAYS          opcional, por defecto 30
  LOG_LEVEL         opcional, por defecto INFO
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Contrato (secc. 1 y 2)
# ---------------------------------------------------------------------------
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SCHEMA_VERSION_RE = re.compile(r"^1\.[0-9]+$")
MACHINE_ID_RE = re.compile(r"^MCH-[0-9]{1,9}$")
ZONE_ID_RE = re.compile(r"^ZONA-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
STATUS_VALUES = frozenset({"operating", "idle", "stopped", "maintenance", "fault"})
METRIC_FIELDS = (
    "engine_temp_c",
    "vibration_mm_s",
    "pressure_bar",
    "fuel_rate_lph",
    "load_kg",
    "speed_kmh",
    "engine_rpm",
    "operating_hours_total",
)
# Comprobaciones de cordura (no están en el contrato): estas magnitudes no son negativas.
NON_NEGATIVE_METRICS = frozenset(METRIC_FIELDS) - {"engine_temp_c"}
MAX_READINGS_PER_MESSAGE = 500


class ValidationError(Exception):
    """Lectura inválida. Se convierte al formato de error de la secc. 7."""

    def __init__(self, message: str, field_name: Optional[str] = None,
                 machine_id: Optional[str] = None):
        super().__init__(message)
        self.field_name = field_name
        self.machine_id = machine_id


def _optional_number(value: Any, field_name: str, fail: Callable[[str, str], None],
                     minimum: Optional[float] = None,
                     maximum: Optional[float] = None) -> Optional[Any]:
    """Número finito o null. Los faltantes se normalizan a None (secc. 1)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        fail(f"El campo {field_name} debe ser numérico", field_name)
    if minimum is not None and maximum is not None and not minimum <= value <= maximum:
        fail(f"El campo {field_name} debe estar entre {minimum} y {maximum}", field_name)
    if minimum is not None and maximum is None and value < minimum:
        fail(f"El campo {field_name} debe ser mayor o igual a {minimum}", field_name)
    return value


def validate_reading(raw: Any) -> Dict[str, Any]:
    """Valida una lectura contra la secc. 2 y la devuelve normalizada.

    Los campos desconocidos se ignoran (compatibilidad hacia adelante) y las
    métricas ausentes se guardan como None.
    """
    if not isinstance(raw, dict):
        raise ValidationError("La lectura debe ser un objeto JSON")

    machine_id = raw.get("machine_id")
    known_id = machine_id if isinstance(machine_id, str) else None

    def fail(message: str, field_name: str) -> None:
        raise ValidationError(message, field_name, known_id)

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not SCHEMA_VERSION_RE.match(schema_version):
        fail('El campo schema_version debe ser una versión 1.x (ej. "1.0")', "schema_version")

    if not isinstance(machine_id, str) or not MACHINE_ID_RE.match(machine_id):
        fail("El campo machine_id debe tener el formato MCH-000123", "machine_id")

    timestamp = raw.get("timestamp")
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.match(timestamp):
        fail("El campo timestamp debe ser ISO 8601 en UTC con Z (ej. 2026-11-14T15:04:05Z)",
             "timestamp")
    try:
        datetime.strptime(timestamp, ISO_FORMAT)
    except ValueError:
        fail("El campo timestamp no es una fecha válida", "timestamp")

    location = raw.get("location")
    if not isinstance(location, dict):
        fail("El campo location debe ser un objeto", "location")
    lat = _optional_number(location.get("lat"), "location.lat", fail, -90, 90)
    lon = _optional_number(location.get("lon"), "location.lon", fail, -180, 180)
    zone_id = location.get("zone_id")
    if not isinstance(zone_id, str) or not ZONE_ID_RE.match(zone_id):
        fail("El campo location.zone_id debe tener el formato ZONA-NORTE-02", "location.zone_id")

    status = raw.get("status")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        fail("El campo status debe ser uno de: " + ", ".join(sorted(STATUS_VALUES)), "status")

    metrics_raw = raw.get("metrics")
    if not isinstance(metrics_raw, dict):
        fail("El campo metrics debe ser un objeto", "metrics")
    metrics = {}
    for name in METRIC_FIELDS:
        minimum = 0 if name in NON_NEGATIVE_METRICS else None
        metrics[name] = _optional_number(metrics_raw.get(name), f"metrics.{name}", fail, minimum)

    return {
        "schema_version": schema_version,
        "machine_id": machine_id,
        "timestamp": timestamp,
        "location": {"lat": lat, "lon": lon, "zone_id": zone_id},
        "status": status,
        "metrics": metrics,
    }


def build_error(err: ValidationError, request_id: str = "") -> Dict[str, Any]:
    """Error uniforme de la secc. 7."""
    details: Dict[str, Any] = {}
    if err.machine_id:
        details["machine_id"] = err.machine_id
    if err.field_name:
        details["field"] = err.field_name
    return {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": str(err),
            "details": details,
            "request_id": request_id,
        }
    }


def extract_readings(body: Any) -> List[Any]:
    """Devuelve las lecturas de un mensaje: una sola o el lote en "readings"."""
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("El mensaje está vacío", "body")
    try:
        payload = json.loads(body)
    except ValueError:
        raise ValidationError("El cuerpo del mensaje no es JSON válido", "body") from None
    if isinstance(payload, dict) and "readings" in payload:
        readings = payload["readings"]
        if not isinstance(readings, list) or not readings:
            raise ValidationError("El campo readings debe ser un arreglo no vacío", "readings")
        if len(readings) > MAX_READINGS_PER_MESSAGE:
            raise ValidationError(
                f"El lote supera el máximo de {MAX_READINGS_PER_MESSAGE} lecturas", "readings")
        return readings
    if isinstance(payload, dict):
        return [payload]
    raise ValidationError(
        "El mensaje debe ser una lectura o un lote con el campo readings", "body")


# ---------------------------------------------------------------------------
# Utilidades de persistencia
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _http_status(exc: ClientError) -> int:
    return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)


def _to_dynamo(value: Any) -> Any:
    """boto3 no acepta float: se convierte a Decimal (vía str, sin ruido binario)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def rows_to_parquet(rows: List[Dict[str, Any]]) -> bytes:
    """Serializa lecturas normalizadas (con "ingested_at") a Parquet.

    Esquema plano, igual que BigQuery (metrics aplanado). pyarrow se importa aquí
    para que validar y probar no lo exijan.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    ts_type = pa.timestamp("us", tz="UTC")

    def parse(value: str) -> datetime:
        return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)

    columns: Dict[str, Any] = {
        "schema_version": pa.array([r["schema_version"] for r in rows], pa.string()),
        "machine_id": pa.array([r["machine_id"] for r in rows], pa.string()),
        "timestamp": pa.array([parse(r["timestamp"]) for r in rows], ts_type),
        "zone_id": pa.array([r["location"]["zone_id"] for r in rows], pa.string()),
        "lat": pa.array([r["location"]["lat"] for r in rows], pa.float64()),
        "lon": pa.array([r["location"]["lon"] for r in rows], pa.float64()),
        "status": pa.array([r["status"] for r in rows], pa.string()),
    }
    for name in METRIC_FIELDS:
        columns[name] = pa.array([r["metrics"][name] for r in rows], pa.float64())
    columns["ingested_at"] = pa.array([parse(r["ingested_at"]) for r in rows], ts_type)

    buffer = io.BytesIO()
    pq.write_table(pa.table(columns), buffer, compression="snappy")
    return buffer.getvalue()


@dataclass
class Deps:
    """Dependencias inyectables (las pruebas pasan mocks)."""

    telemetry_table: Any
    machines_table: Any
    s3: Any
    bucket: str
    ttl_days: int = 30
    now: Callable[[], datetime] = field(default=_utc_now)
    to_parquet: Callable[[List[Dict[str, Any]]], bytes] = field(default=rows_to_parquet)


_DEPS: Optional[Deps] = None


def _build_deps_from_env() -> Deps:
    required = ("TELEMETRY_TABLE", "MACHINES_TABLE", "TELEMETRY_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Faltan variables de entorno: " + ", ".join(missing))
    import boto3  # viene en el runtime de Lambda

    dynamodb = boto3.resource("dynamodb")
    return Deps(
        telemetry_table=dynamodb.Table(os.environ["TELEMETRY_TABLE"]),
        machines_table=dynamodb.Table(os.environ["MACHINES_TABLE"]),
        s3=boto3.client("s3"),
        bucket=os.environ["TELEMETRY_BUCKET"],
        ttl_days=int(os.environ.get("TTL_DAYS", "30")),
    )


def get_deps() -> Deps:
    """Crea los clientes una sola vez por contenedor de Lambda."""
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps_from_env()
    return _DEPS


def _put_telemetry_if_new(table: Any, item: Dict[str, Any]) -> bool:
    """Idempotencia por clave natural (machine_id + timestamp).

    True si la lectura es nueva o ya era de este mismo mensaje (reintento);
    False si otra entrega distinta ya la guardó (duplicado real).
    """
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(#m) OR ingest_message_id = :mid",
            ExpressionAttributeNames={"#m": "machine_id"},
            ExpressionAttributeValues={":mid": item["ingest_message_id"]},
        )
        return True
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return False
        raise


def _telemetry_item(reading: Dict[str, Any], message_id: str, ingested_at: str,
                    ttl: int) -> Dict[str, Any]:
    location = reading["location"]
    return {
        "machine_id": reading["machine_id"],
        "timestamp": reading["timestamp"],
        "schema_version": reading["schema_version"],
        "zone_id": location["zone_id"],
        "status": reading["status"],
        "location": _to_dynamo({"lat": location["lat"], "lon": location["lon"]}),
        "metrics": _to_dynamo(reading["metrics"]),
        "ingest_message_id": message_id,
        "ingested_at": ingested_at,
        "ttl": ttl,
    }


def _partition_key(reading: Dict[str, Any], message_id: str) -> str:
    ts = reading["timestamp"]
    zone = reading["location"]["zone_id"]
    return (f"telemetry/year={ts[0:4]}/month={ts[5:7]}/day={ts[8:10]}/"
            f"zone={zone}/{message_id}.parquet")


def _write_partitions(deps: Deps, message_id: str, rows: List[Dict[str, Any]]) -> List[str]:
    """Un Parquet por partición; clave determinista e If-None-Match para ser idempotente."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_partition_key(row, message_id)].append(row)
    for key, part in groups.items():
        try:
            deps.s3.put_object(
                Bucket=deps.bucket,
                Key=key,
                Body=deps.to_parquet(part),
                IfNoneMatch="*",
                ContentType="application/vnd.apache.parquet",
            )
        except ClientError as exc:
            if _error_code(exc) == "PreconditionFailed" or _http_status(exc) == 412:
                continue  # ya estaba escrito por un intento anterior de este mensaje
            raise
    return sorted(groups)


def _update_machine_state(table: Any, reading: Dict[str, Any], ingested_at: str) -> bool:
    """Estado actual de la máquina; solo avanza si la lectura es más reciente."""
    metrics = reading["metrics"]
    names = {"#st": "status", "#loc": "location"}
    values: Dict[str, Any] = {
        ":ts": reading["timestamp"],
        ":st": reading["status"],
        ":zone": reading["location"]["zone_id"],
        ":loc": _to_dynamo({"lat": reading["location"]["lat"], "lon": reading["location"]["lon"]}),
        ":mx": _to_dynamo(metrics),
        ":now": ingested_at,
    }
    sets = ["last_seen = :ts", "#st = :st", "zone_id = :zone", "#loc = :loc",
            "last_metrics = :mx", "updated_at = :now"]
    hours = metrics.get("operating_hours_total")
    if hours is not None:
        sets.append("operating_hours_total = :oh")
        values[":oh"] = _to_dynamo(hours)
    try:
        table.update_item(
            Key={"machine_id": reading["machine_id"]},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(last_seen) OR last_seen < :ts",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if _error_code(exc) == "ConditionalCheckFailedException":
            return False  # llegó una lectura vieja; el estado ya es más reciente
        raise


# ---------------------------------------------------------------------------
# Procesamiento
# ---------------------------------------------------------------------------
def _log_rejection(err: ValidationError, request_id: str, message_id: str) -> None:
    payload = build_error(err, request_id)
    payload["message_id"] = message_id
    logger.warning(json.dumps(payload, ensure_ascii=False))


def process_message(record: Dict[str, Any], deps: Deps, request_id: str = "") -> Dict[str, Any]:
    """Procesa un mensaje SQS. Lanza si hay una falla transitoria (se reintenta)."""
    message_id = record["messageId"]
    summary = {"message_id": message_id, "accepted": 0, "duplicates": 0, "rejected": 0}

    try:
        raw_readings = extract_readings(record.get("body"))
    except ValidationError as err:
        _log_rejection(err, request_id, message_id)
        summary["rejected"] = 1
        return summary

    valid: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in raw_readings:
        try:
            reading = validate_reading(raw)
        except ValidationError as err:
            _log_rejection(err, request_id, message_id)
            summary["rejected"] += 1
            continue
        key = (reading["machine_id"], reading["timestamp"])
        if key in valid:
            summary["duplicates"] += 1  # repetida dentro del mismo lote
            continue
        valid[key] = reading

    if not valid:
        return summary

    now = deps.now()
    ingested_at = now.strftime(ISO_FORMAT)
    ttl = int(now.timestamp()) + deps.ttl_days * 86400

    written: List[Dict[str, Any]] = []
    for reading in valid.values():
        item = _telemetry_item(reading, message_id, ingested_at, ttl)
        if _put_telemetry_if_new(deps.telemetry_table, item):
            written.append(reading)
        else:
            summary["duplicates"] += 1

    if written:
        rows = [dict(r, ingested_at=ingested_at) for r in written]
        _write_partitions(deps, message_id, rows)

        latest: Dict[str, Dict[str, Any]] = {}
        for reading in written:
            current = latest.get(reading["machine_id"])
            if current is None or reading["timestamp"] > current["timestamp"]:
                latest[reading["machine_id"]] = reading
        for reading in latest.values():
            _update_machine_state(deps.machines_table, reading, ingested_at)

    summary["accepted"] = len(written)
    return summary


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Punto de entrada. Respuesta parcial por lote: solo fallan los mensajes con error."""
    deps = get_deps()
    request_id = getattr(context, "aws_request_id", "") or ""
    failures: List[Dict[str, str]] = []
    totals = {"accepted": 0, "duplicates": 0, "rejected": 0}

    for record in event.get("Records", []):
        try:
            summary = process_message(record, deps, request_id)
        except Exception:  # noqa: BLE001 - cualquier falla se reintenta y acaba en la DLQ
            logger.exception("ingesta_fallo message_id=%s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
            continue
        for name in totals:
            totals[name] += summary[name]

    logger.info(json.dumps({"event": "ingesta_lote", "request_id": request_id,
                            "messages": len(event.get("Records", [])),
                            "failed_messages": len(failures), **totals}))
    return {"batchItemFailures": failures}
