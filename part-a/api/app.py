"""ArquiFusion - Parte A - Lambda de lectura de la API REST /v1 (Python 3.12).

Handler: ``api.app.handler`` (proxy de API Gateway REST).

Rutas (todas GET):
  /machines                            /machines/{machine_id}
  /machines/{machine_id}/telemetry     /machines/{machine_id}/incidents
  /alerts   /technicians   /incidents   /inventory   /notifications

Diseno:
- Todas las consultas usan expresiones en texto (sin importar boto3.dynamodb.conditions)
  y boto3 se importa de forma perezosa: el modulo se puede probar sin AWS.
- Paginacion: ``limit`` + ``next_token`` (token opaco, atado a los filtros de la consulta).
  Toda respuesta paginada devuelve {"items": [...], "next_token": str | null}.
- Errores: formato uniforme 6.8 con request_id.
- Alcance por rol: el autorizador entrega role/zone_id/machine_ids/technician_id; aqui se
  aplican (operator solo ve sus maquinas; technician/maintenance/supervisor se limitan
  a su zona si el token la trae).
"""
import base64
import hashlib
import json
import logging
import os
import re
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

SEVERITIES = ("info", "low", "medium", "high", "critical")
ALERT_STATUSES = ("open", "acknowledged", "resolved")
INCIDENT_STATUSES = ("open", "assigned", "in_progress", "closed")
MACHINE_STATUSES = ("operating", "idle", "stopped", "maintenance", "fault")
AVAILABILITIES = ("available", "busy", "off_shift")
METRICS = (
    "engine_temp_c",
    "vibration_mm_s",
    "pressure_bar",
    "fuel_rate_lph",
    "load_kg",
    "speed_kmh",
    "engine_rpm",
    "operating_hours_total",
)

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
MAX_CALLS = 10  # tope de lecturas a DynamoDB para llenar una pagina


# --------------------------------------------------------------------------- acceso a datos
_resource = None


def _ddb():
    global _resource
    if _resource is None:
        import boto3  # importacion perezosa

        _resource = boto3.resource("dynamodb")
    return _resource


def get_table(env_var):
    """Tabla DynamoDB cuyo nombre viene de una variable de entorno (origen: SSM al desplegar)."""
    name = os.environ.get(env_var)
    if not name:
        raise ApiError("INTERNAL_ERROR", "Configuracion faltante", {"variable": env_var})
    return _ddb().Table(name)


class Expr:
    """Constructor de expresiones DynamoDB (condicion de clave y filtros)."""

    def __init__(self):
        self.names = {}
        self.values = {}
        self.key_parts = []
        self.filters = []
        self._n = 0

    def _ph(self, attr, value=None, with_value=True):
        i = self._n
        self._n += 1
        n = "#n%d" % i
        self.names[n] = attr
        v = None
        if with_value:
            v = ":v%d" % i
            self.values[v] = value
        return n, v

    def key_eq(self, attr, value):
        n, v = self._ph(attr, value)
        self.key_parts.append("%s = %s" % (n, v))

    def key_cmp(self, attr, op, value):
        n, v = self._ph(attr, value)
        self.key_parts.append("%s %s %s" % (n, op, v))

    def key_between(self, attr, low, high):
        n, v1 = self._ph(attr, low)
        i = self._n
        self._n += 1
        v2 = ":v%d" % i
        self.values[v2] = high
        self.key_parts.append("%s BETWEEN %s AND %s" % (n, v1, v2))

    def filter_eq(self, attr, value):
        n, v = self._ph(attr, value)
        self.filters.append("%s = %s" % (n, v))

    def filter_contains(self, attr, value):
        n, v = self._ph(attr, value)
        self.filters.append("contains(%s, %s)" % (n, v))

    def filter_in(self, attr, values):
        n, _ = self._ph(attr, with_value=False)
        phs = []
        for val in values:
            i = self._n
            self._n += 1
            self.values[":v%d" % i] = val
            phs.append(":v%d" % i)
        self.filters.append("%s IN (%s)" % (n, ", ".join(phs)))

    def filter_attr_lte(self, attr_a, attr_b):
        na, _ = self._ph(attr_a, with_value=False)
        nb, _ = self._ph(attr_b, with_value=False)
        self.filters.append("%s <= %s" % (na, nb))

    def params(self):
        p = {}
        if self.key_parts:
            p["KeyConditionExpression"] = " AND ".join(self.key_parts)
        if self.filters:
            p["FilterExpression"] = " AND ".join(self.filters)
        if self.names:
            p["ExpressionAttributeNames"] = dict(self.names)
        if self.values:
            p["ExpressionAttributeValues"] = dict(self.values)
        return p


# --------------------------------------------------------------------------- next_token
def fingerprint(route, **filters):
    raw = json.dumps([route, sorted((k, v) for k, v in filters.items() if v is not None)], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _dec_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError


def encode_token(last_key, fp):
    raw = json.dumps({"k": last_key, "f": fp}, default=_dec_default, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_token(token, fp):
    bad = ApiError("VALIDATION_ERROR", "next_token invalido", {"field": "next_token"})
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()), parse_float=Decimal, parse_int=Decimal)
    except Exception:
        raise bad
    if not isinstance(data, dict) or not isinstance(data.get("k"), dict) or not data["k"]:
        raise bad
    if data.get("f") != fp:
        raise ApiError(
            "VALIDATION_ERROR",
            "next_token no corresponde a esta consulta (cambiaron los filtros)",
            {"field": "next_token"},
        )
    return data["k"]


def paged(table, op, expr, *, limit, token, fp, key_attrs, index=None, forward=True):
    """Devuelve (items, next_token). Llena la pagina aunque el filtro descarte items.

    key_attrs: atributos que forman la clave de paginacion (tabla + indice) para poder
    construir el token si hay que recortar la pagina.
    """
    start = decode_token(token, fp) if token else None
    items = []
    last = None
    for _ in range(MAX_CALLS):
        remaining = limit - len(items)
        kwargs = expr.params()
        kwargs["Limit"] = min(remaining * 4, 1000) if expr.filters else remaining
        if index:
            kwargs["IndexName"] = index
        if op == "query":
            kwargs["ScanIndexForward"] = forward
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = getattr(table, op)(**kwargs)
        got = resp.get("Items", [])
        last = resp.get("LastEvaluatedKey")
        if len(got) > remaining:
            got = got[:remaining]
            items.extend(got)
            last = {k: got[-1][k] for k in key_attrs if k in got[-1]}
            break
        items.extend(got)
        if len(items) >= limit or not last:
            break
        start = last
    return items, (encode_token(last, fp) if last else None)


def page_body(items, token):
    return {"items": items, "next_token": token}


# --------------------------------------------------------------------------- validacion
def _qs(event):
    return event.get("queryStringParameters") or {}


def parse_limit(qs, default_env="DEFAULT_PAGE_SIZE", max_env="MAX_PAGE_SIZE", default=50, maximum=200):
    default = int(os.environ.get(default_env, default))
    maximum = int(os.environ.get(max_env, maximum))
    raw = qs.get("limit")
    if raw in (None, ""):
        return min(default, maximum)
    if not re.fullmatch(r"\d{1,6}", raw) or not 1 <= int(raw) <= maximum:
        raise ApiError(
            "VALIDATION_ERROR",
            "limit debe ser un entero entre 1 y %d" % maximum,
            {"field": "limit", "max": maximum},
        )
    return int(raw)


def enum_param(qs, name, allowed):
    value = qs.get(name)
    if value in (None, ""):
        return None
    if value not in allowed:
        raise ApiError(
            "VALIDATION_ERROR",
            "%s invalido" % name,
            {"field": name, "allowed": list(allowed)},
        )
    return value


def id_param(qs, name):
    value = qs.get(name)
    if value in (None, ""):
        return None
    if not ID_RE.match(value):
        raise ApiError("VALIDATION_ERROR", "%s invalido" % name, {"field": name})
    return value


def iso_param(qs, name):
    value = qs.get(name)
    if value in (None, ""):
        return None
    if not ISO_RE.match(value):
        raise ApiError(
            "VALIDATION_ERROR",
            "%s debe ser ISO 8601 en UTC (ej. 2026-11-14T15:04:05Z)" % name,
            {"field": name},
        )
    return value


def bool_param(qs, name):
    value = qs.get(name)
    if value in (None, ""):
        return False
    if value.lower() not in ("true", "false"):
        raise ApiError("VALIDATION_ERROR", "%s debe ser true o false" % name, {"field": name})
    return value.lower() == "true"


def path_id(value, name):
    if not ID_RE.match(value or ""):
        raise ApiError("VALIDATION_ERROR", "%s invalido" % name, {"field": name})
    return value


# --------------------------------------------------------------------------- alcance por rol
def forbid(message, **details):
    return ApiError("FORBIDDEN", message, details)


def effective_zone(caller, requested):
    scope = caller.scope_zone
    if scope and requested and requested != scope:
        raise forbid("La zona solicitada esta fuera de tu alcance", zone_id=requested)
    return scope or requested


def check_machine_access(caller, machine):
    mid = machine.get("machine_id")
    if caller.role == "operator" and mid not in caller.machine_ids:
        raise forbid("La maquina esta fuera de tu alcance", machine_id=mid)
    if caller.scope_zone and machine.get("zone_id") != caller.scope_zone:
        raise forbid("La maquina esta fuera de tu zona", machine_id=mid)


def load_machine(machine_id, caller):
    path_id(machine_id, "machine_id")
    item = get_table("TABLE_MACHINES").get_item(Key={"machine_id": machine_id}).get("Item")
    if not item:
        raise ApiError("NOT_FOUND", "Maquina no encontrada", {"machine_id": machine_id})
    check_machine_access(caller, item)
    return item


def operator_machine(caller, requested):
    """Un operador debe consultar una de sus maquinas; si tiene una sola se asume."""
    if requested:
        if requested not in caller.machine_ids:
            raise forbid("La maquina esta fuera de tu alcance", machine_id=requested)
        return requested
    if len(caller.machine_ids) == 1:
        return caller.machine_ids[0]
    raise ApiError(
        "VALIDATION_ERROR",
        "machine_id es obligatorio para el rol operator",
        {"field": "machine_id", "allowed": caller.machine_ids},
    )


# --------------------------------------------------------------------------- rutas
def list_machines(caller, qs):
    status = enum_param(qs, "status", MACHINE_STATUSES)
    zone = effective_zone(caller, id_param(qs, "zone_id"))
    limit = parse_limit(qs)
    table = get_table("TABLE_MACHINES")

    if caller.role == "operator":
        items = []
        for mid in caller.machine_ids[:limit]:
            item = table.get_item(Key={"machine_id": mid}).get("Item")
            if item and (not status or item.get("status") == status):
                items.append(item)
        return page_body(items, None)

    expr = Expr()
    op, index, key_attrs = "scan", None, ["machine_id"]
    zone_index = os.environ.get("MACHINES_BY_ZONE_INDEX")
    if zone and zone_index:
        op, index, key_attrs = "query", zone_index, ["machine_id", "zone_id"]
        expr.key_eq("zone_id", zone)
    elif zone:
        expr.filter_eq("zone_id", zone)
    if status:
        expr.filter_eq("status", status)
    fp = fingerprint("machines", zone=zone, status=status)
    items, token = paged(
        table, op, expr, limit=limit, token=qs.get("next_token"), fp=fp, key_attrs=key_attrs, index=index
    )
    return page_body(items, token)


def get_machine(caller, qs, machine_id):
    machine = load_machine(machine_id, caller)
    expr = Expr()
    expr.key_eq("machine_id", machine_id)
    params = expr.params()
    resp = get_table("TABLE_TELEMETRY").query(Limit=1, ScanIndexForward=False, **params)
    readings = resp.get("Items", [])
    result = dict(machine)
    result["last_reading"] = readings[0] if readings else None
    return result


def machine_telemetry(caller, qs, machine_id):
    load_machine(machine_id, caller)
    frm, to = iso_param(qs, "from"), iso_param(qs, "to")
    if frm and to and frm > to:
        raise ApiError("VALIDATION_ERROR", "from debe ser menor o igual que to", {"field": "from"})
    wanted = None
    if qs.get("metrics"):
        wanted = [m.strip() for m in qs["metrics"].split(",") if m.strip()]
        unknown = [m for m in wanted if m not in METRICS]
        if unknown or not wanted:
            raise ApiError(
                "VALIDATION_ERROR",
                "metrics contiene valores no validos",
                {"field": "metrics", "unknown": unknown, "allowed": list(METRICS)},
            )
    order = enum_param(qs, "order", ("asc", "desc")) or "asc"
    limit = parse_limit(qs, "TELEMETRY_DEFAULT_PAGE_SIZE", "TELEMETRY_MAX_PAGE_SIZE", default=100, maximum=1000)

    expr = Expr()
    expr.key_eq("machine_id", machine_id)
    if frm and to:
        expr.key_between("timestamp", frm, to)
    elif frm:
        expr.key_cmp("timestamp", ">=", frm)
    elif to:
        expr.key_cmp("timestamp", "<=", to)
    fp = fingerprint("telemetry", machine_id=machine_id, frm=frm, to=to, order=order)
    items, token = paged(
        get_table("TABLE_TELEMETRY"),
        "query",
        expr,
        limit=limit,
        token=qs.get("next_token"),
        fp=fp,
        key_attrs=["machine_id", "timestamp"],
        forward=(order == "asc"),
    )
    if wanted:
        for item in items:
            metrics = item.get("metrics") or {}
            item["metrics"] = {k: metrics[k] for k in wanted if k in metrics}
    return page_body(items, token)


def machine_incidents(caller, qs, machine_id):
    load_machine(machine_id, caller)
    status = enum_param(qs, "status", INCIDENT_STATUSES)
    limit = parse_limit(qs)
    expr = Expr()
    expr.key_eq("machine_id", machine_id)
    if status:
        expr.filter_eq("status", status)
    if caller.role == "technician" and caller.technician_id:
        expr.filter_eq("technician_id", caller.technician_id)
    fp = fingerprint("machine_incidents", machine_id=machine_id, status=status, tech=caller.technician_id)
    items, token = paged(
        get_table("TABLE_INCIDENTS"),
        "query",
        expr,
        limit=limit,
        token=qs.get("next_token"),
        fp=fp,
        key_attrs=["machine_id", "opened_at"],
        forward=False,
    )
    return page_body(items, token)


def list_alerts(caller, qs):
    severity = enum_param(qs, "severity", SEVERITIES)
    status = enum_param(qs, "status", ALERT_STATUSES)
    machine_id = id_param(qs, "machine_id")
    zone = effective_zone(caller, id_param(qs, "zone_id"))
    limit = parse_limit(qs)
    table = get_table("TABLE_ALERTS")
    zone_index = os.environ.get("ALERTS_ZONE_INDEX", "by_zone_created")
    sev_index = os.environ.get("ALERTS_SEVERITY_INDEX", "by_severity_created")

    if caller.role == "operator":
        machine_id = operator_machine(caller, machine_id)
    elif machine_id:
        load_machine(machine_id, caller)  # valida que exista y este dentro del alcance

    expr = Expr()
    if machine_id:
        op, index, key_attrs, forward = "query", None, ["machine_id", "created_at"], False
        expr.key_eq("machine_id", machine_id)
    elif zone:
        op, index, key_attrs, forward = "query", zone_index, ["zone_id", "created_at", "machine_id"], False
        expr.key_eq("zone_id", zone)
    elif severity:
        op, index, key_attrs, forward = "query", sev_index, ["severity", "created_at", "machine_id"], False
        expr.key_eq("severity", severity)
    else:
        op, index, key_attrs, forward = "scan", None, ["machine_id", "created_at"], True
    # filtros que no son la clave usada
    if severity and not (index == sev_index and not machine_id and not zone):
        expr.filter_eq("severity", severity)
    if zone and machine_id:
        expr.filter_eq("zone_id", zone)
    if status:
        expr.filter_eq("status", status)
    fp = fingerprint("alerts", severity=severity, status=status, machine_id=machine_id, zone=zone)
    items, token = paged(
        table, op, expr, limit=limit, token=qs.get("next_token"), fp=fp, key_attrs=key_attrs, index=index, forward=forward
    )
    return page_body(items, token)


def list_technicians(caller, qs):
    zone = effective_zone(caller, id_param(qs, "zone_id"))
    availability = enum_param(qs, "availability", AVAILABILITIES)
    skill = qs.get("skill") or None
    if skill and not ID_RE.match(skill):
        raise ApiError("VALIDATION_ERROR", "skill invalido", {"field": "skill"})
    limit = parse_limit(qs)
    expr = Expr()
    if zone:
        expr.filter_eq("zone_id", zone)
    if availability:
        expr.filter_eq("availability", availability)
    if skill:
        expr.filter_contains("skills", skill)
    fp = fingerprint("technicians", zone=zone, availability=availability, skill=skill)
    items, token = paged(
        get_table("TABLE_TECHNICIANS"),
        "scan",
        expr,
        limit=limit,
        token=qs.get("next_token"),
        fp=fp,
        key_attrs=["technician_id"],
    )
    return page_body(items, token)


def list_incidents(caller, qs):
    status = enum_param(qs, "status", INCIDENT_STATUSES)
    machine_id = id_param(qs, "machine_id")
    zone = effective_zone(caller, id_param(qs, "zone_id"))
    limit = parse_limit(qs)
    table = get_table("TABLE_INCIDENTS")
    status_index = os.environ.get("INCIDENTS_STATUS_INDEX", "by_status")

    if caller.role == "operator":
        machine_id = operator_machine(caller, machine_id)
    elif machine_id:
        load_machine(machine_id, caller)

    expr = Expr()
    if machine_id:
        op, index, key_attrs, forward = "query", None, ["machine_id", "opened_at"], False
        expr.key_eq("machine_id", machine_id)
        if status:
            expr.filter_eq("status", status)
    elif status:
        op, index, key_attrs, forward = "query", status_index, ["status", "opened_at", "machine_id"], False
        expr.key_eq("status", status)
    else:
        op, index, key_attrs, forward = "scan", None, ["machine_id", "opened_at"], True
    if zone:
        expr.filter_eq("zone_id", zone)  # zone_id desnormalizado en el incidente (propuesta al contrato)
    if caller.role == "technician" and caller.technician_id:
        expr.filter_eq("technician_id", caller.technician_id)
    fp = fingerprint(
        "incidents", status=status, machine_id=machine_id, zone=zone, tech=caller.technician_id
    )
    items, token = paged(
        table, op, expr, limit=limit, token=qs.get("next_token"), fp=fp, key_attrs=key_attrs, index=index, forward=forward
    )
    for item in items:  # nunca se exponen los tokens de callback de Step Functions
        item.pop("ack_task_token", None)
        item.pop("close_task_token", None)
    return page_body(items, token)


def list_inventory(caller, qs):
    zone = effective_zone(caller, id_param(qs, "zone_id"))
    part_id = id_param(qs, "part_id")
    below_min = bool_param(qs, "below_min")
    limit = parse_limit(qs)
    expr = Expr()
    if zone:
        op, key_attrs = "query", ["zone_id", "part_id"]
        expr.key_eq("zone_id", zone)
        if part_id:
            expr.key_eq("part_id", part_id)
    else:
        op, key_attrs = "scan", ["zone_id", "part_id"]
        if part_id:
            expr.filter_eq("part_id", part_id)
    if below_min:
        expr.filter_attr_lte("stock", "min_stock")
    fp = fingerprint("inventory", zone=zone, part_id=part_id, below_min=below_min)
    items, token = paged(
        get_table("TABLE_INVENTORY"),
        op,
        expr,
        limit=limit,
        token=qs.get("next_token"),
        fp=fp,
        key_attrs=key_attrs,
    )
    return page_body(items, token)


def list_notifications(caller, qs):
    role = enum_param(qs, "role", ("operator", "technician", "maintenance", "supervisor", "admin", "management"))
    role = role or caller.role
    if role != caller.role and caller.role != "admin":
        raise forbid("Solo puedes leer las notificaciones de tu rol", role=role)
    since = iso_param(qs, "since")
    limit = parse_limit(qs)
    expr = Expr()
    expr.key_eq("recipient_role", role)
    if since:
        expr.key_cmp("sk", ">", since)
    if caller.scope_zone and role == caller.role:
        expr.filter_eq("zone_id", caller.scope_zone)
    if role == "operator" and caller.role == "operator":
        if not caller.machine_ids:
            return page_body([], None)
        expr.filter_in("machine_id", caller.machine_ids[:50])
    if role == "technician" and caller.role == "technician" and caller.technician_id:
        expr.filter_eq("technician_id", caller.technician_id)
    fp = fingerprint("notifications", role=role, since=since, zone=caller.scope_zone, tech=caller.technician_id)
    items, token = paged(
        get_table("TABLE_NOTIFICATIONS"),
        "query",
        expr,
        limit=limit,
        token=qs.get("next_token"),
        fp=fp,
        key_attrs=["recipient_role", "sk"],
        forward=False,
    )
    return page_body(items, token)


ROUTES = [
    ("GET", re.compile(r"^/machines$"), list_machines),
    ("GET", re.compile(r"^/machines/(?P<machine_id>[^/]+)$"), get_machine),
    ("GET", re.compile(r"^/machines/(?P<machine_id>[^/]+)/telemetry$"), machine_telemetry),
    ("GET", re.compile(r"^/machines/(?P<machine_id>[^/]+)/incidents$"), machine_incidents),
    ("GET", re.compile(r"^/alerts$"), list_alerts),
    ("GET", re.compile(r"^/technicians$"), list_technicians),
    ("GET", re.compile(r"^/incidents$"), list_incidents),
    ("GET", re.compile(r"^/inventory$"), list_inventory),
    ("GET", re.compile(r"^/notifications$"), list_notifications),
]


def handler(event, context=None):
    rid = request_id(event, context)
    try:
        method = (event.get("httpMethod") or "").upper()
        path = (event.get("path") or "").rstrip("/") or "/"
        for route_method, pattern, fn in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                caller = caller_from(event)
                return response(200, fn(caller, _qs(event), **match.groupdict()))
        raise ApiError("NOT_FOUND", "Ruta no encontrada", {"method": method, "path": path})
    except ApiError as err:
        return error_response(err, rid)
    except Exception as exc:  # noqa: BLE001
        if aws_error_code(exc) in THROTTLE_CODES:
            return error_response(ApiError("RATE_LIMITED", "Servicio ocupado, reintenta"), rid)
        logger.exception("error no controlado request_id=%s", rid)
        return error_response(ApiError("INTERNAL_ERROR", "Error interno"), rid)
