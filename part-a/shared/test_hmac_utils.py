"""Pruebas de hmac_utils. Se ejecutan con:  python -m unittest -v  (desde part-a/shared)

Usan un secreto de mentira que solo existe en estas pruebas.
"""

import unittest
from datetime import datetime, timezone

from hmac_utils import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WINDOW_SECONDS,
    build_headers,
    extract_hmac_headers,
    sign_hmac,
    verify_hmac,
)

SECRET = "secreto-solo-para-pruebas"
BODY = b'{"alert_id":"ALT-000812","machine_id":"MCH-000123"}'
NOW = 1_731_596_645  # instante fijo para pruebas
ISO_EPOCH = int(datetime(2026, 11, 14, 15, 4, 5, tzinfo=timezone.utc).timestamp())


class HmacUtilsTest(unittest.TestCase):
    def test_firma_valida(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig, now=NOW))

    def test_firma_es_hex_de_64_caracteres(self):
        _, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertEqual(len(sig), 64)
        self.assertEqual(sig, sig.lower())

    def test_cuerpo_alterado_se_rechaza(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertFalse(verify_hmac(SECRET, BODY + b" ", ts, sig, now=NOW))

    def test_secreto_incorrecto_se_rechaza(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertFalse(verify_hmac("otro-secreto", BODY, ts, sig, now=NOW))

    def test_timestamp_forma_parte_de_la_firma(self):
        # Cambiar solo el timestamp (dentro de la ventana) debe invalidar la firma.
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertFalse(verify_hmac(SECRET, BODY, str(NOW + 10), sig, now=NOW))

    def test_ventana_de_5_minutos(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig, now=NOW + WINDOW_SECONDS))
        self.assertFalse(verify_hmac(SECRET, BODY, ts, sig, now=NOW + WINDOW_SECONDS + 1))

    def test_timestamp_en_el_futuro_se_rechaza(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW + WINDOW_SECONDS + 1))
        self.assertFalse(verify_hmac(SECRET, BODY, ts, sig, now=NOW))

    def test_timestamp_iso_8601(self):
        ts = "2026-11-14T15:04:05Z"
        _, sig = sign_hmac(SECRET, BODY, timestamp=ts)
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig, now=ISO_EPOCH))

    def test_timestamp_iso_sin_zona_se_rechaza(self):
        ts = "2026-11-14T15:04:05"
        _, sig = sign_hmac(SECRET, BODY, timestamp=ts)
        self.assertFalse(verify_hmac(SECRET, BODY, ts, sig, now=ISO_EPOCH))

    def test_entradas_invalidas_devuelven_false(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertFalse(verify_hmac(SECRET, BODY, None, sig, now=NOW))
        self.assertFalse(verify_hmac(SECRET, BODY, ts, None, now=NOW))
        self.assertFalse(verify_hmac("", BODY, ts, sig, now=NOW))
        self.assertFalse(verify_hmac(SECRET, BODY, "no-es-fecha", sig, now=NOW))

    def test_secreto_vacio_no_firma(self):
        with self.assertRaises(ValueError):
            sign_hmac("", BODY)

    def test_acepta_str_y_bytes(self):
        ts, sig = sign_hmac(SECRET.encode(), BODY.decode(), timestamp=str(NOW))
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig, now=NOW))

    def test_firma_en_mayusculas_se_acepta(self):
        ts, sig = sign_hmac(SECRET, BODY, timestamp=str(NOW))
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig.upper(), now=NOW))

    def test_build_y_extract_headers(self):
        headers = build_headers(SECRET, BODY)
        self.assertEqual(set(headers), {TIMESTAMP_HEADER, SIGNATURE_HEADER})
        mixed = {"X-Timestamp": headers[TIMESTAMP_HEADER], "X-Signature": headers[SIGNATURE_HEADER]}
        ts, sig = extract_hmac_headers(mixed)
        self.assertTrue(verify_hmac(SECRET, BODY, ts, sig))

    def test_extract_sin_cabeceras(self):
        self.assertEqual(extract_hmac_headers(None), (None, None))
        self.assertEqual(extract_hmac_headers({}), (None, None))


if __name__ == "__main__":
    unittest.main()
