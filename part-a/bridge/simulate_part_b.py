#!/usr/bin/env python3
"""
simulate_part_b.py — Simula a la Parte B (Cloud Run) enviando una alerta firmada
con HMAC a  POST /v1/alerts  de la API de la Parte A.

Autor: IA-4 | Rama: ia4-puente | Carpeta: /part-a/bridge

Firma: usa part-a/shared/hmac_utils.py (contrato v1.2, sección 11):
    mensaje   = "{x-timestamp}.{cuerpo_crudo}"
    cabeceras = x-timestamp (epoch en segundos), x-signature (hex minúsculas)
El cuerpo que se firma es EXACTAMENTE el mismo byte-a-byte que se envía.

Origen de la configuración (nada de secretos en el repo, regla 5):
  - Secreto HMAC, en orden de prioridad:
      1. variable de entorno ALERT_HMAC_SECRET (solo para pruebas locales)
      2. Secrets Manager  arquifusion/{env}/alert-hmac  (nombre del docstring de hmac_utils)
  - URL base de la API:
      1. --api-url
      2. SSM  /arquifusion/{env}/api/base-url        (SUPUESTO: confirmar con IA-3 / contrato)

Uso:
  python simulate_part_b.py --env test
  python simulate_part_b.py --env test --payload-file ../../contract/examples/alert.json
  python simulate_part_b.py --env test --dry-run          # no envía nada
  python simulate_part_b.py --env test --tamper           # alterna el cuerpo tras firmar (espera 401/403)
  python simulate_part_b.py --env test --old-timestamp 600  # timestamp 10 min atrás (espera rechazo)

Códigos de salida: 0 = respuesta 2xx; 1 = error HTTP o de red; 2 = error de configuración.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Importar la librería de IA-1 (part-a/shared) sin copiarla.
_SHARED = Path(__file__).resolve().parents[1] / "shared"
sys.path.insert(0, str(_SHARED))
from hmac_utils import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_hmac  # noqa: E402

ALLOWED_REGIONS = ("us-east-1", "us-west-2")
ALERTS_PATH = "/v1/alerts"


def default_alert():
    """Alerta mínima de ejemplo. SUPUESTO: ajustar al esquema real del contrato
    o usar --payload-file apuntando a un mock de /contract/examples."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "alert_id": f"ALT-SIM-{int(time.time())}",
        "machine_id": "MCH-000123",
        "severity": "high",
        "message": "Alerta simulada por la Parte B (Cloud Run)",
        "created_at": now,
        "source": "part-b-simulator",
    }


def get_secret(env, region):
    local = os.environ.get("ALERT_HMAC_SECRET")
    if local:
        return local
    import boto3  # import tardío: no hace falta para --help ni con secreto local

    sm = boto3.client("secretsmanager", region_name=region)
    return sm.get_secret_value(SecretId=f"arquifusion/{env}/alert-hmac")["SecretString"]


def get_api_url(env, region, override):
    if override:
        return override.rstrip("/")
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    name = f"/arquifusion/{env}/api/base-url"  # SUPUESTO
    return ssm.get_parameter(Name=name)["Parameter"]["Value"].rstrip("/")


def main():
    p = argparse.ArgumentParser(description="Simula a la Parte B enviando POST /v1/alerts firmado.")
    p.add_argument("--env", required=True, choices=["shared", "test"])
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"), choices=ALLOWED_REGIONS)
    p.add_argument("--api-url", help="URL base de la API (si no, se lee de SSM)")
    p.add_argument("--payload-file", help="JSON de la alerta (p. ej. un mock de /contract/examples)")
    p.add_argument("--dry-run", action="store_true", help="Firma y muestra la petición sin enviarla")
    p.add_argument("--tamper", action="store_true", help="Altera el cuerpo después de firmar (prueba negativa)")
    p.add_argument("--old-timestamp", type=int, metavar="SEG", help="Usa un timestamp N segundos en el pasado (prueba negativa)")
    p.add_argument("--timeout", type=float, default=15.0)
    args = p.parse_args()

    # 1) Cuerpo crudo: se serializa UNA vez y esos mismos bytes se firman y se envían.
    if args.payload_file:
        payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    else:
        payload = default_alert()
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    # 2) Secreto y firma.
    try:
        secret = get_secret(args.env, args.region)
    except Exception as exc:
        print(f"ERROR: no se pudo obtener el secreto HMAC: {type(exc).__name__}", file=sys.stderr)
        return 2

    ts_override = None
    if args.old_timestamp:
        ts_override = str(int(time.time()) - args.old_timestamp)
    timestamp, signature = sign_hmac(secret, body, timestamp=ts_override)

    if args.tamper:
        body = body + b" "  # el servidor debe rechazar: la firma ya no coincide

    headers = {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature,
    }

    # 3) URL destino.
    try:
        base = get_api_url(args.env, args.region, args.api_url)
    except Exception as exc:
        print(f"ERROR: no se pudo resolver la URL de la API ({type(exc).__name__}). Usa --api-url.", file=sys.stderr)
        return 2
    url = base + ALERTS_PATH

    print(f"POST {url}")
    print(f"  {TIMESTAMP_HEADER}: {timestamp}")
    print(f"  cuerpo ({len(body)} bytes): {body.decode('utf-8', errors='replace')}")
    if args.dry_run:
        print("DRY-RUN: no se envió nada.")
        return 0

    # 4) Envío con los bytes exactos que se firmaron.
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print(f"HTTP {resp.status}\n{resp.read().decode('utf-8', errors='replace')}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}\n{exc.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"ERROR de red: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())