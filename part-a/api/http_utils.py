"""Utilidades HTTP compartidas por las Lambdas de la API (Parte A).

- Formato de error uniforme de la seccion 6.8 del contrato.
- Respuestas JSON con CORS (el origen sale de ALLOWED_ORIGIN).
- Lectura del contexto del autorizador (rol, zona, maquinas) para el control de alcance.

Imports absolutos desde la raiz /part-a: ``from api.http_utils import ...``.
"""
import json
import os
from decimal import Decimal

# Codigos de error y su HTTP status (6.8 + UNAUTHORIZED/FORBIDDEN aprobados).
ERROR_STATUS = {
    "VALIDATION_ERROR": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
}

# Roles de la tabla 6.9 (codigos en ingles, usados en el claim "role" del JWT).
ROLES = ("operator", "technician", "maintenance", "supervisor", "admin", "management")
# Roles cuyo alcance se limita a la zona del token (si el token trae zone_id).
ZONE_SCOPED_ROLES = ("technician", "maintenance", "supervisor")


class ApiError(Exception):
    """Error de negocio con formato 6.8."""

    def __init__(self, code, message, details=None):
        if code not in ERROR_STATUS:
            raise ValueError("codigo de error desconocido: %s" % code)
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def status(self):
        return ERROR_STATUS[self.code]


def _json_default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError("no serializable: %r" % type(obj))


def dumps(obj):
    return json.dumps(obj, default=_json_default, ensure_ascii=False, separators=(",", ":"))


def response(status, body, extra_headers=None):
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": dumps(body)}


def request_id(event, context=None):
    rid = ((event or {}).get("requestContext") or {}).get("requestId")
    return rid or getattr(context, "aws_request_id", None) or "local"


def error_body(code, message, details, rid):
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": rid}}


def error_response(err, rid):
    return response(err.status, error_body(err.code, err.message, err.details, rid))


def get_header(event, name):
    """Lectura de cabecera sin distinguir mayusculas."""
    wanted = name.lower()
    for key, value in ((event or {}).get("headers") or {}).items():
        if key.lower() == wanted:
            return value
    return None


def get_claims(event):
    """Contexto que devuelve el autorizador (valores string)."""
    return ((event or {}).get("requestContext") or {}).get("authorizer") or {}


class Caller:
    """Quien llama, segun el JWT validado por el autorizador."""

    def __init__(self, role, zone_id=None, machine_ids=None, technician_id=None, sub=None):
        self.role = role
        self.zone_id = zone_id
        self.machine_ids = list(machine_ids or [])
        self.technician_id = technician_id
        self.sub = sub

    @property
    def scope_zone(self):
        """Zona obligatoria para este llamador, o None si ve todas."""
        return self.zone_id if (self.role in ZONE_SCOPED_ROLES and self.zone_id) else None


def caller_from(event):
    ctx = get_claims(event)
    role = ctx.get("role")
    if role not in ROLES:
        raise ApiError("UNAUTHORIZED", "Rol ausente o no reconocido")
    ids = [m for m in (ctx.get("machine_ids") or "").split(",") if m]
    return Caller(
        role=role,
        zone_id=ctx.get("zone_id") or None,
        machine_ids=ids,
        technician_id=ctx.get("technician_id") or None,
        sub=ctx.get("sub") or ctx.get("principalId"),
    )


def aws_error_code(exc):
    """Codigo de error de botocore sin importar botocore."""
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")


THROTTLE_CODES = (
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "Throttling",
    "TooManyRequestsException",
)
