#!/usr/bin/env python3
"""
restore_from_azure.py — Restaura Parquet desde Azure Blob a S3 (inverso de af-backup-azure).

Autor: IA-4 | Rama: ia4-puente | Carpeta: /part-a/backup

Lee los blobs bajo BLOB_PREFIX (por defecto "af-backup/"), quita ese prefijo y los
sube a S3 con la misma clave. Salta los que ya existen con el mismo tamaño
(usa --overwrite para forzar). Descarga en streaming: no llena disco ni memoria.

Configuración (mismos nombres que el Lambda de respaldo; SUPUESTO: confirmar contra /contract):
  SSM  /arquifusion/{env}/data/parquet-bucket          -> bucket destino (o --bucket)
  SSM  /arquifusion/{env}/backup/azure-container-url   -> URL del contenedor (o --container-url)
  SSM  /arquifusion/{env}/backup/sas-secret-name       -> secreto con el SAS
  Para pruebas locales: variable de entorno AZURE_SAS_TOKEN (nunca la subas al repo).

El SAS necesita permisos de lectura y listado (r, l).

Uso:
  python restore_from_azure.py --env test --dry-run
  python restore_from_azure.py --env test --prefix 2026/11/ --since 2026-11-01T00:00:00Z
  python restore_from_azure.py --env test --bucket mi-bucket --dest-prefix restauracion/
Códigos de salida: 0 = ok; 1 = hubo errores; 2 = error de configuración.
"""
import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import boto3
from botocore.exceptions import ClientError

ALLOWED_REGIONS = ("us-east-1", "us-west-2")
AZURE_VERSION = "2021-08-06"


def get_ssm(ssm, env, name):
    return ssm.get_parameter(Name=f"/arquifusion/{env}/{name}")["Parameter"]["Value"]


def list_blobs(container_url, sas, prefix):
    marker = None
    while True:
        q = {"restype": "container", "comp": "list", "prefix": prefix}
        if marker:
            q["marker"] = marker
        url = f"{container_url}?{urllib.parse.urlencode(q)}&{sas}"
        req = urllib.request.Request(url, headers={"x-ms-version": AZURE_VERSION})
        with urllib.request.urlopen(req, timeout=60) as r:
            root = ET.fromstring(r.read())
        for b in root.iter("Blob"):
            props = b.find("Properties")
            yield (
                b.findtext("Name"),
                int(props.findtext("Content-Length")),
                parsedate_to_datetime(props.findtext("Last-Modified")),
            )
        marker = root.findtext("NextMarker") or None
        if not marker:
            return


def blob_stream(container_url, sas, name):
    url = f"{container_url}/{urllib.parse.quote(name, safe='/')}?{sas}"
    req = urllib.request.Request(url, headers={"x-ms-version": AZURE_VERSION})
    return urllib.request.urlopen(req, timeout=120)


def s3_has_same_size(s3, bucket, key, size):
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == size
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def main():
    p = argparse.ArgumentParser(description="Restaura Parquet desde Azure Blob a S3.")
    p.add_argument("--env", required=True, choices=["shared", "test"])
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"), choices=ALLOWED_REGIONS)
    p.add_argument("--bucket", help="Bucket S3 destino (si no, SSM data/parquet-bucket)")
    p.add_argument("--container-url", help="URL del contenedor Azure (si no, SSM backup/azure-container-url)")
    p.add_argument("--blob-prefix", default="af-backup/", help="Prefijo usado por el respaldo")
    p.add_argument("--prefix", default="", help="Filtra por prefijo relativo (p. ej. 2026/11/)")
    p.add_argument("--dest-prefix", default="", help="Prefijo extra en S3 al restaurar")
    p.add_argument("--since", help="Solo blobs modificados desde esta fecha ISO 8601 con zona")
    p.add_argument("--overwrite", action="store_true", help="Sobrescribe aunque ya exista igual en S3")
    p.add_argument("--dry-run", action="store_true", help="Lista lo que restauraría, sin copiar")
    args = p.parse_args()

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    try:
        ssm = boto3.client("ssm", region_name=args.region)
        bucket = args.bucket or get_ssm(ssm, args.env, "data/parquet-bucket")
        container = (args.container_url or get_ssm(ssm, args.env, "backup/azure-container-url")).rstrip("/")
        sas = os.environ.get("AZURE_SAS_TOKEN")
        if not sas:
            sm = boto3.client("secretsmanager", region_name=args.region)
            sas = sm.get_secret_value(SecretId=get_ssm(ssm, args.env, "backup/sas-secret-name"))["SecretString"]
        sas = sas.strip().lstrip("?")
    except Exception as exc:
        print(f"ERROR de configuración: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    s3 = boto3.client("s3", region_name=args.region)
    restored = skipped = failed = 0

    for name, size, modified in list_blobs(container, sas, args.blob_prefix + args.prefix):
        if not name.endswith(".parquet") or (since and modified < since):
            continue
        key = args.dest_prefix + name[len(args.blob_prefix):]
        try:
            if not args.overwrite and s3_has_same_size(s3, bucket, key, size):
                skipped += 1
                continue
            if args.dry_run:
                print(f"[dry-run] azure:{name} -> s3://{bucket}/{key} ({size} bytes)")
                restored += 1
                continue
            with blob_stream(container, sas, name) as stream:
                s3.upload_fileobj(stream, bucket, key)
            print(f"restaurado s3://{bucket}/{key} ({size} bytes)")
            restored += 1
        except Exception as exc:
            failed += 1
            print(f"ERROR en {name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"Resumen: restaurados={restored} omitidos={skipped} fallidos={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())