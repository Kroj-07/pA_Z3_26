"""ArquiFusion - Parte A - POST /v1/alerts (Python 3.12).

Handler: ``api.alerts.handler``. Recibe la alerta de la Parte B (Cloud Run en Google Cloud).

Flujo:
 1. Autenticacion por firma: cabeceras ``x-timestamp`` y ``x-signature``.
    La firma es HMAC-SHA256 sobre ``"<timestamp>.<cuerpo>"`` (formato vigente) con el secreto
    ``arquifusion/{env}/alert-hmac`` de Secrets Manager. La marca de tiempo no puede
    diferir mas de HMAC_MAX_SKEW_SECONDS (300 s) del reloj actual.
 2. Validacion del cuerpo contra el contrato 6.4 y las escalas 6.5.
 3. Escritura idempotente en DynamoDB (clave machine_id + created_at; alert_id estable).
 4. Publicacion de ``alert.created`` en EventBridge. Si falla, responde 500 y B reintenta
    con el mismo cuerpo: la alerta ya guardada se detecta como duplicado y solo se
    republica el evento (bandera ``event_published``).

Suposicion sobre /part-a/shared/hmac_utils.py (IA-1), por confirmar:
    verify_signature(secret: str, timestamp: str, body: str, signature: str) -> bool
(tambien se aceptan los nombres verify_hmac / verify). La libreria compara en tiempo
constante y arma la cadena "timestamp.cuerpo"; aqui solo se valida el rango de la marca de tiempo.
"""
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

from api.http_utils import (
    ApiError,
    aws_error_code,
    dumps,
    error_response,
    get_header,
    request_id,
    response,
)

try:  # libreria de IA-1
    from shared import hmac_utils
except ImportError:  # se informa al llamar, no al importar (permite pruebas aisladas)
    hmac_utils = None

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ALERT_TYPES = (
    "predictive_failure",
    "anomaly",
    "fuel_drift",
    "low_utilization",
    "overuse",
    "common_cause",
    "maintenance_due",
)
SEVERITIES = ("info", "low", "medium", "high", "critical")
RECOMMENDED_ACTIONS = ("monitor", "inspect_within_72h", "inspect_within_24h", "stop_and_inspect_now")
SEVERITY_BANDS = {"info": (0, 24), "low": (25, 49), "medium": (50, 74), "high": (75, 89), "critical": (90, 100)}
MAX_BODY_BYTES = 256 * 1024

ALERT_ID_RE = re.compile(r"^ALT-[A-Za-z0-9-]{1,48}$")
MACHINE_ID_RE = re.compile(r"^MCH-[A-Za-z0-9-]{1,48}$")
ZONE_ID_RE = re.compile(r"^ZONA-[A-Za-z0-9-]{1,48}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

# ------------------------------------------------------------------ clientes AWS (perezosos)
_clients = {}


def _client(name):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


_table = None


def get_table():
    global _table
    if _table is None:
        import boto3

        name = os.environ.get("TABLE_ALERTS")
        if not name:
            raise ApiError("INTERNAL_ERROR", "Configuracion faltante", {"variable": "TABLE_ALERTS"})
        _table = boto3.resource("dynamodb").Table(name)
    return _table


# ------------------------------------------------------------------ secreto HMAC
_secret_cache = {"value": None, "expires": 0.0}
SECRET_TTL_SECONDS = 300


def get_hmac_secret():
    now = time.time()
    if _secret_cache["value"] and _secret_cache["expires"] > now:
        return _secret_cache["value"]
    secret_id = os.environ.get("HMAC_SECRET_ID")
    if not secret_id:
        raise ApiError("INTERNAL_ERROR", "Configuracion faltante", {"variable": "HMAC_SECRET_ID"})
    raw = _client("secretsmanager").get_secret_value(SecretId=secret_id).get("SecretString") or ""
    value = raw
    try:  # el secreto puede guardarse como texto plano o como JSON {"secret": "..."}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            value = parsed.get("secret") or parsed.get("key") or parsed.get("hmac") or ""
    except ValueError:
        pass
    if not value:
        raise ApiError("INTERNAL_ERROR", "Secreto HMAC vacio")
    _secret_cache.update(value=value, expires=now + SECRET_TTL_SECONDS)
    return value


def verify_signature(secret, timestamp, body, signature):
    if hmac_utils is None:
        logger.error("shared.hmac_utils no esta disponible en el paquete de la Lambda")
        raise ApiError("INTERNAL_ERROR", "Verificacion de firma no disponible")
    fn = (
        getattr(hmac_utils, "verify_signature", None)
        or getattr(hmac_utils, "verify_hmac", None)
        or getattr(hmac_utils, "verify", None)
    )
    if fn is None:
        logger.error("hmac_utils no expone verify_signature/verify_hmac/verify")
        raise ApiError("INTERNAL_ERROR", "Verificacion de firma no disponible")
    return bool(fn(secret, timestamp, body, signature))


def parse_timestamp(value):
    """Acepta epoch en segundos (o milisegundos) e ISO 8601; devuelve epoch en segundos."""
    value = (value or "").strip()
    try:
        if re.fullmatch(r"\d{9,13}", value):
            number = int(value)
            return number / 1000.0 if number > 10**11 else float(number)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ApiError("UNAUTHORIZED", "x-timestamp invalido", {"reason": "bad_timestamp"})


def authenticate(event, raw_body):
    timestamp = get_header(event, "x-timestamp")
    signature = get_header(event, "x-signature")
    if not timestamp or not signature:
        raise ApiError("UNAUTHORIZED", "Faltan las cabeceras x-timestamp y x-signature")
    max_skew = int(os.environ.get("HMAC_MAX_SKEW_SECONDS", "300"))
    if abs(time.time() - parse_timestamp(timestamp)) > max_skew:
        raise ApiError("UNAUTHORIZED", "x-timestamp fuera del rango permitido", {"reason": "timestamp_out_of_range"})
    if not verify_signature(get_hmac_secret(), timestamp, raw_body, signature):
        raise ApiError("UNAUTHORIZED", "Firma invalida", {"reason": "bad_signature"})


# ------------------------------------------------------------------ validacion 6.4
def _is_number(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def validate_alert(data):
    """Valida el cuerpo y devuelve el item normalizado (solo campos conocidos)."""
    if not isinstance(data, dict):
        raise ApiError("VALIDATION_ERROR", "El cuerpo debe ser un objeto JSON")
    errors = []

    def bad(field, message):
        errors.append({"field": field, "message": message})

    def need_str(field, regex=None, maximum=500):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            bad(field, "es obligatorio y debe ser texto")
        elif len(value) > maximum:
            bad(field, "supera %d caracteres" % maximum)
        elif regex and not regex.match(value):
            bad(field, "formato invalido")
        return value

    version = data.get("schema_version")
    if not isinstance(version, str) or not version.startswith("1."):
        bad("schema_version", "debe ser '1.x'")
    need_str("alert_id", ALERT_ID_RE)
    need_str("machine_id", MACHINE_ID_RE)
    need_str("zone_id", ZONE_ID_RE)
    need_str("created_at", ISO_RE)
    need_str("summary")
    for field, allowed in (("type", ALERT_TYPES), ("severity", SEVERITIES), ("recommended_action", RECOMMENDED_ACTIONS)):
        if data.get(field) not in allowed:
            bad(field, "debe ser uno de: %s" % ", ".join(allowed))
    score, confidence = data.get("score"), data.get("confidence")
    if not _is_number(score) or not 0 <= score <= 100:
        bad("score", "debe ser un numero entre 0 y 100")
    if not _is_number(confidence) or not 0 <= confidence <= 1:
        bad("confidence", "debe ser un numero entre 0 y 1")
    component = data.get("suspected_component")
    if component is not None and not isinstance(component, str):
        bad("suspected_component", "debe ser texto o null")

    evidence = data.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 50:
        bad("evidence", "debe ser una lista (maximo 50 elementos)")
    else:
        for i, ev in enumerate(evidence):
            if not isinstance(ev, dict) or not isinstance(ev.get("metric"), str):
                bad("evidence[%d]" % i, "cada evidencia requiere 'metric' (texto)")
                continue
            for numeric in ("observed", "expected"):
                if ev.get(numeric) is not None and not _is_number(ev[numeric]):
                    bad("evidence[%d].%s" % (i, numeric), "debe ser numerico")

    related = data.get("related_machine_ids", [])
    if not isinstance(related, list) or not all(isinstance(r, str) for r in related) or len(related) > 500:
        bad("related_machine_ids", "debe ser una lista de IDs de maquina")
    if data.get("type") == "common_cause" and not data.get("common_factor"):
        bad("common_factor", "es obligatorio cuando type es common_cause")

    if errors:
        raise ApiError("VALIDATION_ERROR", "El cuerpo de la alerta no cumple el contrato 6.4", {"errors": errors})

    if _is_number(score) and data["severity"] in SEVERITY_BANDS:
        low, high = SEVERITY_BANDS[data["severity"]]
        if not low <= score <= high:  # no se rechaza: solo se deja rastro para B
            logger.warning("severidad %s no coincide con score %s (alert_id=%s)", data["severity"], score, data["alert_id"])

    item = {
        key: data[key]
        for key in (
            "schema_version", "alert_id", "machine_id", "zone_id", "created_at", "type", "severity",
            "score", "confidence", "summary", "recommended_action", "evidence",
        )
    }
    item["suspected_component"] = component
    item["related_machine_ids"] = related
    if data.get("common_factor"):
        item["common_factor"] = data["common_factor"]
    return item


# ------------------------------------------------------------------ evento
def publish_alert_created(item):
    detail = {k: v for k, v in item.items() if k != "event_published"}
    resp = _client("events").put_events(
        Entries=[
            {
                "EventBusName": os.environ.get("EVENT_BUS_NAME", "default"),
                "Source": "arquifusion.alerts",
                "DetailType": "alert.created",
                "Detail": dumps(detail),
            }
        ]
    )
    if resp.get("FailedEntryCount"):
        raise RuntimeError("EventBridge rechazo el evento: %s" % resp.get("Entries"))


def mark_published(table, item):
    table.update_item(
        Key={"machine_id": item["machine_id"], "created_at": item["created_at"]},
        UpdateExpression="SET event_published = :t",
        ExpressionAttributeValues={":t": True},
    )


# ------------------------------------------------------------------ handler
def handler(event, context=None):
    rid = request_id(event, context)
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
            raise ApiError("VALIDATION_ERROR", "El cuerpo supera el tamano maximo", {"max_bytes": MAX_BODY_BYTES})

        authenticate(event, raw)  # primero la firma: nada se parsea sin autenticar

        try:
            data = json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise ApiError("VALIDATION_ERROR", "El cuerpo no es JSON valido")
        item = validate_alert(data)
        item["status"] = "open"
        item["ingested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        item["event_published"] = False

        table = get_table()
        replay = False
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(machine_id) AND attribute_not_exists(created_at)",
            )
        except Exception as exc:  # noqa: BLE001
            if aws_error_code(exc) != "ConditionalCheckFailedException":
                raise
            existing = table.get_item(
                Key={"machine_id": item["machine_id"], "created_at": item["created_at"]}, ConsistentRead=True
            ).get("Item")
            if not existing or existing.get("alert_id") != item["alert_id"]:
                raise ApiError(
                    "CONFLICT",
                    "Ya existe otra alerta para esa maquina y created_at",
                    {"machine_id": item["machine_id"], "created_at": item["created_at"]},
                )
            if existing.get("event_published"):
                return response(200, {"alert_id": item["alert_id"], "machine_id": item["machine_id"], "status": "duplicate"})
            item, replay = existing, True  # guardada pero sin evento: se republica

        try:
            publish_alert_created(item)
            mark_published(table, item)
        except Exception:  # noqa: BLE001
            logger.exception("fallo al publicar alert.created request_id=%s alert_id=%s", rid, item["alert_id"])
            raise ApiError(
                "INTERNAL_ERROR",
                "La alerta se guardo pero no se pudo publicar el evento; reintenta con el mismo cuerpo",
                {"alert_id": item["alert_id"], "retryable": True},
            )
        status = "republished" if replay else "created"
        return response(
            200 if replay else 201,
            {"alert_id": item["alert_id"], "machine_id": item["machine_id"], "status": status},
        )
    except ApiError as err:
        return error_response(err, rid)
    except Exception:  # noqa: BLE001
        logger.exception("error no controlado request_id=%s", rid)
        return error_response(ApiError("INTERNAL_ERROR", "Error interno"), rid)
