"""
af-bridge-pubsub — Puente AWS -> Google Cloud Pub/Sub (topic `telemetry-raw`).

Autor: IA-4 | Rama: ia4-puente | Carpeta: /part-a/bridge

Fuentes soportadas (se detecta por `eventSource` de cada record):
  - aws:sqs        -> el body del mensaje se publica tal cual (JSON).
  - aws:dynamodb   -> se publica NewImage deserializado (solo INSERT/MODIFY).

Configuración (nada hardcodeado, regla 4):
  Variables de entorno (las inyecta /infra/bridge/template.yaml):
    ENV                  -> dev | prod ...
    SSM_PREFIX           -> opcional, por defecto /arquifusion/{ENV}
  Parámetros SSM  (SUPUESTO: confirmar contra /contract):
    {SSM_PREFIX}/bridge/gcp-secret-name   -> nombre del secreto en Secrets Manager
    {SSM_PREFIX}/bridge/gcp-project-id    -> id del proyecto GCP
    {SSM_PREFIX}/bridge/pubsub-topic      -> por defecto "telemetry-raw"
  Secrets Manager:
    El secreto contiene el JSON completo de la service account de GCP.
    Nunca se escribe en logs ni en disco.

Respuesta: ReportBatchItemFailures (reintenta solo los records fallidos).
"""
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeDeserializer
from google.cloud import pubsub_v1
from google.oauth2 import service_account

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "dev")
SSM_PREFIX = os.environ.get("SSM_PREFIX", f"/arquifusion/{ENV}").rstrip("/")
PUBLISH_TIMEOUT_S = float(os.environ.get("PUBLISH_TIMEOUT_S", "20"))

_ssm = boto3.client("ssm")
_secrets = boto3.client("secretsmanager")
_deserializer = TypeDeserializer()

# Caché en el contenedor de Lambda (se reutiliza entre invocaciones calientes).
_cache = {"params": None, "publisher": None, "topic_path": None}


def _get_param(name, default=None):
    try:
        return _ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"]
    except _ssm.exceptions.ParameterNotFound:
        if default is not None:
            return default
        raise


def _init():
    """Carga config de SSM, la clave de GCP de Secrets Manager y crea el publisher."""
    if _cache["publisher"] is not None:
        return _cache["publisher"], _cache["topic_path"]

    secret_name = _get_param("bridge/gcp-secret-name")
    project_id = _get_param("bridge/gcp-project-id")
    topic = _get_param("bridge/pubsub-topic", default="telemetry-raw")

    secret_str = _secrets.get_secret_value(SecretId=secret_name)["SecretString"]
    creds = service_account.Credentials.from_service_account_info(
        json.loads(secret_str),
        scopes=["https://www.googleapis.com/auth/pubsub"],
    )
    publisher = pubsub_v1.PublisherClient(credentials=creds)
    topic_path = publisher.topic_path(project_id, topic)

    _cache["publisher"], _cache["topic_path"] = publisher, topic_path
    logger.info("Publisher listo para %s", topic_path)
    return publisher, topic_path


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    raise TypeError(f"No serializable: {type(o)}")


def _record_to_payload(record):
    """Devuelve (bytes, attributes) a publicar, o None si el record se ignora."""
    source = record.get("eventSource")

    if source == "aws:sqs":
        body = record["body"]
        # Si el body no es JSON válido, se publica como texto crudo.
        try:
            json.loads(body)
        except (ValueError, TypeError):
            logger.warning("Body SQS no-JSON en messageId=%s", record.get("messageId"))
        return body.encode("utf-8"), {
            "source": "sqs",
            "aws_message_id": record.get("messageId", ""),
        }

    if source == "aws:dynamodb":
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            return None
        new_image = record["dynamodb"].get("NewImage")
        if not new_image:
            return None
        item = {k: _deserializer.deserialize(v) for k, v in new_image.items()}
        return json.dumps(item, default=_json_default).encode("utf-8"), {
            "source": "dynamodb",
            "aws_event_id": record.get("eventID", ""),
        }

    logger.warning("eventSource no soportado: %s", source)
    return None


def _record_id(record):
    """Identificador para batchItemFailures según la fuente."""
    if record.get("eventSource") == "aws:sqs":
        return {"itemIdentifier": record["messageId"]}
    return {"itemIdentifier": record["dynamodb"]["SequenceNumber"]}


def handler(event, context):
    publisher, topic_path = _init()
    records = event.get("Records", [])
    pending = []   # (record, future)
    failures = []

    for record in records:
        try:
            prepared = _record_to_payload(record)
            if prepared is None:
                continue
            data, attrs = prepared
            attrs["env"] = ENV
            pending.append((record, publisher.publish(topic_path, data, **attrs)))
        except Exception:
            logger.exception("Error preparando record")
            failures.append(_record_id(record))

    deadline = time.monotonic() + PUBLISH_TIMEOUT_S
    for record, future in pending:
        try:
            future.result(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:
            logger.exception("Falló publicación en Pub/Sub")
            failures.append(_record_id(record))

    logger.info(
        "Procesados=%d publicados=%d fallidos=%d",
        len(records), len(pending) - len(failures), len(failures),
    )
    return {"batchItemFailures": failures}