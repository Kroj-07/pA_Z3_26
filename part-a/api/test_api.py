"""Pruebas unitarias de la Parte A (API, alertas, autorizador, callbacks, notificador).

Sin AWS ni dependencias externas: usa unittest (compatible con pytest) y tablas/clientes falsos.
Ejecutar desde la carpeta /part-a:
    python -m unittest api.test_api -v
    # o con pytest, si lo tienes:  python -m pytest api/test_api.py -q
"""
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # /part-a
sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "ALLOWED_ORIGIN": "*",
        "TABLE_MACHINES": "machines",
        "TABLE_ALERTS": "alerts",
        "TABLE_INCIDENTS": "incidents",
        "TABLE_TECHNICIANS": "technicians",
        "TABLE_TELEMETRY": "telemetry",
        "TABLE_INVENTORY": "inventory",
        "TABLE_NOTIFICATIONS": "notifications",
        "EVENT_BUS_NAME": "af-dev-arquifusion-events",
        "HMAC_MAX_SKEW_SECONDS": "300",
        "TOPIC_MAINTENANCE": "arn:aws:sns:us-east-1:1:af-dev-notify-maintenance",
        "TOPIC_OPERATOR": "arn:aws:sns:us-east-1:1:af-dev-notify-operator",
        "TOPIC_SUPERVISOR": "arn:aws:sns:us-east-1:1:af-dev-notify-supervisor",
        "TOPIC_TECHNICIAN": "arn:aws:sns:us-east-1:1:af-dev-notify-technician",
        "TOPIC_ADMIN": "arn:aws:sns:us-east-1:1:af-dev-notify-admin",
        "TOPIC_MANAGEMENT": "arn:aws:sns:us-east-1:1:af-dev-notify-management",
    }
)

# La libreria real es de IA-1. Si no esta en el repo, se usa una equivalente (firma sobre "timestamp.cuerpo").
try:
    from shared import hmac_utils  # noqa: F401
    USING_FAKE_HMAC = False
except ImportError:
    fake = types.ModuleType("shared.hmac_utils")

    def _verify(secret, timestamp, body, signature):
        expected = hmac.new(secret.encode(), ("%s.%s" % (timestamp, body)).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    fake.verify_signature = _verify
    pkg = types.ModuleType("shared")
    pkg.hmac_utils = fake
    sys.modules["shared"] = pkg
    sys.modules["shared.hmac_utils"] = fake
    USING_FAKE_HMAC = True

from api import alerts, app, authorizer, incident_callbacks, mint_token  # noqa: E402
from events import notifier  # noqa: E402


# --------------------------------------------------------------------------- dobles de prueba
class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    """Tabla DynamoDB minima: pagina por Limit/ExclusiveStartKey; ignora las expresiones."""

    def __init__(self, items=(), key_attrs=("id",), pred=None):
        self.items = [dict(i) for i in items]
        self.key_attrs = list(key_attrs)
        self.pred = pred
        self.calls = []
        self.puts = []
        self.updates = []
        self.raise_on_query = None

    def _chunk(self, kw):
        start = 0
        esk = kw.get("ExclusiveStartKey")
        if esk:
            for i, it in enumerate(self.items):
                if all(it.get(k) == v for k, v in esk.items()):
                    start = i + 1
                    break
        limit = kw.get("Limit", len(self.items))
        chunk = self.items[start : start + limit]
        more = start + limit < len(self.items)
        last = {k: chunk[-1][k] for k in self.key_attrs if k in chunk[-1]} if (more and chunk) else None
        if self.pred and "FilterExpression" in kw:
            chunk = [c for c in chunk if self.pred(c)]
        out = {"Items": [dict(c) for c in chunk]}
        if last:
            out["LastEvaluatedKey"] = last
        return out

    def query(self, **kw):
        self.calls.append(("query", kw))
        if self.raise_on_query:
            raise self.raise_on_query
        return self._chunk(kw)

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        if self.raise_on_query:
            raise self.raise_on_query
        return self._chunk(kw)

    def get_item(self, Key, **kw):
        for it in self.items:
            if all(it.get(k) == v for k, v in Key.items()):
                return {"Item": dict(it)}
        return {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression:
            for it in self.items:
                if all(it.get(k) == Item.get(k) for k in self.key_attrs):
                    raise FakeClientError("ConditionalCheckFailedException")
        self.items.append(dict(Item))
        self.puts.append(dict(Item))

    def update_item(self, **kw):
        self.updates.append(kw)
        return {}


def api_event(method, path, qs=None, role="admin", zone="", machine_ids="", technician_id="", body=None, headers=None):
    authorizer_ctx = (
        {"role": role, "zone_id": zone, "machine_ids": machine_ids, "technician_id": technician_id, "sub": "u1"} if role else {}
    )
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": qs,
        "headers": headers or {},
        "body": body,
        "requestContext": {"requestId": "req-123", "authorizer": authorizer_ctx},
    }


def body_of(resp):
    return json.loads(resp["body"])


def machine(mid, zone="ZONA-N"):
    return {"machine_id": mid, "zone_id": zone, "status": "operating"}


def alert_rows(n, machine_id="MCH-000001", zone="ZONA-N"):
    return [
        {
            "machine_id": machine_id,
            "created_at": "2026-11-14T10:%02d:00Z" % (59 - i),
            "alert_id": "ALT-%03d" % i,
            "zone_id": zone,
            "severity": "high",
        }
        for i in range(n)
    ]


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tables = {
            "TABLE_MACHINES": FakeTable([machine("MCH-000001"), machine("MCH-000002", "ZONA-S")], ("machine_id",)),
            "TABLE_ALERTS": FakeTable([], ("machine_id", "created_at")),
            "TABLE_INCIDENTS": FakeTable([], ("machine_id", "opened_at")),
            "TABLE_TECHNICIANS": FakeTable([], ("technician_id",)),
            "TABLE_TELEMETRY": FakeTable([], ("machine_id", "timestamp")),
            "TABLE_INVENTORY": FakeTable([], ("zone_id", "part_id")),
            "TABLE_NOTIFICATIONS": FakeTable([], ("recipient_role", "sk")),
        }
        self._orig = app.get_table
        app.get_table = lambda var: self.tables[var]

    def tearDown(self):
        app.get_table = self._orig

    def get(self, path, qs=None, **kw):
        return app.handler(api_event("GET", path, qs, **kw), None)


# --------------------------------------------------------------------------- formato de error 6.8
class ErrorFormatTests(AppTestCase):
    def assert_error(self, resp, status, code):
        self.assertEqual(resp["statusCode"], status)
        err = body_of(resp)["error"]
        self.assertEqual(err["code"], code)
        self.assertEqual(set(err), {"code", "message", "details", "request_id"})
        self.assertEqual(err["request_id"], "req-123")
        self.assertIsInstance(err["details"], dict)
        self.assertEqual(resp["headers"]["Content-Type"], "application/json; charset=utf-8")
        return err

    def test_ruta_desconocida_404(self):
        self.assert_error(self.get("/nada"), 404, "NOT_FOUND")

    def test_metodo_no_soportado_404(self):
        self.assert_error(app.handler(api_event("DELETE", "/machines"), None), 404, "NOT_FOUND")

    def test_sin_rol_401(self):
        self.assert_error(self.get("/machines", role=None), 401, "UNAUTHORIZED")

    def test_rol_desconocido_401(self):
        self.assert_error(self.get("/machines", role="hacker"), 401, "UNAUTHORIZED")

    def test_limit_no_numerico(self):
        err = self.assert_error(self.get("/alerts", {"limit": "abc"}), 400, "VALIDATION_ERROR")
        self.assertEqual(err["details"]["field"], "limit")

    def test_limit_cero_y_excedido(self):
        self.assert_error(self.get("/alerts", {"limit": "0"}), 400, "VALIDATION_ERROR")
        err = self.assert_error(self.get("/alerts", {"limit": "201"}), 400, "VALIDATION_ERROR")
        self.assertEqual(err["details"]["max"], 200)

    def test_severity_invalida_lista_valores_permitidos(self):
        err = self.assert_error(self.get("/alerts", {"severity": "terrible"}), 400, "VALIDATION_ERROR")
        self.assertIn("critical", err["details"]["allowed"])

    def test_id_con_caracteres_raros(self):
        self.assert_error(self.get("/alerts", {"machine_id": "MCH 1;drop"}), 400, "VALIDATION_ERROR")

    def test_maquina_inexistente_404(self):
        self.assert_error(self.get("/machines/MCH-999999"), 404, "NOT_FOUND")

    def test_operador_otra_maquina_403(self):
        self.assert_error(self.get("/machines/MCH-000002", role="operator", machine_ids="MCH-000001"), 403, "FORBIDDEN")

    def test_supervisor_otra_zona_403(self):
        self.assert_error(self.get("/machines/MCH-000002", role="supervisor", zone="ZONA-N"), 403, "FORBIDDEN")
        self.assert_error(self.get("/alerts", {"zone_id": "ZONA-S"}, role="supervisor", zone="ZONA-N"), 403, "FORBIDDEN")

    def test_operador_sin_machine_id_y_varias_maquinas(self):
        err = self.assert_error(self.get("/alerts", role="operator", machine_ids="MCH-000001,MCH-000002"), 400, "VALIDATION_ERROR")
        self.assertEqual(err["details"]["field"], "machine_id")

    def test_telemetria_parametros_invalidos(self):
        base = "/machines/MCH-000001/telemetry"
        self.assert_error(self.get(base, {"from": "ayer"}), 400, "VALIDATION_ERROR")
        self.assert_error(self.get(base, {"metrics": "engine_temp_c,inventada"}), 400, "VALIDATION_ERROR")
        self.assert_error(
            self.get(base, {"from": "2026-11-14T10:00:00Z", "to": "2026-11-14T09:00:00Z"}), 400, "VALIDATION_ERROR"
        )
        self.assert_error(self.get(base, {"limit": "1001"}), 400, "VALIDATION_ERROR")

    def test_error_interno_no_filtra_detalles(self):
        self.tables["TABLE_TECHNICIANS"].raise_on_query = RuntimeError("clave secreta xyz")
        err = self.assert_error(self.get("/technicians"), 500, "INTERNAL_ERROR")
        self.assertNotIn("xyz", json.dumps(err))

    def test_throttling_de_dynamodb_es_429(self):
        self.tables["TABLE_TECHNICIANS"].raise_on_query = FakeClientError("ProvisionedThroughputExceededException")
        self.assert_error(self.get("/technicians"), 429, "RATE_LIMITED")


# --------------------------------------------------------------------------- paginacion
class PaginationTests(AppTestCase):
    def collect(self, path, qs, **kw):
        ids, tokens, sizes = [], [], []
        token = None
        for _ in range(20):
            q = dict(qs)
            if token:
                q["next_token"] = token
            resp = self.get(path, q, **kw)
            self.assertEqual(resp["statusCode"], 200, resp["body"])
            body = body_of(resp)
            self.assertIn("next_token", body)
            ids += [i["alert_id"] for i in body["items"]]
            sizes.append(len(body["items"]))
            tokens.append(body["next_token"])
            token = body["next_token"]
            if not token:
                break
        return ids, tokens, sizes

    def test_paginas_sin_duplicados_ni_faltantes(self):
        rows = alert_rows(5)
        self.tables["TABLE_ALERTS"] = FakeTable(rows, ("machine_id", "created_at"))
        ids, tokens, sizes = self.collect("/alerts", {"machine_id": "MCH-000001", "limit": "2"})
        self.assertEqual(sizes, [2, 2, 1])
        self.assertEqual(ids, [r["alert_id"] for r in rows])
        self.assertTrue(tokens[0] and tokens[1])
        self.assertIsNone(tokens[-1])

    def test_ultima_pagina_exacta_devuelve_next_token_null(self):
        self.tables["TABLE_ALERTS"] = FakeTable(alert_rows(4), ("machine_id", "created_at"))
        _, tokens, sizes = self.collect("/alerts", {"machine_id": "MCH-000001", "limit": "4"})
        self.assertEqual(sizes, [4])
        self.assertIsNone(tokens[-1])

    def test_pagina_se_recorta_cuando_el_filtro_devuelve_de_mas(self):
        rows = alert_rows(7)
        self.tables["TABLE_ALERTS"] = FakeTable(rows, ("zone_id", "created_at", "machine_id"))
        ids, _, sizes = self.collect("/alerts", {"zone_id": "ZONA-N", "severity": "high", "limit": "2"})
        self.assertEqual(sizes, [2, 2, 2, 1])
        self.assertEqual(ids, [r["alert_id"] for r in rows])
        kind, kwargs = self.tables["TABLE_ALERTS"].calls[0]
        self.assertEqual(kind, "query")
        self.assertEqual(kwargs["IndexName"], "by_zone_created")
        self.assertFalse(kwargs["ScanIndexForward"])  # mas recientes primero

    def test_filtros_que_no_dan_clave_se_aplican_como_filtro(self):
        self.tables["TABLE_ALERTS"] = FakeTable(alert_rows(1), ("machine_id", "created_at"))
        self.get("/alerts", {"severity": "high"})
        _, kwargs = self.tables["TABLE_ALERTS"].calls[0]
        self.assertEqual(kwargs["IndexName"], "by_severity_created")
        self.assertNotIn("FilterExpression", kwargs)  # la severidad ya es la clave del indice

    def test_token_de_otra_consulta_se_rechaza(self):
        self.tables["TABLE_ALERTS"] = FakeTable(alert_rows(5), ("machine_id", "created_at"))
        token = body_of(self.get("/alerts", {"machine_id": "MCH-000001", "limit": "2"}))["next_token"]
        resp = self.get("/alerts", {"machine_id": "MCH-000001", "status": "open", "limit": "2", "next_token": token})
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(body_of(resp)["error"]["details"]["field"], "next_token")

    def test_token_basura_se_rechaza(self):
        for bad in ("%%%", "abc", "e30"):  # e30 = "{}"
            resp = self.get("/alerts", {"machine_id": "MCH-000001", "next_token": bad})
            self.assertEqual(resp["statusCode"], 400, bad)
            self.assertEqual(body_of(resp)["error"]["code"], "VALIDATION_ERROR")

    def test_tamano_por_defecto(self):
        self.tables["TABLE_ALERTS"] = FakeTable(alert_rows(60), ("machine_id", "created_at"))
        body = body_of(self.get("/alerts", {"machine_id": "MCH-000001"}))
        self.assertEqual(len(body["items"]), 50)
        self.assertIsNotNone(body["next_token"])

    def test_telemetria_limite_1000_y_filtro_de_metricas(self):
        rows = [
            {"machine_id": "MCH-000001", "timestamp": "2026-11-14T10:%02d:00Z" % i,
             "metrics": {"engine_temp_c": 90 + i, "vibration_mm_s": 4, "fuel_rate_lph": 30}}
            for i in range(3)
        ]
        self.tables["TABLE_TELEMETRY"] = FakeTable(rows, ("machine_id", "timestamp"))
        resp = self.get("/machines/MCH-000001/telemetry", {"metrics": "engine_temp_c", "limit": "1000", "order": "desc"})
        self.assertEqual(resp["statusCode"], 200, resp["body"])
        items = body_of(resp)["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(list(i["metrics"]) == ["engine_temp_c"] for i in items))
        _, kwargs = self.tables["TABLE_TELEMETRY"].calls[0]
        self.assertEqual(kwargs["Limit"], 1000)
        self.assertFalse(kwargs["ScanIndexForward"])

    def test_detalle_de_maquina_incluye_ultima_lectura(self):
        self.tables["TABLE_TELEMETRY"] = FakeTable(
            [{"machine_id": "MCH-000001", "timestamp": "2026-11-14T10:00:00Z", "metrics": {"engine_temp_c": 91}}],
            ("machine_id", "timestamp"),
        )
        body = body_of(self.get("/machines/MCH-000001"))
        self.assertEqual(body["machine_id"], "MCH-000001")
        self.assertEqual(body["last_reading"]["metrics"]["engine_temp_c"], 91)

    def test_operador_solo_lista_sus_maquinas(self):
        body = body_of(self.get("/machines", role="operator", machine_ids="MCH-000001"))
        self.assertEqual([m["machine_id"] for m in body["items"]], ["MCH-000001"])
        self.assertIsNone(body["next_token"])

    def test_incidentes_nunca_exponen_tokens(self):
        self.tables["TABLE_INCIDENTS"] = FakeTable(
            [{"machine_id": "MCH-000001", "opened_at": "2026-11-14T10:00:00Z", "incident_id": "INC-1", "ack_task_token": "SECRETO"}],
            ("machine_id", "opened_at"),
        )
        resp = self.get("/incidents", {"machine_id": "MCH-000001"})
        self.assertNotIn("SECRETO", resp["body"])


# --------------------------------------------------------------------------- POST /alerts
SECRET = "s3cret-hmac"


def valid_alert(**over):
    data = {
        "schema_version": "1.0",
        "alert_id": "ALT-000812",
        "machine_id": "MCH-000123",
        "zone_id": "ZONA-NORTE-02",
        "created_at": "2026-11-14T15:10:00Z",
        "type": "predictive_failure",
        "severity": "high",
        "score": 82,
        "confidence": 0.78,
        "summary": "Temperatura y vibracion en aumento sostenido durante 3 dias",
        "suspected_component": "cooling_system",
        "recommended_action": "inspect_within_24h",
        "evidence": [{"metric": "engine_temp_c", "observed": 104.2, "expected": 91.0, "window": "6h", "trend": "rising"}],
        "related_machine_ids": [],
    }
    data.update(over)
    return data


def signed_event(data=None, raw=None, ts=None, secret=SECRET, signature=None):
    raw = raw if raw is not None else json.dumps(data if data is not None else valid_alert())
    ts = str(int(time.time())) if ts is None else str(ts)
    sig = signature or hmac.new(secret.encode(), ("%s.%s" % (ts, raw)).encode(), hashlib.sha256).hexdigest()
    return api_event("POST", "/alerts", role=None, body=raw, headers={"X-Timestamp": ts, "X-Signature": sig})


class FakeEvents:
    def __init__(self, fail=False):
        self.entries, self.fail = [], fail

    def put_events(self, Entries):
        if self.fail:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}
        self.entries += Entries
        return {"FailedEntryCount": 0, "Entries": [{}]}


class PostAlertsTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable([], ("machine_id", "created_at"))
        self.events = FakeEvents()
        self._saved = (alerts.get_table, alerts._client, alerts.get_hmac_secret)
        alerts.get_table = lambda: self.table
        alerts._client = lambda name: self.events
        alerts.get_hmac_secret = lambda: SECRET

    def tearDown(self):
        alerts.get_table, alerts._client, alerts.get_hmac_secret = self._saved

    def call(self, event):
        resp = alerts.handler(event, None)
        return resp, body_of(resp)

    def test_sin_cabeceras_401(self):
        event = signed_event()
        event["headers"] = {}
        resp, body = self.call(event)
        self.assertEqual(resp["statusCode"], 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(self.table.puts, [])

    def test_firma_invalida_401_y_no_guarda(self):
        resp, body = self.call(signed_event(signature="0" * 64))
        self.assertEqual(resp["statusCode"], 401)
        self.assertEqual(body["error"]["details"]["reason"], "bad_signature")
        self.assertEqual(self.table.puts, [])

    def test_firma_con_otro_secreto_401(self):
        resp, _ = self.call(signed_event(secret="otro"))
        self.assertEqual(resp["statusCode"], 401)

    def test_firma_sobre_timestamp_punto_cuerpo(self):
        # firmar solo el cuerpo (formato anterior) ya no es valido
        raw = json.dumps(valid_alert())
        old_style = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        resp, _ = self.call(signed_event(raw=raw, signature=old_style))
        self.assertEqual(resp["statusCode"], 401)

    def test_timestamp_viejo_401(self):
        resp, body = self.call(signed_event(ts=int(time.time()) - 1000))
        self.assertEqual(resp["statusCode"], 401)
        self.assertEqual(body["error"]["details"]["reason"], "timestamp_out_of_range")

    def test_timestamp_iso_aceptado(self):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        resp, _ = self.call(signed_event(ts=ts))
        self.assertEqual(resp["statusCode"], 201)

    def test_cuerpo_invalido_400_lista_errores(self):
        resp, body = self.call(signed_event(valid_alert(severity="terrible", score=150)))
        self.assertEqual(resp["statusCode"], 400)
        fields = {e["field"] for e in body["error"]["details"]["errors"]}
        self.assertTrue({"severity", "score"} <= fields)

    def test_json_malformado_400(self):
        resp, body = self.call(signed_event(raw="{no es json"))
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_ERROR")

    def test_common_cause_exige_common_factor(self):
        resp, body = self.call(signed_event(valid_alert(type="common_cause", related_machine_ids=["MCH-000124"])))
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("common_factor", {e["field"] for e in body["error"]["details"]["errors"]})

    def test_alerta_valida_201_guarda_y_publica(self):
        resp, body = self.call(signed_event())
        self.assertEqual(resp["statusCode"], 201, resp["body"])
        self.assertEqual(body, {"alert_id": "ALT-000812", "machine_id": "MCH-000123", "status": "created"})
        self.assertEqual(len(self.table.puts), 1)
        stored = self.table.puts[0]
        self.assertEqual(stored["status"], "open")
        self.assertFalse(stored["event_published"])
        self.assertEqual(len(self.events.entries), 1)
        entry = self.events.entries[0]
        self.assertEqual(entry["DetailType"], "alert.created")
        self.assertEqual(entry["EventBusName"], "af-dev-arquifusion-events")
        self.assertEqual(json.loads(entry["Detail"])["alert_id"], "ALT-000812")
        self.assertTrue(self.table.updates)  # event_published = true

    def test_reenvio_identico_es_idempotente(self):
        self.call(signed_event())
        self.table.items[0]["event_published"] = True
        resp, body = self.call(signed_event())
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["status"], "duplicate")
        self.assertEqual(len(self.events.entries), 1)  # no se republica

    def test_si_fallo_el_evento_el_reintento_lo_republica(self):
        self.events.fail = True
        resp, body = self.call(signed_event())
        self.assertEqual(resp["statusCode"], 500)
        self.assertTrue(body["error"]["details"]["retryable"])
        self.assertEqual(len(self.table.puts), 1)  # quedo guardada
        self.events.fail = False
        resp, body = self.call(signed_event())
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["status"], "republished")
        self.assertEqual(len(self.events.entries), 1)
        self.assertEqual(len(self.table.puts), 1)

    def test_mismo_instante_otro_alert_id_es_conflicto(self):
        self.call(signed_event())
        resp, body = self.call(signed_event(valid_alert(alert_id="ALT-000999")))
        self.assertEqual(resp["statusCode"], 409)
        self.assertEqual(body["error"]["code"], "CONFLICT")


# --------------------------------------------------------------------------- autorizador
JWT_SECRET = "jwt-secret"


def token(role="maintenance", **over):
    now = int(time.time())
    claims = {"sub": "maria", "role": role, "exp": now + 600}
    claims.update(over)
    return mint_token.mint(JWT_SECRET, claims)


METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/v1/GET/machines"


class AuthorizerTests(unittest.TestCase):
    def setUp(self):
        self._orig = authorizer.get_secret
        authorizer.get_secret = lambda: JWT_SECRET

    def tearDown(self):
        authorizer.get_secret = self._orig

    def run_auth(self, tok):
        return authorizer.handler({"headers": {"Authorization": "Bearer " + tok} if tok else {}, "methodArn": METHOD_ARN})

    def test_token_valido_devuelve_politica_del_rol(self):
        out = self.run_auth(token("maintenance", zone_id="ZONA-N"))
        stmt = out["policyDocument"]["Statement"][0]
        self.assertEqual(stmt["Effect"], "Allow")
        self.assertIn("arn:aws:execute-api:us-east-1:123456789012:abc123/v1/GET/technicians", stmt["Resource"])
        self.assertIn("arn:aws:execute-api:us-east-1:123456789012:abc123/v1/POST/incidents/*/ack", stmt["Resource"])
        self.assertEqual(out["context"]["role"], "maintenance")
        self.assertEqual(out["context"]["zone_id"], "ZONA-N")

    def test_operador_no_ve_tecnicos_ni_callbacks(self):
        res = self.run_auth(token("operator", machine_ids=["MCH-000001"]))["policyDocument"]["Statement"][0]["Resource"]
        self.assertFalse(any(r.endswith("/GET/technicians") or "/POST/" in r for r in res))
        self.assertTrue(any(r.endswith("/GET/alerts") for r in res))

    def test_machine_ids_pasa_como_cadena(self):
        out = self.run_auth(token("operator", machine_ids=["MCH-000001", "MCH-000002"]))
        self.assertEqual(out["context"]["machine_ids"], "MCH-000001,MCH-000002")

    def test_sin_token_es_unauthorized(self):
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(None)

    def test_token_expirado(self):
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(token(exp=int(time.time()) - 5))

    def test_firma_alterada(self):
        tok = token()
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB"))

    def test_otro_secreto(self):
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(mint_token.mint("otro-secreto", {"sub": "x", "role": "admin", "exp": int(time.time()) + 60}))

    def test_algoritmo_none_rechazado(self):
        b64 = mint_token.b64url
        forged = "%s.%s." % (
            b64(json.dumps({"alg": "none", "typ": "JWT"}).encode()),
            b64(json.dumps({"sub": "x", "role": "admin", "exp": int(time.time()) + 60}).encode()),
        )
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(forged)

    def test_rol_desconocido(self):
        with self.assertRaisesRegex(Exception, "Unauthorized"):
            self.run_auth(token("root"))


# --------------------------------------------------------------------------- callbacks de incidentes
class FakeSfn:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def send_task_success(self, taskToken, output):
        if self.error:
            raise FakeClientError(self.error)
        self.calls.append((taskToken, json.loads(output)))


def incident(**over):
    row = {
        "machine_id": "MCH-000123", "opened_at": "2026-11-14T15:00:00Z", "incident_id": "INC-000045",
        "status": "assigned", "technician_id": "TEC-0007", "zone_id": "ZONA-N",
        "ack_task_token": "tok-ack", "close_task_token": "tok-close",
    }
    row.update(over)
    return row


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable([incident()], ("machine_id", "opened_at"))
        self.sfn = FakeSfn()
        self._saved = (incident_callbacks.get_table, incident_callbacks.get_sfn)
        incident_callbacks.get_table = lambda: self.table
        incident_callbacks.get_sfn = lambda: self.sfn

    def tearDown(self):
        incident_callbacks.get_table, incident_callbacks.get_sfn = self._saved

    def post(self, action, body=None, role="technician", technician_id="TEC-0007", inc="INC-000045"):
        event = api_event(
            "POST", "/incidents/%s/%s" % (inc, action), role=role, technician_id=technician_id,
            body=json.dumps(body) if body is not None else None,
        )
        resp = incident_callbacks.handler(event, None)
        return resp, body_of(resp)

    def test_ack_reanuda_el_flujo_y_no_devuelve_tokens(self):
        resp, body = self.post("ack")
        self.assertEqual(resp["statusCode"], 200, resp["body"])
        self.assertEqual(self.sfn.calls[0][0], "tok-ack")
        self.assertEqual(self.sfn.calls[0][1]["technician_id"], "TEC-0007")
        self.assertIn("acknowledged_at", body)
        self.assertNotIn("tok-ack", resp["body"])
        self.assertNotIn("tok-close", resp["body"])

    def test_ack_repetido_es_idempotente(self):
        self.table.items[0]["acknowledged_at"] = "2026-11-14T15:05:00Z"
        resp, _ = self.post("ack")
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(self.sfn.calls, [])

    def test_ack_sin_token_es_conflicto(self):
        self.table.items[0].pop("ack_task_token")
        resp, body = self.post("ack")
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (409, "CONFLICT"))

    def test_ack_con_ventana_expirada_es_conflicto(self):
        self.sfn.error = "TaskTimedOut"
        resp, body = self.post("ack")
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (409, "CONFLICT"))

    def test_token_ya_consumido_se_tolera(self):
        self.sfn.error = "InvalidToken"
        resp, _ = self.post("ack")
        self.assertEqual(resp["statusCode"], 200)

    def test_tecnico_ajeno_403(self):
        resp, body = self.post("ack", technician_id="TEC-0099")
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (403, "FORBIDDEN"))

    def test_operador_403(self):
        resp, _ = self.post("ack", role="operator")
        self.assertEqual(resp["statusCode"], 403)

    def test_incidente_inexistente_404(self):
        self.table.items = []
        resp, body = self.post("ack")
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (404, "NOT_FOUND"))

    def test_start_solo_desde_assigned(self):
        resp, body = self.post("start")
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["status"], "in_progress")
        self.table.items[0]["status"] = "open"
        resp, body = self.post("start")
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (409, "CONFLICT"))

    def test_close_valida_cuerpo(self):
        resp, body = self.post("close", {"actions": []})
        self.assertEqual((resp["statusCode"], body["error"]["code"]), (400, "VALIDATION_ERROR"))
        resp, _ = self.post("close", {"actions": ["cambio de bomba"], "parts_replaced": [{"part_id": "xx", "qty": 0}]})
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(self.sfn.calls, [])

    def test_close_reanuda_flujo_con_acciones_y_repuestos(self):
        resp, body = self.post("close", {"actions": ["cambio de bomba"], "parts_replaced": [{"part_id": "PRT-0031", "qty": 2}]})
        self.assertEqual(resp["statusCode"], 200, resp["body"])
        token_used, output = self.sfn.calls[0]
        self.assertEqual(token_used, "tok-close")
        self.assertEqual(output["parts_replaced"], [{"part_id": "PRT-0031", "qty": 2}])
        self.assertEqual(body["parts_replaced"], [{"part_id": "PRT-0031", "qty": 2}])
        self.assertNotIn("tok-close", resp["body"])


# --------------------------------------------------------------------------- notificador (tabla 6.9)
class FakeSns:
    def __init__(self):
        self.published = []

    def publish(self, **kw):
        self.published.append(kw)


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable([], ("recipient_role", "sk"))
        self.sns = FakeSns()
        self._saved = (notifier.get_table, notifier._client)
        notifier.get_table = lambda: self.table
        notifier._client = lambda name: self.sns if name == "sns" else FakeSns()
        os.environ.pop("SES_SENDER", None)

    def tearDown(self):
        notifier.get_table, notifier._client = self._saved

    def roles(self, detail_type, **detail):
        return sorted(notifier.plan_deliveries(detail_type, detail))

    def test_alerta_critica_masiva(self):
        plan = dict(notifier.plan_deliveries("alert.created", {"severity": "critical", "type": "common_cause"}))
        self.assertEqual(plan["management"], "immediate")
        self.assertEqual(plan["admin"], "immediate")
        self.assertEqual(plan["maintenance"], "immediate")
        self.assertEqual(plan["operator"], "immediate")

    def test_alerta_critica_no_masiva_va_a_resumen_semanal_de_gerencia(self):
        plan = dict(notifier.plan_deliveries("alert.created", {"severity": "critical", "type": "predictive_failure"}))
        self.assertEqual(plan["management"], "digest:weekly")

    def test_alerta_high(self):
        plan = dict(notifier.plan_deliveries("alert.created", {"severity": "high", "type": "anomaly"}))
        self.assertEqual(plan["maintenance"], "immediate")
        self.assertEqual(plan["supervisor"], "immediate")
        self.assertEqual(plan["admin"], "digest:daily")
        self.assertNotIn("management", plan)

    def test_alerta_medium_va_a_resumenes(self):
        plan = dict(notifier.plan_deliveries("alert.created", {"severity": "medium"}))
        self.assertEqual(plan, {"operator": "immediate", "maintenance": "digest:hourly", "supervisor": "digest:shift"})

    def test_alerta_baja_no_avisa_a_nadie(self):
        self.assertEqual(notifier.plan_deliveries("alert.created", {"severity": "low"}), [])
        self.assertEqual(notifier.plan_deliveries("alert.created", {"severity": "info"}), [])

    def test_incidente_asignado_avisa_al_tecnico(self):
        plan = dict(notifier.plan_deliveries("incident.opened", {"severity": "high", "technician_id": "TEC-0007"}))
        self.assertEqual(plan["technician"], "immediate")
        self.assertEqual(plan["maintenance"], "immediate")

    def test_cierre_con_recurrencia_avisa_a_mantenimiento_ya(self):
        plan = dict(notifier.plan_deliveries("incident.closed", {"recurrence_of": "INC-000010"}))
        self.assertEqual(plan["maintenance"], "immediate")
        plan = dict(notifier.plan_deliveries("incident.closed", {}))
        self.assertEqual(plan["maintenance"], "digest:hourly")

    def event(self, **over):
        alert = valid_alert(**over)
        return {"detail-type": "alert.created", "detail": alert, "time": alert["created_at"]}

    def test_proceso_envia_inmediatos_y_encola_resumenes(self):
        result = notifier.handler(self.event(severity="medium", score=60))
        self.assertEqual({d["role"]: d["result"] for d in result["deliveries"]},
                         {"operator": "sent", "maintenance": "queued", "supervisor": "queued"})
        self.assertEqual(len(self.sns.published), 1)
        queued = [p for p in self.table.puts if p["recipient_role"] == "maintenance"][0]
        self.assertEqual(queued["digest_period"], "hourly")
        self.assertEqual(queued["status"], "queued")

    def test_evento_duplicado_no_reenvia(self):
        event = self.event(severity="high")
        notifier.handler(event)
        for it in self.table.items:  # el envio marco el aviso como enviado
            if it["recipient_role"] in ("operator", "maintenance", "supervisor"):
                it["status"] = "sent"
        sent_before = len(self.sns.published)
        result = notifier.handler(event)
        self.assertEqual(len(self.sns.published), sent_before)
        self.assertTrue(all(d["result"] == "duplicate" for d in result["deliveries"]))

    def test_asunto_sns_va_en_ascii(self):
        notifier.handler(self.event(severity="high", summary="Vibración alta"))
        subject = self.sns.published[0]["Subject"]
        self.assertTrue(subject.isascii())
        self.assertLessEqual(len(subject), 100)

    def test_fallo_de_envio_marca_failed_y_relanza(self):
        def boom(**kw):
            raise FakeClientError("InternalError")

        self.sns.publish = boom
        with self.assertRaises(FakeClientError):
            notifier.handler(self.event(severity="high"))
        self.assertTrue(any(u["ExpressionAttributeValues"].get(":s") == "failed" for u in self.table.updates))

    def test_mensaje_del_tecnico_incluye_evidencia_y_componente(self):
        detail = {"severity": "high", "incident_id": "INC-1", "machine_id": "MCH-1", "zone_id": "ZONA-N",
                  "suspected_component": "cooling_system",
                  "evidence": [{"metric": "engine_temp_c", "observed": 104, "expected": 91, "window": "6h", "trend": "rising"}]}
        _, body = notifier.render("technician", "incident.opened", detail)
        self.assertIn("cooling_system", body)
        self.assertIn("engine_temp_c", body)

    def test_resumen_envia_y_saca_los_avisos_del_indice(self):
        row = {"recipient_role": "maintenance", "sk": "2026-11-14T10:00:00Z#alert.created#ALT-1", "severity": "medium",
               "machine_id": "MCH-1", "title": "[MEDIUM] MCH-1: anomalo", "digest_period": "hourly"}
        self.table.items = [row]
        result = notifier.handler({"mode": "digest", "period": "hourly"})
        self.assertEqual(result["sent"], {"maintenance": 1})
        self.assertEqual(len(self.sns.published), 1)
        self.assertIn("REMOVE digest_period", self.table.updates[0]["UpdateExpression"])

    def test_resumen_con_periodo_invalido(self):
        with self.assertRaises(ValueError):
            notifier.handler({"mode": "digest", "period": "cada-minuto"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
