"""Genera un JWT HS256 para probar la API (demo / pruebas). No es parte de ninguna Lambda.

Uso (con credenciales del Learner Lab ya cargadas en el entorno):
  python part-a/api/mint_token.py --env test --role maintenance --sub maria --zone ZONA-NORTE-02
  python part-a/api/mint_token.py --env test --role operator --sub luis --machines MCH-000123,MCH-000124
  python part-a/api/mint_token.py --env test --role technician --sub TEC-0007 --technician-id TEC-0007 --zone ZONA-NORTE-02

Sin acceso a AWS se puede pasar el secreto por variable de entorno JWT_SECRET (solo para pruebas
locales; nunca lo guardes en el repositorio).
El token se imprime en la salida estandar; su vigencia por defecto es de 8 horas.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time

ROLES = ("operator", "technician", "maintenance", "supervisor", "admin", "management")


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mint(secret, claims):
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), ("%s.%s" % (header, payload)).encode(), hashlib.sha256).digest()
    return "%s.%s.%s" % (header, payload, b64url(signature))


def load_secret(env):
    if os.environ.get("JWT_SECRET"):
        return os.environ["JWT_SECRET"]
    import boto3

    raw = boto3.client("secretsmanager").get_secret_value(SecretId="arquifusion/%s/jwt-signing" % env)["SecretString"]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("secret") or parsed.get("key")
    except ValueError:
        pass
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="test")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--sub", required=True)
    parser.add_argument("--zone")
    parser.add_argument("--machines", help="IDs separados por coma (rol operator)")
    parser.add_argument("--technician-id")
    parser.add_argument("--hours", type=float, default=8.0)
    args = parser.parse_args()

    now = int(time.time())
    claims = {"sub": args.sub, "role": args.role, "iat": now, "exp": now + int(args.hours * 3600)}
    if args.zone:
        claims["zone_id"] = args.zone
    if args.machines:
        claims["machine_ids"] = [m for m in args.machines.split(",") if m]
    if args.technician_id:
        claims["technician_id"] = args.technician_id
    print(mint(load_secret(args.env), claims))


if __name__ == "__main__":
    sys.exit(main())
