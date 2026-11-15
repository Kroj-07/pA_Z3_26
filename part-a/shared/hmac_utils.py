"""Firma y verificación HMAC-SHA256 para ArquiFusion (contrato v1.2, sección 11).

Mensaje firmado:  "{timestamp}.{cuerpo_crudo}"   (decisión del equipo)
Cabeceras:        x-timestamp, x-signature (hex en minúsculas)
Ventana:          +-300 segundos (5 minutos), hacia el pasado y hacia el futuro

Solo usa la biblioteca estándar, así que funciona en Lambda sin capas extra.
El secreto nunca se escribe en el repositorio: lo entrega Secrets Manager
(arquifusion/{env}/alert-hmac) o el Secret Manager de Google.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Mapping, Optional, Tuple, Union

logger = logging.getLogger(__name__)

TIMESTAMP_HEADER = "x-timestamp"
SIGNATURE_HEADER = "x-signature"
WINDOW_SECONDS = 300  # 5 minutos

BytesLike = Union[str, bytes, bytearray]


def _to_bytes(value: BytesLike) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def _compute_signature(secret: bytes, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _parse_timestamp(timestamp: str) -> Optional[float]:
    """Devuelve epoch en segundos, o None si el formato no es válido.

    Acepta epoch en segundos ("1731596645") o ISO 8601 con zona
    ("2026-11-14T15:04:05Z"). Una fecha ISO sin zona se rechaza.
    """
    ts = timestamp.strip()
    if not ts:
        return None
    if ts.isascii() and ts.isdigit():
        return float(int(ts))
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def sign_hmac(
    secret: BytesLike,
    body: BytesLike,
    *,
    timestamp: Optional[str] = None,
) -> Tuple[str, str]:
    """Firma un cuerpo. Devuelve (timestamp, signature).

    `timestamp` es epoch en segundos como texto; si no se da, se usa la hora
    actual. El texto exacto que se firma es el que debe viajar en x-timestamp.
    `body` debe ser el cuerpo crudo tal como se enviará por la red.
    """
    secret_b = _to_bytes(secret)
    if not secret_b:
        raise ValueError("El secreto HMAC no puede estar vacío")
    ts = timestamp if timestamp is not None else str(int(time.time()))
    return ts, _compute_signature(secret_b, ts, _to_bytes(body))


def verify_hmac(
    secret: BytesLike,
    body: BytesLike,
    timestamp: Optional[str],
    signature: Optional[str],
    *,
    now: Optional[float] = None,
    window_seconds: int = WINDOW_SECONDS,
) -> bool:
    """True solo si la firma es válida y la marca de tiempo está en la ventana.

    Nunca lanza por datos de entrada inválidos: devuelve False y deja el
    motivo en el log (sin secretos ni firmas). `now` existe para pruebas.
    """
    if not secret or not timestamp or not signature:
        logger.warning("hmac_rechazado motivo=faltan_datos")
        return False

    secret_b = _to_bytes(secret)
    if not secret_b:
        logger.warning("hmac_rechazado motivo=secreto_vacio")
        return False

    ts_epoch = _parse_timestamp(timestamp)
    if ts_epoch is None:
        logger.warning("hmac_rechazado motivo=timestamp_invalido")
        return False

    current = time.time() if now is None else now
    if abs(current - ts_epoch) > window_seconds:
        logger.warning("hmac_rechazado motivo=fuera_de_ventana")
        return False

    expected = _compute_signature(secret_b, timestamp, _to_bytes(body))
    if not hmac.compare_digest(expected, signature.strip().lower()):
        logger.warning("hmac_rechazado motivo=firma_no_coincide")
        return False

    return True


def build_headers(secret: BytesLike, body: BytesLike) -> dict:
    """Atajo para quien envía: devuelve las dos cabeceras listas para usar."""
    ts, sig = sign_hmac(secret, body)
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sig}


def extract_hmac_headers(
    headers: Optional[Mapping[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    """Lee (timestamp, signature) de un mapa de cabeceras sin importar mayúsculas."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    return lowered.get(TIMESTAMP_HEADER), lowered.get(SIGNATURE_HEADER)
