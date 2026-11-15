"""Pruebas de la Lambda de ingesta. Desde part-a/ingest:  python -m unittest -v

DynamoDB y S3 se simulan con objetos falsos (sin red, sin credenciales). La prueba
real de Parquet solo corre si pyarrow está instalado.
"""

import copy
import io
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from botocore.exceptions import ClientError

import app

FIXED_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)

VALID = {
    "schema_version": "1.0",
    "machine_id": "MCH-000123",
    "timestamp": "2026-11-14T15:04:05Z",
    "location": {"lat": -12.0464, "lon": -77.0428, "zone_id": "ZONA-NORTE-02"},
    "status": "operating",
    "metrics": {
        "engine_temp_c": 92.4,
        "vibration_mm_s": 4.8,
        "pressure_bar": 6.1,
        "fuel_rate_lph": 31.2,
        "load_kg": 12400,
        "speed_kmh": 18.5,
        "engine_rpm": 1850,
        "operating_hours_total": 3240.7,
    },
}


def make_reading(**overrides):
    """Lectura válida con cambios. Claves con punto ("metrics.speed_kmh") entran en el anidado."""
    reading = copy.deepcopy(VALID)
    for path, value in overrides.items():
        target = reading
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        if value is DELETE:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = value
    return reading


DELETE = object()


def client_error(code, operation, status=400):
    return ClientError(
        {"Error": {"Code": code, "Message": code},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


def sqs_record(message_id, body):
    return {"messageId": message_id, "body": body if isinstance(body, str) else json.dumps(body)}


class FakeTelemetryTable:
    """Imita la condición del put: se acepta si no existe o si es del mismo mensaje."""

    def __init__(self):
        self.items = {}
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        item = kwargs["Item"]
        key = (item["machine_id"], item["timestamp"])
        existing = self.items.get(key)
        if existing and existing["ingest_message_id"] != kwargs["ExpressionAttributeValues"][":mid"]:
            raise client_error("ConditionalCheckFailedException", "PutItem")
        self.items[key] = item


class FakeMachinesTable:
    """Imita la condición "solo si last_seen es menor"."""

    def __init__(self, last_seen=None):
        self.state = dict(last_seen or {})
        self.calls = []

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        machine_id = kwargs["Key"]["machine_id"]
        ts = kwargs["ExpressionAttributeValues"][":ts"]
        if machine_id in self.state and self.state[machine_id] >= ts:
            raise client_error("ConditionalCheckFailedException", "UpdateItem")
        self.state[machine_id] = ts


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_times = 0

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise client_error("InternalError", "PutObject", 500)
        key = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise client_error("PreconditionFailed", "PutObject", 412)
        self.objects[key] = kwargs["Body"]


def make_deps(machines=None):
    return app.Deps(
        telemetry_table=FakeTelemetryTable(),
        machines_table=machines or FakeMachinesTable(),
        s3=FakeS3(),
        bucket="af-test-data-123456789012",
        ttl_days=30,
        now=lambda: FIXED_NOW,
        to_parquet=lambda rows: json.dumps([r["machine_id"] for r in rows]).encode(),
    )


class ValidationTest(unittest.TestCase):
    def assertInvalid(self, reading, field_name):
        with self.assertRaises(app.ValidationError) as ctx:
            app.validate_reading(reading)
        self.assertEqual(ctx.exception.field_name, field_name)
        return ctx.exception

    def test_lectura_valida(self):
        result = app.validate_reading(make_reading())
        self.assertEqual(result["machine_id"], "MCH-000123")
        self.assertEqual(result["location"]["zone_id"], "ZONA-NORTE-02")
        self.assertEqual(set(result["metrics"]), set(app.METRIC_FIELDS))

    def test_falta_machine_id(self):
        self.assertInvalid(make_reading(machine_id=DELETE), "machine_id")

    def test_machine_id_con_formato_malo(self):
        self.assertInvalid(make_reading(machine_id="mch-123"), "machine_id")
        self.assertInvalid(make_reading(machine_id=123), "machine_id")

    def test_schema_version(self):
        self.assertInvalid(make_reading(schema_version="2.0"), "schema_version")
        self.assertInvalid(make_reading(schema_version=DELETE), "schema_version")
        app.validate_reading(make_reading(schema_version="1.3"))

    def test_timestamp_formatos_no_validos(self):
        self.assertInvalid(make_reading(timestamp="2026-11-14T15:04:05"), "timestamp")
        self.assertInvalid(make_reading(timestamp="2026-11-14T15:04:05.123Z"), "timestamp")
        self.assertInvalid(make_reading(timestamp="2026-13-40T15:04:05Z"), "timestamp")
        self.assertInvalid(make_reading(timestamp=1731596645), "timestamp")

    def test_status_fuera_del_enum(self):
        self.assertInvalid(make_reading(status="moving"), "status")
        self.assertInvalid(make_reading(status=["operating"]), "status")

    def test_todos_los_status_del_contrato(self):
        for status in ("operating", "idle", "stopped", "maintenance", "fault"):
            app.validate_reading(make_reading(status=status))

    def test_location_invalida(self):
        self.assertInvalid(make_reading(location="Lima"), "location")
        self.assertInvalid(make_reading(**{"location.lat": 91}), "location.lat")
        self.assertInvalid(make_reading(**{"location.lon": -181}), "location.lon")
        self.assertInvalid(make_reading(**{"location.lat": "-12.0"}), "location.lat")
        self.assertInvalid(make_reading(**{"location.zone_id": "norte"}), "location.zone_id")
        self.assertInvalid(make_reading(**{"location.zone_id": DELETE}), "location.zone_id")

    def test_metrica_no_numerica(self):
        exc = self.assertInvalid(make_reading(**{"metrics.engine_temp_c": "92.4"}),
                                 "metrics.engine_temp_c")
        self.assertIn("numérico", str(exc))
        self.assertEqual(exc.machine_id, "MCH-000123")

    def test_metrica_booleana_o_no_finita(self):
        self.assertInvalid(make_reading(**{"metrics.speed_kmh": True}), "metrics.speed_kmh")
        self.assertInvalid(make_reading(**{"metrics.speed_kmh": float("nan")}), "metrics.speed_kmh")
        self.assertInvalid(make_reading(**{"metrics.speed_kmh": float("inf")}), "metrics.speed_kmh")

    def test_metrica_negativa_donde_no_corresponde(self):
        self.assertInvalid(make_reading(**{"metrics.speed_kmh": -1}), "metrics.speed_kmh")
        self.assertInvalid(make_reading(**{"metrics.load_kg": -5}), "metrics.load_kg")
        # la temperatura sí puede ser negativa
        app.validate_reading(make_reading(**{"metrics.engine_temp_c": -10.5}))

    def test_faltantes_se_normalizan_a_null(self):
        result = app.validate_reading(make_reading(**{
            "metrics.pressure_bar": None, "metrics.engine_rpm": DELETE}))
        self.assertIsNone(result["metrics"]["pressure_bar"])
        self.assertIsNone(result["metrics"]["engine_rpm"])

    def test_cero_es_un_valor_valido_no_un_faltante(self):
        result = app.validate_reading(make_reading(**{"metrics.speed_kmh": 0}))
        self.assertEqual(result["metrics"]["speed_kmh"], 0)

    def test_lat_lon_pueden_ser_null(self):
        result = app.validate_reading(make_reading(**{"location.lat": None, "location.lon": None}))
        self.assertIsNone(result["location"]["lat"])

    def test_campos_desconocidos_se_ignoran(self):
        reading = make_reading()
        reading["campo_nuevo"] = "x"
        reading["metrics"]["metrica_nueva"] = 1
        result = app.validate_reading(reading)
        self.assertNotIn("campo_nuevo", result)
        self.assertNotIn("metrica_nueva", result["metrics"])

    def test_metrics_no_es_objeto(self):
        self.assertInvalid(make_reading(metrics=[1, 2]), "metrics")

    def test_lectura_no_es_objeto(self):
        with self.assertRaises(app.ValidationError):
            app.validate_reading([1, 2, 3])

    def test_formato_de_error_del_contrato(self):
        exc = self.assertInvalid(make_reading(**{"metrics.engine_temp_c": "x"}),
                                 "metrics.engine_temp_c")
        error = app.build_error(exc, "req-123")["error"]
        self.assertEqual(error["code"], "VALIDATION_ERROR")
        self.assertEqual(error["details"], {"machine_id": "MCH-000123",
                                            "field": "metrics.engine_temp_c"})
        self.assertEqual(error["request_id"], "req-123")


class ExtractReadingsTest(unittest.TestCase):
    def test_lectura_suelta(self):
        self.assertEqual(len(app.extract_readings(json.dumps(VALID))), 1)

    def test_lote(self):
        body = json.dumps({"readings": [VALID, VALID]})
        self.assertEqual(len(app.extract_readings(body)), 2)

    def test_json_invalido(self):
        with self.assertRaises(app.ValidationError):
            app.extract_readings("{no es json")

    def test_cuerpo_vacio_o_ausente(self):
        for body in ("", "   ", None):
            with self.assertRaises(app.ValidationError):
                app.extract_readings(body)

    def test_readings_vacio_o_no_lista(self):
        for body in ({"readings": []}, {"readings": "x"}):
            with self.assertRaises(app.ValidationError):
                app.extract_readings(json.dumps(body))

    def test_lote_demasiado_grande(self):
        body = json.dumps({"readings": [VALID] * (app.MAX_READINGS_PER_MESSAGE + 1)})
        with self.assertRaises(app.ValidationError):
            app.extract_readings(body)

    def test_payload_que_no_es_objeto(self):
        with self.assertRaises(app.ValidationError):
            app.extract_readings("[1, 2]")


class PersistenceTest(unittest.TestCase):
    def test_guarda_en_dynamodb_con_condicion_idempotente(self):
        deps = make_deps()
        summary = app.process_message(sqs_record("m1", VALID), deps, "req-1")
        self.assertEqual(summary["accepted"], 1)
        call = deps.telemetry_table.calls[0]
        self.assertIn("attribute_not_exists", call["ConditionExpression"])
        self.assertEqual(call["ExpressionAttributeValues"][":mid"], "m1")
        item = deps.telemetry_table.items[("MCH-000123", "2026-11-14T15:04:05Z")]
        self.assertEqual(item["zone_id"], "ZONA-NORTE-02")

    def test_floats_se_guardan_como_decimal(self):
        deps = make_deps()
        app.process_message(sqs_record("m1", VALID), deps)
        item = next(iter(deps.telemetry_table.items.values()))
        self.assertIsInstance(item["metrics"]["engine_temp_c"], Decimal)
        self.assertEqual(item["metrics"]["engine_temp_c"], Decimal("92.4"))
        self.assertEqual(item["metrics"]["engine_rpm"], 1850)
        self.assertIsInstance(item["location"]["lat"], Decimal)

    def test_ttl_es_ingesta_mas_30_dias(self):
        deps = make_deps()
        app.process_message(sqs_record("m1", VALID), deps)
        item = next(iter(deps.telemetry_table.items.values()))
        self.assertEqual(item["ttl"], int(FIXED_NOW.timestamp()) + 30 * 86400)
        self.assertEqual(item["ingested_at"], "2026-11-15T12:00:00Z")

    def test_guarda_en_s3_particionado_y_condicional(self):
        deps = make_deps()
        app.process_message(sqs_record("m1", VALID), deps)
        self.assertEqual(len(deps.s3.calls), 1)
        call = deps.s3.calls[0]
        self.assertEqual(call["Bucket"], "af-test-data-123456789012")
        self.assertEqual(
            call["Key"],
            "telemetry/year=2026/month=11/day=14/zone=ZONA-NORTE-02/m1.parquet")
        self.assertEqual(call["IfNoneMatch"], "*")

    def test_lote_en_varias_particiones(self):
        deps = make_deps()
        batch = {"readings": [
            make_reading(),
            make_reading(machine_id="MCH-000124"),  # misma partición, mismo archivo
            make_reading(**{"location.zone_id": "ZONA-SUR-01", "machine_id": "MCH-000125"}),
            make_reading(timestamp="2026-11-15T01:00:00Z", machine_id="MCH-000126"),
        ]}
        summary = app.process_message(sqs_record("m1", batch), deps)
        self.assertEqual(summary["accepted"], 4)
        keys = {key for (_, key) in deps.s3.objects}
        self.assertEqual(keys, {
            "telemetry/year=2026/month=11/day=14/zone=ZONA-NORTE-02/m1.parquet",
            "telemetry/year=2026/month=11/day=14/zone=ZONA-SUR-01/m1.parquet",
            "telemetry/year=2026/month=11/day=15/zone=ZONA-NORTE-02/m1.parquet",
        })

    def test_actualiza_estado_de_la_maquina(self):
        deps = make_deps()
        app.process_message(sqs_record("m1", VALID), deps)
        self.assertEqual(len(deps.machines_table.calls), 1)
        call = deps.machines_table.calls[0]
        self.assertEqual(call["Key"], {"machine_id": "MCH-000123"})
        self.assertEqual(call["ExpressionAttributeValues"][":ts"], "2026-11-14T15:04:05Z")
        self.assertEqual(call["ExpressionAttributeNames"]["#st"], "status")

    def test_estado_usa_la_lectura_mas_reciente_de_cada_maquina(self):
        deps = make_deps()
        batch = {"readings": [
            make_reading(timestamp="2026-11-14T15:05:05Z"),
            make_reading(timestamp="2026-11-14T15:04:05Z"),
            make_reading(machine_id="MCH-000124"),
        ]}
        app.process_message(sqs_record("m1", batch), deps)
        by_machine = {c["Key"]["machine_id"]: c["ExpressionAttributeValues"][":ts"]
                      for c in deps.machines_table.calls}
        self.assertEqual(by_machine, {"MCH-000123": "2026-11-14T15:05:05Z",
                                      "MCH-000124": "2026-11-14T15:04:05Z"})

    def test_lectura_vieja_no_retrocede_el_estado_ni_falla(self):
        machines = FakeMachinesTable(last_seen={"MCH-000123": "2026-11-14T16:00:00Z"})
        deps = make_deps(machines)
        summary = app.process_message(sqs_record("m1", VALID), deps)
        self.assertEqual(summary["accepted"], 1)  # la telemetría sí se guarda
        self.assertEqual(machines.state["MCH-000123"], "2026-11-14T16:00:00Z")

    def test_horas_de_operacion_nulas_no_pisan_el_valor_guardado(self):
        deps = make_deps()
        app.process_message(
            sqs_record("m1", make_reading(**{"metrics.operating_hours_total": None})), deps)
        update = deps.machines_table.calls[0]["UpdateExpression"]
        self.assertNotIn("operating_hours_total", update)


class IdempotencyTest(unittest.TestCase):
    def test_reenvio_de_otro_mensaje_no_duplica(self):
        deps = make_deps()
        first = app.process_message(sqs_record("m1", VALID), deps)
        second = app.process_message(sqs_record("m2", VALID), deps)
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(deps.telemetry_table.items), 1)
        self.assertEqual(len(deps.s3.objects), 1)  # no se escribió otro archivo
        self.assertEqual(len(deps.machines_table.calls), 1)  # ni se tocó el estado

    def test_reintento_del_mismo_mensaje_es_seguro(self):
        deps = make_deps()
        app.process_message(sqs_record("m1", VALID), deps)
        again = app.process_message(sqs_record("m1", VALID), deps)
        self.assertEqual(again["accepted"], 1)  # sigue siendo "suyo"
        self.assertEqual(len(deps.telemetry_table.items), 1)
        self.assertEqual(len(deps.s3.objects), 1)  # If-None-Match evita reescribir

    def test_duplicada_dentro_del_mismo_lote(self):
        deps = make_deps()
        batch = {"readings": [make_reading(), make_reading(), make_reading(status="idle")]}
        summary = app.process_message(sqs_record("m1", batch), deps)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["duplicates"], 2)
        self.assertEqual(len(deps.telemetry_table.calls), 1)

    def test_misma_maquina_distinto_timestamp_no_es_duplicado(self):
        deps = make_deps()
        batch = {"readings": [make_reading(), make_reading(timestamp="2026-11-14T15:04:06Z")]}
        summary = app.process_message(sqs_record("m1", batch), deps)
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual(len(deps.telemetry_table.items), 2)

    def test_fallo_de_s3_no_deja_lecturas_sin_copia_al_reintentar(self):
        deps = make_deps()
        deps.s3.fail_times = 1
        with self.assertRaises(ClientError):
            app.process_message(sqs_record("m1", VALID), deps)
        # DynamoDB ya tiene la lectura, S3 no. SQS reintenta el mismo mensaje:
        self.assertEqual(len(deps.s3.objects), 0)
        retry = app.process_message(sqs_record("m1", VALID), deps)
        self.assertEqual(retry["accepted"], 1)
        self.assertEqual(len(deps.s3.objects), 1)  # la copia en S3 sí quedó
        self.assertEqual(len(deps.telemetry_table.items), 1)


class RejectionTest(unittest.TestCase):
    def test_lectura_invalida_no_detiene_el_lote(self):
        deps = make_deps()
        batch = {"readings": [
            make_reading(),
            make_reading(machine_id="MCH-000124", **{"metrics.engine_temp_c": "caliente"}),
            make_reading(machine_id="MCH-000125"),
        ]}
        with self.assertLogs(level="WARNING") as logs:
            summary = app.process_message(sqs_record("m1", batch), deps, "req-9")
        self.assertEqual(summary["accepted"], 2)
        self.assertEqual(summary["rejected"], 1)
        logged = json.loads(logs.records[0].getMessage())
        self.assertEqual(logged["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(logged["error"]["details"]["machine_id"], "MCH-000124")
        self.assertEqual(logged["error"]["request_id"], "req-9")

    def test_mensaje_que_no_es_json_se_rechaza_sin_escribir(self):
        deps = make_deps()
        with self.assertLogs(level="WARNING"):
            summary = app.process_message(sqs_record("m1", "{roto"), deps)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(deps.telemetry_table.calls, [])
        self.assertEqual(deps.s3.calls, [])

    def test_todo_invalido_no_toca_dynamodb_ni_s3(self):
        deps = make_deps()
        with self.assertLogs(level="WARNING"):
            app.process_message(sqs_record("m1", make_reading(status="moving")), deps)
        self.assertEqual(deps.telemetry_table.calls, [])
        self.assertEqual(deps.s3.calls, [])


class HandlerTest(unittest.TestCase):
    def run_handler(self, deps, records):
        with mock.patch.object(app, "get_deps", return_value=deps):
            return app.lambda_handler({"Records": records}, mock.Mock(aws_request_id="req-1"))

    def test_todo_bien_no_devuelve_fallos(self):
        deps = make_deps()
        result = self.run_handler(deps, [sqs_record("m1", VALID),
                                         sqs_record("m2", make_reading(machine_id="MCH-000124"))])
        self.assertEqual(result, {"batchItemFailures": []})

    def test_respuesta_parcial_solo_falla_el_mensaje_con_error(self):
        deps = make_deps()
        deps.s3.fail_times = 1  # el primer put a S3 falla
        records = [sqs_record("m1", VALID),
                   sqs_record("m2", make_reading(machine_id="MCH-000124"))]
        with self.assertLogs(level="ERROR"):
            result = self.run_handler(deps, records)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})
        self.assertIn(("af-test-data-123456789012",
                       "telemetry/year=2026/month=11/day=14/zone=ZONA-NORTE-02/m2.parquet"),
                      deps.s3.objects)

    def test_mensajes_invalidos_no_se_reintentan(self):
        deps = make_deps()
        with self.assertLogs(level="WARNING"):
            result = self.run_handler(deps, [sqs_record("m1", "{roto"),
                                             sqs_record("m2", make_reading(status="x"))])
        self.assertEqual(result, {"batchItemFailures": []})

    def test_falla_de_dynamodb_se_reporta_para_reintento(self):
        deps = make_deps()
        deps.telemetry_table.put_item = mock.Mock(
            side_effect=client_error("ProvisionedThroughputExceededException", "PutItem"))
        with self.assertLogs(level="ERROR"):
            result = self.run_handler(deps, [sqs_record("m1", VALID)])
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "m1"}])
        self.assertEqual(deps.s3.calls, [])  # no se escribió nada a medias en S3

    def test_evento_sin_registros(self):
        self.assertEqual(self.run_handler(make_deps(), []), {"batchItemFailures": []})


class DepsTest(unittest.TestCase):
    def test_faltan_variables_de_entorno(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(app, "_DEPS", None):
            with self.assertRaises(RuntimeError) as ctx:
                app.get_deps()
        self.assertIn("TELEMETRY_TABLE", str(ctx.exception))
        self.assertIn("TELEMETRY_BUCKET", str(ctx.exception))


try:
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover
    pq = None


@unittest.skipIf(pq is None, "pyarrow no está instalado")
class ParquetTest(unittest.TestCase):
    def test_parquet_real(self):
        rows = [
            dict(app.validate_reading(make_reading()), ingested_at="2026-11-15T12:00:00Z"),
            dict(app.validate_reading(make_reading(
                machine_id="MCH-000124", **{"metrics.pressure_bar": None})),
                ingested_at="2026-11-15T12:00:00Z"),
        ]
        table = pq.read_table(io.BytesIO(app.rows_to_parquet(rows)))
        self.assertEqual(table.num_rows, 2)
        self.assertEqual(table.column("machine_id").to_pylist(), ["MCH-000123", "MCH-000124"])
        self.assertEqual(str(table.schema.field("timestamp").type), "timestamp[us, tz=UTC]")
        self.assertEqual(table.column("engine_temp_c").to_pylist()[0], 92.4)
        self.assertIsNone(table.column("pressure_bar").to_pylist()[1])
        for name in app.METRIC_FIELDS:
            self.assertIn(name, table.column_names)


if __name__ == "__main__":
    unittest.main()
