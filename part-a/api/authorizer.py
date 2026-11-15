"""ArquiFusion - Parte A - Autorizador Lambda (REQUEST) de la API REST /v1.

Handler: ``api.authorizer.handler``.

Valida ``Authorization: Bearer <jwt>`` (HS256, sin dependencias externas). El secreto de firma
vive en Secrets Manager (``arquifusion/{env}/jwt-signing``).

Claims del JWT:
  sub          (obligatorio)  identificador del usuario
  role         (obligatorio)  operator | technician | maintenance | supervisor | admin | management
  exp          (obligatorio)  expiracion, epoch en segundos
  zone_id      (opcional)     limita a technician/maintenance/supervisor a esa zona
  machine_ids  (opcional)     lista de maquinas (solo operator)
  technician_id (opcional)    para el rol technician

Devuelve una politica IAM que permite SOLO las rutas del rol (la politica se cachea 300 s por
token, por eso cubre todas las rutas permitidas y no solo la solicitada). Una ruta fuera de la
politica produce 403 (FORBIDDEN). Token ausente o invalido: ``raise Exception("Unauthorized")``
que API Gateway convierte en 401 (UNAUTHORIZED) con el formato 6.8.

Alcance fino (zona, maquinas, tecnico) se aplica en api/app.py con el contexto que devuelve este
autorizador (role, zone_id, machine_ids, technician_id, sub).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ALL_ROLES = ("operator", "technician", "maintenance", "supervisor", "admin", "management")
READ_ALL = ALL_ROLES
# Matriz de acceso por ruta (metodo, patron del recurso sin el stage).
# Los patrones usan '*' como en los ARN de execute-api.
ACCESS = [
    ("GET", "machines", READ_ALL),
    ("GET", "machines/*", READ_ALL),
    ("GET", "machines/*/telemetry", READ_ALL),
    ("GET", "machines/*/incidents", READ_ALL),
    ("GET", "alerts", READ_ALL),
    ("GET", "incidents", READ_ALL),
    ("GET", "notifications", READ_ALL),
    ("GET", "technicians", ("maintenance", "supervisor", "admin", "management")),
    ("GET", "inventory", ("technician", "maintenance", "supervisor", "admin", "management")),
    ("POST", "incidents/*/ack", ("technician", "maintenance", "supervisor", "admin")),
    ("POST", "incidents/*/start", ("technician", "maintenance", "supervisor", "admin")),
    ("POST", "incidents/*/close", ("technician", "maintenance", "supervisor", "admin")),
]

_secret_cache = {"value": None, "expires": 0.0}
SECRET_TTL_SECONDS = 300


class Unauthorized(Exception):
    pass


def _b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def get_secret():
    now = time.time()
    if _secret_cache["value"] and _secret_cache["expires"] > now:
        return _secret_cache["value"]
    import boto3  # importacion perezosa

    secret_id = os.environ["JWT_SECRET_ID"]
    raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id).get("SecretString") or ""
    value = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            value = parsed.get("secret") or parsed.get("key") or ""
    except ValueError:
        pass
    if not value:
        raise RuntimeError("secreto JWT vacio")
    _secret_cache.update(value=value, expires=now + SECRET_TTL_SECONDS)
    return value


def verify_jwt(token, secret, now=None):
    """Valida un JWT HS256 y devuelve sus claims; lanza Unauthorized si algo falla."""
    now = time.time() if now is None else now
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthorized("formato")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except Exception:
        raise Unauthorized("decodificacion")
    if not isinstance(header, dict) or header.get("alg") != "HS256":  # rechaza "none" y otros algoritmos
        raise Unauthorized("algoritmo")
    expected = hmac.new(secret.encode(), ("%s.%s" % (parts[0], parts[1])).encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise Unauthorized("firma")
    if not isinstance(claims, dict):
        raise Unauthorized("claims")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= now:
        raise Unauthorized("expirado")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and not isinstance(nbf, bool) and nbf > now + 30:
        raise Unauthorized("nbf")
    if claims.get("role") not in ALL_ROLES or not claims.get("sub"):
        raise Unauthorized("claims")
    return claims


def build_policy(claims, method_arn):
    """Politica que permite las rutas del rol dentro de este API y stage."""
    # method_arn = arn:aws:execute-api:{region}:{account}:{apiId}/{stage}/{METHOD}/{path...}
    base, _, rest = method_arn.partition("/")
    stage = rest.split("/", 1)[0]
    api_arn = "%s/%s" % (base, stage)
    resources = [
        "%s/%s/%s" % (api_arn, method, pattern)
        for method, pattern, roles in ACCESS
        if claims["role"] in roles
    ]
    machine_ids = claims.get("machine_ids") or []
    if isinstance(machine_ids, str):
        machine_ids = [m for m in machine_ids.split(",") if m]
    context = {
        "sub": str(claims["sub"]),
        "role": claims["role"],
        "zone_id": str(claims.get("zone_id") or ""),
        "machine_ids": ",".join(str(m) for m in machine_ids),
        "technician_id": str(claims.get("technician_id") or ""),
    }
    return {
        "principalId": str(claims["sub"]),
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": "Allow", "Resource": resources}],
        },
        "context": context,
    }


def _bearer(event):
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == "authorization" and isinstance(value, str):
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    return None


def handler(event, context=None):
    token = _bearer(event)
    if not token:
        raise Exception("Unauthorized")  # noqa: TRY002  (API Gateway lo convierte en 401)
    try:
        claims = verify_jwt(token, get_secret())
    except Unauthorized as exc:
        logger.info("token rechazado: %s", exc)  # nunca se registra el token
        raise Exception("Unauthorized")  # noqa: TRY002
    return build_policy(claims, event.get("methodArn", ""))
