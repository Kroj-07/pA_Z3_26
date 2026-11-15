#!/usr/bin/env python3
"""Generador de carga de ArquiFusion: simula 100, 1.000 o 5.000 maquinas enviando telemetria.

Solo biblioteca estandar (urllib + threading). Ejemplos:

  # prueba de humo sin red: genera y firma los payloads, no envia nada
  python load_test.py --profile 100 --dry-run

  # 1.000 maquinas durante 60 s contra la ingesta real
  export AF_INGEST_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com/v1/telemetry
  export AF_HMAC_SECRET=...      # NUNCA en el repo: sale de Secrets Manager
  python load_test.py --profile 1000 --duration 60 --report report-1000.json

  # los tres perfiles seguidos
  python load_test.py --profile all --duration 60

CONTRATO v1.3 (seccion 2): cuerpo con schema_version, machine_id (MCH-000123), timestamp (UTC con Z),
location{lat,lon,zone_id}, status y metrics; POST /v1/telemetry acepta una lectura o {"readings":[...]}.
AUTENTICACION: el contrato no fija la de /v1/telemetry. Opcional y configurable (confirmar con IA-2):
  - AF_JWT: se envia como Authorization: Bearer <jwt> (se puede generar con part-a/api/mint_token.py).
  - AF_HMAC_SECRET: firma HMAC-SHA256 de "{x-timestamp}.{cuerpo}" en x-timestamp / x-signature
    (el esquema que el contrato define para POST /v1/alerts; el secreto real vive en Secrets Manager).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

PROFILES = (100, 1000, 5000)
ZONES = ("ZONA-NORTE-01", "ZONA-NORTE-02", "ZONA-SUR-01")
SCHEMA_VERSION = "1.0"
SIGNATURE_HEADER = "x-signature"
TIMESTAMP_HEADER = "x-timestamp"
STATUSES = ("operating", "operating", "operating", "idle", "stopped", "maintenance", "fault")

Sender = Callable[[bytes, Dict[str, str]], Tuple[int, float, Optional[str]]]


def make_payload(machine_idx: int, seq: int, rng: random.Random) -> Dict[str, Any]:
    """Lectura segun contrato v1.3 seccion 2. `seq` no viaja (el contrato no lo define)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "machine_id": "MCH-%06d" % machine_idx,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location": {
            "lat": round(-12.0464 + rng.uniform(-0.2, 0.2), 5),
            "lon": round(-77.0428 + rng.uniform(-0.2, 0.2), 5),
            "zone_id": ZONES[machine_idx % len(ZONES)],
        },
        "status": rng.choice(STATUSES),
        "metrics": {
            "engine_temp_c": round(rng.uniform(70, 110), 1),
            "vibration_mm_s": round(rng.uniform(1, 9), 1),
            "pressure_bar": round(rng.uniform(4, 8), 1),
            "fuel_rate_lph": round(rng.uniform(10, 45), 1),
            "load_kg": rng.randint(0, 20000),
            "speed_kmh": round(rng.uniform(0, 30), 1),
            "engine_rpm": rng.randint(700, 2400),
            "operating_hours_total": round(rng.uniform(100, 9000), 1),
        },
    }


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def build_schedule(machines: int, duration: float, interval: float, seed: int) -> List[Tuple[float, int, int]]:
    """(segundo_programado, maquina, secuencia) ordenado. Cada maquina tiene un desfase propio."""
    rng = random.Random(seed)
    per_machine = max(1, int(duration // interval))
    out = []
    for m in range(machines):
        offset = rng.uniform(0, interval)
        for s in range(per_machine):
            out.append((offset + s * interval, m, s))
    out.sort()
    return out


def percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)
    return sorted_vals[k]


def http_sender(url: str, timeout: float) -> Sender:
    def send(body: bytes, headers: Dict[str, str]) -> Tuple[int, float, Optional[str]]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
                return resp.status, (time.perf_counter() - t0) * 1000, None
        except urllib.error.HTTPError as exc:
            return exc.code, (time.perf_counter() - t0) * 1000, None
        except Exception as exc:  # timeout, DNS, conexion
            return 0, (time.perf_counter() - t0) * 1000, type(exc).__name__
    return send


def run_load(
    machines: int,
    duration: float,
    interval: float,
    concurrency: int,
    sender: Optional[Sender],
    secret: Optional[bytes] = None,
    seed: int = 42,
    batch: int = 1,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Ejecuta una prueba. sender=None = dry-run (genera y firma, no envia)."""
    schedule = build_schedule(machines, duration, interval, seed)
    rng = random.Random(seed)
    q: "queue.Queue[Optional[Tuple[float, bytes, Dict[str, str]]]]" = queue.Queue()
    lock = threading.Lock()
    latencies: List[float] = []
    lags: List[float] = []
    status: Dict[str, int] = {}
    errors: Dict[str, int] = {}
    bytes_total = 0

    def worker() -> None:
        while True:
            item = q.get()
            if item is None:
                return
            due, body, headers = item
            lag = (time.perf_counter() - due) * 1000
            code, ms, err = sender(body, headers)  # type: ignore[misc]
            with lock:
                lags.append(max(0.0, lag))
                latencies.append(ms)
                status[str(code)] = status.get(str(code), 0) + 1
                if err:
                    errors[err] = errors.get(err, 0) + 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)] if sender else []
    for t in threads:
        t.start()

    start = time.perf_counter()
    requests_planned = 0
    for i in range(0, len(schedule), batch):
        chunk = schedule[i:i + batch]
        readings = [make_payload(m, sq, rng) for _, m, sq in chunk]
        doc = readings[0] if batch == 1 else {"readings": readings}
        body = json.dumps(doc, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if secret:
            ts = str(int(time.time()))
            headers[TIMESTAMP_HEADER] = ts
            headers[SIGNATURE_HEADER] = sign(secret, ts, body)
        bytes_total += len(body)
        requests_planned += 1
        at = chunk[0][0]
        if sender:
            delay = start + at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            q.put((start + at, body, headers))
    for _ in threads:
        q.put(None)
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    sent = len(latencies)
    ok = sum(n for code, n in status.items() if code.startswith("2"))
    lat = sorted(latencies)
    lag_sorted = sorted(lags)
    return {
        "machines": machines,
        "dry_run": sender is None,
        "duration_s": round(duration, 1),
        "interval_s": interval,
        "concurrency": concurrency,
        "readings_planned": len(schedule),
        "batch_size": batch,
        "requests_planned": requests_planned,
        "requests_sent": sent,
        "requests_ok": ok,
        "error_rate": round((sent - ok) / sent, 4) if sent else None,
        "status_counts": status,
        "client_errors": errors,
        "payload_bytes_total": bytes_total,
        "elapsed_s": round(elapsed, 2),
        "throughput_rps": round(sent / elapsed, 1) if sent and elapsed else None,
        "latency_ms": {
            "min": round(lat[0], 1), "p50": round(percentile(lat, 50), 1),
            "p95": round(percentile(lat, 95), 1), "p99": round(percentile(lat, 99), 1),
            "max": round(lat[-1], 1), "mean": round(sum(lat) / len(lat), 1),
        } if lat else None,
        "dispatch_lag_ms_p95": round(percentile(lag_sorted, 95), 1) if lag_sorted else None,
    }


def evaluate(report: Dict[str, Any], max_error_rate: float, max_p95_ms: float) -> Dict[str, Any]:
    problems = []
    if report["dry_run"]:
        return {"passed": True, "problems": [], "note": "dry-run: sin envio, no se evaluan umbrales"}
    if report["requests_sent"] == 0:
        problems.append("no se envio ninguna peticion")
    if report["error_rate"] is not None and report["error_rate"] > max_error_rate:
        problems.append("error_rate %.4f > %.4f" % (report["error_rate"], max_error_rate))
    if report["latency_ms"] and report["latency_ms"]["p95"] > max_p95_ms:
        problems.append("p95 %.0f ms > %.0f ms" % (report["latency_ms"]["p95"], max_p95_ms))
    if report["dispatch_lag_ms_p95"] and report["dispatch_lag_ms_p95"] > 500:
        problems.append("el cliente se saturo (lag p95 %.0f ms): sube --concurrency o reparte la carga entre maquinas" % report["dispatch_lag_ms_p95"])
    return {"passed": not problems, "problems": problems}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="100", help="100, 1000, 5000 o all")
    ap.add_argument("--machines", type=int, help="numero libre de maquinas (reemplaza --profile)")
    ap.add_argument("--url", default=os.environ.get("AF_INGEST_URL"), help="endpoint de ingesta (o AF_INGEST_URL)")
    ap.add_argument("--duration", type=float, default=60, help="segundos de prueba por perfil")
    ap.add_argument("--interval", type=float, default=10, help="segundos entre mensajes de una misma maquina")
    ap.add_argument("--concurrency", type=int, default=0, help="hilos (0 = automatico, max 300)")
    ap.add_argument("--batch", type=int, default=1, help="lecturas por peticion (1 = una lectura; >1 usa {\"readings\": [...]})")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--max-p95-ms", type=float, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="no envia nada")
    ap.add_argument("--report", help="archivo JSON de salida")
    args = ap.parse_args(argv)

    if args.machines:
        sizes = [args.machines]
    elif args.profile == "all":
        sizes = list(PROFILES)
    else:
        sizes = [int(args.profile)]
    if not args.dry_run and not args.url:
        ap.error("falta --url (o AF_INGEST_URL), o usa --dry-run")
    secret_env = os.environ.get("AF_HMAC_SECRET")
    secret = secret_env.encode() if secret_env else None
    token = os.environ.get("AF_JWT")
    if not args.dry_run and not secret and not token:
        print("AVISO: ni AF_JWT ni AF_HMAC_SECRET definidos: las peticiones van sin autenticacion.", file=sys.stderr)

    reports = []
    ok_all = True
    for n in sizes:
        conc = args.concurrency or min(300, max(10, n // 10))
        sender = None if args.dry_run else http_sender(args.url, args.timeout)
        print("== %d maquinas, %.0f s, cada %.0f s, %d hilos%s" % (n, args.duration, args.interval, conc, " (dry-run)" if args.dry_run else ""))
        rep = run_load(n, args.duration, args.interval, conc, sender, secret, args.seed, max(1, args.batch), token)
        rep["verdict"] = evaluate(rep, args.max_error_rate, args.max_p95_ms)
        ok_all = ok_all and rep["verdict"]["passed"]
        print(json.dumps(rep, indent=2))
        reports.append(rep)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=2)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
