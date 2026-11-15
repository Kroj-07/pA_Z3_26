"""Pruebas offline del generador de carga y del caos. python -m unittest discover -s load/test/runner -v"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import load.runner.chaos_sqs as chaos_sqs  # noqa: E402
import load.runner.load_test as load_test  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Handler.seen.append((json.loads(body), dict(self.headers)))
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class LoadTest(unittest.TestCase):
    def test_payload_matches_contract(self):
        import random
        p = load_test.make_payload(123, 0, random.Random(1))
        self.assertEqual(set(p), {"schema_version", "machine_id", "timestamp", "location", "status", "metrics"})
        self.assertEqual(set(p["location"]), {"lat", "lon", "zone_id"})
        self.assertIn(p["status"], {"operating", "idle", "stopped", "maintenance", "fault"})
        self.assertEqual(set(p["metrics"]), {"engine_temp_c", "vibration_mm_s", "pressure_bar", "fuel_rate_lph",
                                             "load_kg", "speed_kmh", "engine_rpm", "operating_hours_total"})

    def test_batch_groups_readings(self):
        rep = load_test.run_load(100, 10, 10, 5, None, None, 1, batch=10)
        self.assertEqual(rep["readings_planned"], 100)
        self.assertEqual(rep["requests_planned"], 10)

    def test_schedule_size_for_profiles(self):
        for n in load_test.PROFILES:
            self.assertEqual(len(load_test.build_schedule(n, 10, 10, 1)), n)

    def test_dry_run_signs_without_network(self):
        rep = load_test.run_load(100, 20, 10, 5, None, b"secret", 1)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["requests_planned"], 200)
        self.assertEqual(rep["requests_sent"], 0)
        self.assertTrue(load_test.evaluate(rep, 0.01, 1000)["passed"])

    def test_signature_is_verifiable(self):
        sig = load_test.sign(b"k", "123", b"body")
        self.assertEqual(sig, load_test.sign(b"k", "123", b"body"))
        self.assertEqual(len(sig), 64)

    def test_real_send_against_local_server(self):
        Handler.seen = []
        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/v1/telemetry" % srv.server_port
        rep = load_test.run_load(20, 1, 1, 8, load_test.http_sender(url, 5), b"secret", 7)
        srv.shutdown()
        self.assertEqual(rep["requests_sent"], 20)
        self.assertEqual(rep["requests_ok"], 20)
        self.assertEqual(rep["error_rate"], 0.0)
        body, headers = Handler.seen[0]
        self.assertRegex(body["machine_id"], r"^MCH-\d{6}$")
        self.assertEqual(body["schema_version"], "1.0")
        self.assertRegex(body["timestamp"], r"Z$")
        self.assertIn(load_test.SIGNATURE_HEADER.lower(), {k.lower() for k in headers})

    def test_unreachable_server_counts_errors(self):
        rep = load_test.run_load(5, 1, 1, 2, load_test.http_sender("http://127.0.0.1:9/x", 1), None, 1)
        self.assertEqual(rep["requests_ok"], 0)
        self.assertFalse(load_test.evaluate(rep, 0.01, 1000)["passed"])


class FakeLambda:
    def __init__(self):
        self.state, self.calls = "Enabled", []

    def update_event_source_mapping(self, UUID, Enabled):
        self.calls.append(Enabled)
        self.state = "Enabled" if Enabled else "Disabled"

    def get_event_source_mapping(self, UUID):
        return {"State": self.state}


class FakeSqs:
    def __init__(self, lam):
        self.lam, self.depth = lam, 0

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        if self.lam.state == "Disabled":
            self.depth += 50
        else:
            self.depth = max(0, self.depth - 200)
        return {"Attributes": {"ApproximateNumberOfMessages": str(self.depth),
                               "ApproximateNumberOfMessagesNotVisible": "0",
                               "ApproximateAgeOfOldestMessage": str(self.depth // 10)}}


class Chaos(unittest.TestCase):
    def test_outage_then_recovery(self):
        lam = FakeLambda()
        clock = {"t": 0.0}
        import time as _t
        real = _t.monotonic
        _t.monotonic = lambda: clock["t"]
        try:
            rep = chaos_sqs.run_chaos(lam, FakeSqs(lam), "uuid", "q", 60, 5, 300,
                                      sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
        finally:
            _t.monotonic = real
        self.assertEqual(lam.calls, [False, True])
        self.assertGreater(rep["during_outage"]["peak_depth"], 0)
        self.assertTrue(rep["passed"])

    def test_reenables_even_if_sampling_fails(self):
        lam = FakeLambda()

        class Boom(FakeSqs):
            n = 0

            def get_queue_attributes(self, **kw):
                Boom.n += 1
                if Boom.n > 1:
                    raise RuntimeError("fallo sqs")
                return super().get_queue_attributes(**kw)

        with self.assertRaises(RuntimeError):
            chaos_sqs.run_chaos(lam, Boom(lam), "u", "q", 60, 1, 10, sleep=lambda s: None)
        self.assertEqual(lam.calls[-1], True)

    def test_dry_run_touches_nothing(self):
        self.assertEqual(chaos_sqs.main(["--dry-run", "--function-name", "f", "--queue-url", "q"]), 0)


if __name__ == "__main__":
    unittest.main()
