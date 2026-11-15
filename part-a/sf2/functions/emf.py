"""Métricas personalizadas de CloudWatch con Embedded Metric Format (EMF).

Una Lambda solo imprime una línea JSON y CloudWatch la convierte en métricas:
no hace llamadas a la API (sin latencia, sin permisos extra, sin put_metric_data).
Costo: la línea de log cuenta como ingesta de logs y cada métrica distinta (nombre
+ combinación de dimensiones) cuesta como métrica personalizada.

Uso:
    import emf
    emf.emit("ArquiFusion/SF2", {"ZonesProcessed": (3, "Count")}, {"Env": "test"})

Reglas de CloudWatch que se validan aquí (si no, la métrica se descarta en silencio):
  - hasta 100 métricas por línea y hasta 30 dimensiones;
  - valores numéricos finitos, unidades de la lista oficial;
  - los nombres y valores de las dimensiones son texto no vacío.

COPIA: part-a/sf2/functions/emf.py es una copia idéntica (la Lambda empaqueta solo su
CodeUri). Si cambias este archivo, copia: cp part-a/observability/emf.py part-a/sf2/functions/emf.py
"""

from __future__ import annotations

import json
import math
import sys
import time
from typing import Any, Dict, Mapping, Optional, Tuple

UNITS = frozenset({
    "Seconds", "Microseconds", "Milliseconds", "Bytes", "Kilobytes", "Megabytes",
    "Gigabytes", "Terabytes", "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits",
    "Percent", "Count", "Bytes/Second", "Kilobytes/Second", "Megabytes/Second",
    "Gigabytes/Second", "Terabytes/Second", "Bits/Second", "Kilobits/Second",
    "Megabits/Second", "Gigabits/Second", "Terabits/Second", "Count/Second", "None",
})
MAX_METRICS = 100
MAX_DIMENSIONS = 30

MetricSpec = Tuple[float, str]


def build(
    namespace: str,
    metrics: Mapping[str, MetricSpec],
    dimensions: Optional[Mapping[str, str]] = None,
    *,
    timestamp_ms: Optional[int] = None,
    properties: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Arma el documento EMF. Lanza ValueError si algo no cumple las reglas."""
    if not namespace or not isinstance(namespace, str):
        raise ValueError("namespace obligatorio")
    if not metrics:
        raise ValueError("se necesita al menos una métrica")
    if len(metrics) > MAX_METRICS:
        raise ValueError("máximo %d métricas por línea" % MAX_METRICS)
    dimensions = dict(dimensions or {})
    if len(dimensions) > MAX_DIMENSIONS:
        raise ValueError("máximo %d dimensiones" % MAX_DIMENSIONS)

    doc: Dict[str, Any] = {}
    for key, value in dimensions.items():
        if not key or not isinstance(value, str) or not value:
            raise ValueError("la dimensión %r necesita un valor de texto no vacío" % key)
        doc[key] = value

    declared = []
    for name, spec in metrics.items():
        value, unit = spec
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("la métrica %r debe ser un número finito" % name)
        if unit not in UNITS:
            raise ValueError("unidad no válida para %r: %r" % (name, unit))
        doc[name] = value
        declared.append({"Name": name, "Unit": unit})

    for key, value in (properties or {}).items():
        doc.setdefault(key, value)  # datos extra para buscar en los logs (no son métricas)

    doc["_aws"] = {
        "Timestamp": int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms),
        "CloudWatchMetrics": [{
            "Namespace": namespace,
            "Dimensions": [list(dimensions)],
            "Metrics": declared,
        }],
    }
    return doc


def emit(
    namespace: str,
    metrics: Mapping[str, MetricSpec],
    dimensions: Optional[Mapping[str, str]] = None,
    *,
    stream: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Imprime la línea EMF (stdout por defecto; Lambda la manda a CloudWatch Logs).

    Nunca rompe el handler: si los datos no son válidos, lo registra en stderr y sigue.
    """
    try:
        doc = build(namespace, metrics, dimensions, **kwargs)
    except ValueError as exc:
        print("EMF inválido, métrica descartada: %s" % exc, file=sys.stderr)
        return {}
    print(json.dumps(doc, separators=(",", ":"), sort_keys=True), file=stream or sys.stdout, flush=True)
    return doc
