"""ArquiFusion - Parte A - Callbacks de incidentes de SF-1 (Python 3.12).

Handler: ``api.incident_callbacks.handler``
  POST /v1/incidents/{incident_id}/ack     el tecnico confirma que atendera el incidente
  POST /v1/incidents/{incident_id}/start   el tecnico empieza el trabajo (status -> in_progress)
  POST /v1/incidents/{incident_id}/close   el tecnico cierra (acciones y repuestos usados)

SF-1 (Parte C) guarda en el item del incidente los tokens de callback ``ack_task_token`` y
``close_task_token`` y queda esperando. Aqui se valida, se reanuda el flujo con
``SendTaskSuccess`` y se actualiza el item. Los tokens NUNCA se devuelven al cliente.

Convenciones propuestas al contrato (por confirmar con C / IA-2):
  - Incidents necesita un GSI ``by_incident_id`` (PK incident_id) para buscar por id.
  - Atributos nuevos del incidente: ack_task_token, close_task_token, acknowledged_at,
    acknowledged_by, started_at, close_requested_at, closed_by.
  - /close NO pone status=closed: lo hace SF-1 despues de descontar repuestos
    (el incidente queda con close_requested_at hasta entonces).
Idempotencia: repetir un callback ya aplicado devuelve 200 con el estado actual.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

from api.http_utils import (
    THROTTLE_CODES,
    ApiError,
    aws_error_code,
    caller_from,
    error_response,
    request_id,
    response,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ROUTE_RE = re.compile(r"^/incidents/(?P<incident_id>[^/]+)/(?P<action>ack|start|close)$")
INCIDENT_ID_RE = re.compile(r"^INC-[A-Za-z0-9-]{1,48}$")
PART_ID_RE = re.compile(r"^PRT-[A-Za-z0-9-]{1,48}$")
ALLOWED_ROLES = ("technician", "maintenance", "supervisor", "admin")
SECRET_FIELDS = ("ack_task_token", "close_task_token")
MAX_BODY_BYTES = 64 * 1024

_resource = None
_sfn = None


def get_table():
    global _resource
    if _resource is None:
        import boto3

        _resource = boto3.resource("dynamodb")
    name = os.environ.get("TABLE_INCIDENTS")
    if not name:
        raise ApiError("INTERNAL_ERROR", "Configuracion faltante", {"variable": "TABLE_INCIDENTS"})
    return _resource.Table(name)


def get_sfn():
    global _sfn
    if _sfn is None:
        import boto3

        _sfn = boto3.client("stepfunctions")
    return _sfn


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(item):
    return {k: v for k, v in item.items() if k not in SECRET_FIELDS}


def parse_body(event):
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise ApiError("VALIDATION_ERROR", "El cuerpo supera el tamano maximo")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw, parse_float=Decimal)
    except ValueError:
        raise ApiError("VALIDATION_ERROR", "El cuerpo no es JSON valido")
    if not isinstance(data, dict):
        raise ApiError("VALIDATION_ERROR", "El cuerpo debe ser un objeto JSON")
    return data


def validate_close(data):
    errors = []
    actions = data.get("actions")
    if not isinstance(actions, list) or not actions or len(actions) > 20 or not all(
        isinstance(a, str) and a.strip() and len(a) <= 500 for a in actions
    ):
        errors.append({"field": "actions", "message": "lista de 1 a 20 textos no vacios"})
    parts = data.get("parts_replaced", [])
    if not isinstance(parts, list) or len(parts) > 100:
        errors.append({"field": "parts_replaced", "message": "debe ser una lista (maximo 100)"})
        parts = []
    for i, part in enumerate(parts):
        qty = part.get("qty") if isinstance(part, dict) else None
        if not isinstance(part, dict) or not PART_ID_RE.match(str(part.get("part_id", ""))):
            errors.append({"field": "parts_replaced[%d].part_id" % i, "message": "formato PRT-..."})
        if not isinstance(qty, (int, Decimal)) or isinstance(qty, bool) or qty < 1:
            errors.append({"field": "parts_replaced[%d].qty" % i, "message": "entero >= 1"})
    if errors:
        raise ApiError("VALIDATION_ERROR", "Cuerpo de cierre invalido", {"errors": errors})
    return [a.strip() for a in actions], [{"part_id": p["part_id"], "qty": int(p["qty"])} for p in parts]


def find_incident(incident_id):
    index = os.environ.get("INCIDENTS_BY_ID_INDEX", "by_incident_id")
    resp = get_table().query(
        IndexName=index,
        KeyConditionExpression="incident_id = :i",
        ExpressionAttributeValues={":i": incident_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        raise ApiError("NOT_FOUND", "Incidente no encontrado", {"incident_id": incident_id})
    return items[0]


def send_task_success(token, output):
    """Reanuda SF-1. Un token ya consumido se tolera (idempotencia); uno expirado es conflicto."""
    try:
        get_sfn().send_task_success(taskToken=token, output=json.dumps(output, default=str))
    except Exception as exc:  # noqa: BLE001
        code = aws_error_code(exc)
        if code == "TaskTimedOut":
            raise ApiError("CONFLICT", "La ventana de este paso expiro (el flujo ya escalo)")
        if code in ("InvalidToken", "TaskDoesNotExist"):
            logger.info("token ya consumido; se continua de forma idempotente")
            return
        raise


def update_item(incident, set_values, remove=()):
    names, values, sets = {}, {}, []
    for i, (attr, value) in enumerate(set_values.items()):
        names["#s%d" % i] = attr
        values[":s%d" % i] = value
        sets.append("#s%d = :s%d" % (i, i))
    expr = "SET " + ", ".join(sets)
    if remove:
        removes = []
        for j, attr in enumerate(remove):
            names["#r%d" % j] = attr
            removes.append("#r%d" % j)
        expr += " REMOVE " + ", ".join(removes)
    resp = get_table().update_item(
        Key={"machine_id": incident["machine_id"], "opened_at": incident["opened_at"]},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes") or {**incident, **set_values}


def check_permission(caller, incident):
    if caller.role not in ALLOWED_ROLES:
        raise ApiError("FORBIDDEN", "Tu rol no puede operar incidentes", {"role": caller.role})
    if caller.role == "technician" and caller.technician_id and incident.get("technician_id") != caller.technician_id:
        raise ApiError("FORBIDDEN", "El incidente esta asignado a otro tecnico", {"incident_id": incident.get("incident_id")})
    scope = caller.scope_zone
    if scope and incident.get("zone_id") and incident["zone_id"] != scope:
        raise ApiError("FORBIDDEN", "El incidente esta fuera de tu zona", {"incident_id": incident.get("incident_id")})


def do_ack(incident, caller, data):
    if incident.get("acknowledged_at"):
        return incident
    if incident.get("status") == "closed":
        raise ApiError("CONFLICT", "El incidente ya esta cerrado")
    token = incident.get("ack_task_token")
    if not token:
        raise ApiError("CONFLICT", "El flujo aun no espera la confirmacion de este incidente")
    stamp = now_iso()
    send_task_success(
        token,
        {"incident_id": incident["incident_id"], "technician_id": incident.get("technician_id"), "acknowledged_by": caller.sub, "acknowledged_at": stamp},
    )
    return update_item(incident, {"acknowledged_at": stamp, "acknowledged_by": caller.sub or "unknown"}, remove=("ack_task_token",))


def do_start(incident, caller, data):
    if incident.get("status") == "in_progress":
        return incident
    if incident.get("status") != "assigned":
        raise ApiError("CONFLICT", "Solo se puede iniciar un incidente asignado", {"status": incident.get("status")})
    return update_item(incident, {"status": "in_progress", "started_at": now_iso()})


def do_close(incident, caller, data):
    if incident.get("close_requested_at") or incident.get("status") == "closed":
        return incident
    if incident.get("status") not in ("assigned", "in_progress"):
        raise ApiError("CONFLICT", "Solo se puede cerrar un incidente asignado o en curso", {"status": incident.get("status")})
    actions, parts = validate_close(data)
    token = incident.get("close_task_token")
    if not token:
        raise ApiError("CONFLICT", "El flujo aun no espera el cierre de este incidente")
    stamp = now_iso()
    send_task_success(
        token,
        {"incident_id": incident["incident_id"], "actions": actions, "parts_replaced": parts, "closed_by": caller.sub, "close_requested_at": stamp},
    )
    return update_item(
        incident,
        {"actions": actions, "parts_replaced": parts, "close_requested_at": stamp, "closed_by": caller.sub or "unknown"},
        remove=("close_task_token",),
    )


ACTIONS = {"ack": do_ack, "start": do_start, "close": do_close}


def handler(event, context=None):
    rid = request_id(event, context)
    try:
        if (event.get("httpMethod") or "").upper() != "POST":
            raise ApiError("NOT_FOUND", "Ruta no encontrada")
        match = ROUTE_RE.match((event.get("path") or "").rstrip("/"))
        if not match:
            raise ApiError("NOT_FOUND", "Ruta no encontrada", {"path": event.get("path")})
        incident_id = match.group("incident_id")
        if not INCIDENT_ID_RE.match(incident_id):
            raise ApiError("VALIDATION_ERROR", "incident_id invalido", {"field": "incident_id"})
        caller = caller_from(event)
        incident = find_incident(incident_id)
        check_permission(caller, incident)
        data = parse_body(event)
        updated = ACTIONS[match.group("action")](incident, caller, data)
        return response(200, clean(updated))
    except ApiError as err:
        return error_response(err, rid)
    except Exception as exc:  # noqa: BLE001
        if aws_error_code(exc) in THROTTLE_CODES:
            return error_response(ApiError("RATE_LIMITED", "Servicio ocupado, reintenta"), rid)
        logger.exception("error no controlado request_id=%s", rid)
        return error_response(ApiError("INTERNAL_ERROR", "Error interno"), rid)
