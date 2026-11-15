#!/usr/bin/env python3
"""Caos: apaga el consumidor de SQS 60 s y mide el impacto y la recuperacion.

Como: deshabilita el event source mapping (cola -> Lambda consumidora). Los mensajes se acumulan
en la cola durante la ventana; luego se vuelve a habilitar y se mide cuanto tarda en vaciarse.
Es seguro: el mapping se vuelve a habilitar SIEMPRE (finally), incluso con Ctrl+C o error.
No borra mensajes ni toca datos.

Ejemplos:
  # ver el plan sin tocar nada
  python chaos_sqs.py --dry-run --function-name af-ingest-consumer-test --queue-url https://sqs...

  # real, con los nombres leidos de SSM /arquifusion/test/ingest/{consumer-function-name,queue-url}
  python chaos_sqs.py --env test --region us-east-1 --duration 60

  # genera carga en otra terminal mientras corre:
  python load_test.py --profile 1000 --duration 120

Pide: permisos lambda:ListEventSourceMappings/UpdateEventSourceMapping, sqs:GetQueueAttributes,
ssm:GetParameter (LabRole los tiene normalmente). Region solo us-east-1 o us-west-2.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable, Dict, List, Optional

REGIONS = ("us-east-1", "us-west-2")
QUEUE_ATTRS = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateAgeOfOldestMessage"]


def queue_snapshot(sqs: Any, queue_url: str) -> Dict[str, int]:
    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=QUEUE_ATTRS)["Attributes"]
    return {
        "depth": int(attrs.get("ApproximateNumberOfMessages", 0)),
        "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        "oldest_age_s": int(attrs.get("ApproximateAgeOfOldestMessage", 0)),
    }


def find_mapping(lam: Any, function_name: str) -> str:
    maps = lam.list_event_source_mappings(FunctionName=function_name).get("EventSourceMappings", [])
    sqs_maps = [m for m in maps if ":sqs:" in m.get("EventSourceArn", "")]
    if len(sqs_maps) != 1:
        raise RuntimeError("se esperaba 1 mapping SQS para %s y hay %d; usa --mapping-uuid" % (function_name, len(sqs_maps)))
    return sqs_maps[0]["UUID"]


def wait_state(lam: Any, uuid: str, wanted: str, timeout: float, sleep: Callable[[float], None]) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 <= timeout:
        if lam.get_event_source_mapping(UUID=uuid)["State"] == wanted:
            return time.monotonic() - t0
        sleep(2)
    raise TimeoutError("el mapping no llego a %s en %.0f s" % (wanted, timeout))


def run_chaos(
    lam: Any, sqs: Any, uuid: str, queue_url: str, duration: float, poll: float,
    recovery_timeout: float, sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"mapping": uuid, "queue_url": queue_url, "outage_s": duration}
    report["baseline"] = queue_snapshot(sqs, queue_url)
    samples: List[Dict[str, Any]] = []
    disabled = False
    try:
        lam.update_event_source_mapping(UUID=uuid, Enabled=False)
        disabled = True
        report["disable_confirmed_after_s"] = round(wait_state(lam, uuid, "Disabled", 60, sleep), 1)
        t_off = time.monotonic()
        while time.monotonic() - t_off < duration:
            snap = queue_snapshot(sqs, queue_url)
            snap["t_s"] = round(time.monotonic() - t_off, 1)
            samples.append(snap)
            sleep(poll)
    finally:
        if disabled:
            lam.update_event_source_mapping(UUID=uuid, Enabled=True)
            report["reenabled"] = True
    report["during_outage"] = {
        "peak_depth": max((s["depth"] for s in samples), default=0),
        "peak_oldest_age_s": max((s["oldest_age_s"] for s in samples), default=0),
        "samples": samples,
    }
    wait_state(lam, uuid, "Enabled", 120, sleep)
    t_on = time.monotonic()
    drained: Optional[float] = None
    while time.monotonic() - t_on <= recovery_timeout:
        snap = queue_snapshot(sqs, queue_url)
        if snap["depth"] <= report["baseline"]["depth"] and snap["in_flight"] == 0:
            drained = time.monotonic() - t_on
            break
        sleep(poll)
    report["recovery"] = {
        "drained": drained is not None,
        "time_to_drain_s": round(drained, 1) if drained is not None else None,
        "final": queue_snapshot(sqs, queue_url),
    }
    report["passed"] = drained is not None
    return report


def ssm_value(ssm: Any, name: str) -> str:
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-east-1", choices=REGIONS)
    ap.add_argument("--env", help="lee function-name y queue-url de SSM /arquifusion/{env}/ingest/")
    ap.add_argument("--function-name", help="Lambda consumidora (alternativa a --env)")
    ap.add_argument("--mapping-uuid", help="UUID del event source mapping (si no, se busca por funcion)")
    ap.add_argument("--queue-url", help="URL de la cola (alternativa a --env)")
    ap.add_argument("--duration", type=float, default=60, help="segundos con el consumidor apagado")
    ap.add_argument("--poll", type=float, default=5)
    ap.add_argument("--recovery-timeout", type=float, default=300)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", help="archivo JSON de salida")
    args = ap.parse_args(argv)

    function_name, queue_url = args.function_name, args.queue_url
    if args.dry_run and not (function_name and queue_url) and args.env:
        function_name = function_name or "/arquifusion/%s/ingest/consumer-function-name (SSM)" % args.env
        queue_url = queue_url or "/arquifusion/%s/ingest/queue-url (SSM)" % args.env
    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "region": args.region, "function": function_name, "queue": queue_url,
            "plan": ["snapshot de la cola", "deshabilitar mapping", "esperar %.0f s muestreando la cola" % args.duration,
                     "rehabilitar mapping (siempre)", "medir tiempo de vaciado"],
        }, indent=2))
        return 0

    import boto3  # solo en ejecucion real

    session = boto3.session.Session(region_name=args.region)
    lam, sqs, ssm = session.client("lambda"), session.client("sqs"), session.client("ssm")
    if args.env:
        function_name = function_name or ssm_value(ssm, "/arquifusion/%s/ingest/consumer-function-name" % args.env)
        queue_url = queue_url or ssm_value(ssm, "/arquifusion/%s/ingest/queue-url" % args.env)
    if not (args.mapping_uuid or function_name) or not queue_url:
        ap.error("faltan datos: usa --env, o --function-name/--mapping-uuid y --queue-url")
    uuid = args.mapping_uuid or find_mapping(lam, function_name)
    report = run_chaos(lam, sqs, uuid, queue_url, args.duration, args.poll, args.recovery_timeout)
    print(json.dumps(report, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
