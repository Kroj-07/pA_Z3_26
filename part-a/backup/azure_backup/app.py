"""
af-backup-azure — Respaldo a Azure Blob (A10). Lo invoca EventBridge Scheduler.

Autor: IA-4 | Rama: ia4-puente | Carpeta: /part-a/backup

Qué hace: lista los .parquet de S3 modificados en las últimas LOOKBACK_HOURS
(36 por defecto, o `lookback_hours` en el evento) y copia a Azure Blob los que
aún no están (misma clave y mismo tamaño). Es idempotente: se puede reintentar.
Sube por bloques de 8 MB leyendo S3 en streaming; no necesita SDK de Azure
(REST + SAS con la biblioteca estándar).

Configuración (regla 4):
  Variables de entorno: ENV, SSM_PREFIX (opc.), LOOKBACK_HOURS (opc.), BLOB_PREFIX (opc.)
  Parámetros SSM (SUPUESTO: confirmar contra /contract):
    {SSM_PREFIX}/data/telemetry-bucket        -> bucket S3 con los Parquet de la IA-2
    {SSM_PREFIX}/backup/source-prefix       -> prefijo S3 a respaldar (opcional, "" por defecto)
    {SSM_PREFIX}/backup/azure-container-url -> https://<cuenta>.blob.core.windows.net/<contenedor>
    {SSM_PREFIX}/backup/sas-secret-name     -> nombre del secreto con el token SAS
  Secrets Manager: el token SAS (con o sin "?" inicial). Nunca se escribe en logs.
"""
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "test")
SSM_PREFIX = os.environ.get("SSM_PREFIX", f"/arquifusion/{ENV}").rstrip("/")
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "36"))
BLOB_PREFIX = os.environ.get("BLOB_PREFIX", "af-backup/")

AZURE_VERSION = "2021-08-06"
BLOCK_SIZE = 8 * 1024 * 1024
RETRY_CODES = (429, 500, 502, 503, 504)

_s3 = boto3.client("s3")
_ssm = boto3.client("ssm")
_sm = boto3.client("secretsmanager")
_cfg = {}


def _param(name, default=None):
    try:
        return _ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"]
    except _ssm.exceptions.ParameterNotFound:
        if default is not None:
            return default
        raise


def _config():
    if not _cfg:
        sas = _sm.get_secret_value(SecretId=_param("backup/sas-secret-name"))["SecretString"]
        _cfg.update(
            bucket=_param("data/telemetry-bucket"),
            source_prefix=_param("backup/source-prefix", default=""),
            container_url=_param("backup/azure-container-url").rstrip("/"),
            sas=sas.strip().lstrip("?"),
        )
    return _cfg


def _http(method, url, data=None, headers=None):
    """Devuelve (status, headers, body). Reintenta 429/5xx y errores de red."""
    hdrs = {"x-ms-version": AZURE_VERSION, **(headers or {})}
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return e.code, e.headers, e.read()
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("inalcanzable")


def _blob_url(cfg, blob_name, query=""):
    path = urllib.parse.quote(blob_name, safe="/")
    q = f"{query}&{cfg['sas']}" if query else cfg["sas"]
    return f"{cfg['container_url']}/{path}?{q}"


def _already_backed_up(cfg, blob_name, size):
    status, headers, _ = _http("HEAD", _blob_url(cfg, blob_name))
    return status == 200 and int(headers.get("Content-Length", -1)) == size


def _upload(cfg, key, etag):
    blob_name = BLOB_PREFIX + key
    body = _s3.get_object(Bucket=cfg["bucket"], Key=key)["Body"]
    block_ids, i = [], 0
    while True:
        chunk = body.read(BLOCK_SIZE)
        if not chunk:
            break
        bid = base64.b64encode(f"{i:08d}".encode()).decode()
        q = f"comp=block&blockid={urllib.parse.quote(bid, safe='')}"
        status, _, resp = _http(
            "PUT", _blob_url(cfg, blob_name, q), chunk,
            {"Content-Type": "application/octet-stream"},
        )
        if status != 201:
            raise RuntimeError(f"Put Block falló HTTP {status}: {resp[:200]!r}")
        block_ids.append(bid)
        i += 1

    xml = (
        '<?xml version="1.0" encoding="utf-8"?><BlockList>'
        + "".join(f"<Latest>{b}</Latest>" for b in block_ids)
        + "</BlockList>"
    ).encode()
    status, _, resp = _http(
        "PUT", _blob_url(cfg, blob_name, "comp=blocklist"), xml,
        {
            "Content-Type": "application/xml",
            "x-ms-blob-content-type": "application/octet-stream",
            "x-ms-meta-s3etag": etag.strip('"'),
        },
    )
    if status != 201:
        raise RuntimeError(f"Put Block List falló HTTP {status}: {resp[:200]!r}")


def handler(event, context):
    cfg = _config()
    lookback = float((event or {}).get("lookback_hours", LOOKBACK_HOURS))
    since = datetime.now(timezone.utc) - timedelta(hours=lookback)

    copied = skipped = failed = 0
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=cfg["source_prefix"]):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".parquet") or obj["LastModified"] < since:
                continue
            try:
                if _already_backed_up(cfg, BLOB_PREFIX + key, obj["Size"]):
                    skipped += 1
                    continue
                _upload(cfg, key, obj.get("ETag", ""))
                copied += 1
                logger.info("Respaldado %s (%d bytes)", key, obj["Size"])
            except Exception:
                failed += 1
                logger.exception("Falló el respaldo de %s", key)

    summary = {"copied": copied, "skipped": skipped, "failed": failed, "lookback_hours": lookback}
    logger.info(json.dumps(summary))
    if failed:
        # Hace visible el fallo en CloudWatch y activa el reintento del Scheduler.
        raise RuntimeError(f"Respaldo incompleto: {summary}")
    return summary