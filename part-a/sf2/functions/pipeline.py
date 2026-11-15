"""SF-2 (pipeline analítico) - tareas del Step Functions. Python 3.12, solo stdlib.

Todas las tareas son MOCK por defecto (MOCK=true): BigQuery y el entrenamiento real aún no
existen. Cada handler respeta el contrato de entrada/salida que usa la máquina de estados, de modo
que sustituir un mock por la versión real no obliga a tocar el ASL.

Handlers (módulo `pipeline`):
  list_zones       -> {"zones": [...]}                       zonas a procesar
  sync_bigquery    -> {"rows_synced": n, "tables": {...}}    DynamoDB -> BigQuery (dataset fleet)
  train_zone       -> {"zone_id", "candidate": {...}}        reentrena el modelo de una zona
  evaluate_model   -> {..., "evaluation": {..., "promote"}}  compara candidato vs. modelo vigente
  promote_model    -> {"zone_id", "promoted": true, ...}     registra el candidato como vigente
  backup_to_azure  -> {"copied": bool, "target": "azure://..."}  respaldo A10
  summarize        -> {"zones_total", "promoted", ...}       resumen + métricas EMF

Las métricas se emiten con EMF (emf.py viaja junto al código de la Lambda).
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List

try:  # empaquetado: emf.py viaja junto al código
    import emf
except ImportError:  # pruebas desde el repo
    try:
        from observability import emf  # type: ignore
    except ImportError:  # sin EMF las métricas se omiten, el pipeline sigue
        class emf:  # type: ignore
            @staticmethod
            def emit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                return {}

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

NAMESPACE = "ArquiFusion/SF2"
# Tablas del dataset BigQuery `fleet` (contrato v1.3, seccion 11). alerts_out la escribe B;
# model_runs la escribe este pipeline (una fila por zona y corrida) en la version real.
BQ_SYNC_TABLES = ("telemetry", "machines", "technicians", "incidents")
DEFAULT_ZONES = "ZONA-NORTE-01,ZONA-NORTE-02,ZONA-SUR-01"


def _env() -> str:
    return os.environ.get("ENV_NAME", "test")


def _mock() -> bool:
    return os.environ.get("MOCK", "true").lower() == "true"


def _min_improvement() -> float:
    """Mejora relativa mínima del MAE para promover el candidato."""
    return float(os.environ.get("PROMOTE_MIN_IMPROVEMENT", "0.02"))


def _unit(*parts: str) -> float:
    """Número determinista en [0, 1) a partir de textos (mocks reproducibles)."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def list_zones(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    override = event.get("zones") if isinstance(event, dict) else None
    if override:
        zones: List[str] = [str(z) for z in override]
    else:
        zones = [z.strip() for z in os.environ.get("ZONES", DEFAULT_ZONES).split(",") if z.strip()]
    if not zones:
        raise ValueError("no hay zonas configuradas")
    return {"zones": zones}


def sync_bigquery(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """MOCK: la versión real leerá DynamoDB (Telemetry/Alerts) y cargará BigQuery."""
    if event.get("force_failure"):  # permite probar Retry/Catch del ASL
        raise RuntimeError("BigQueryUnavailable: fallo simulado")
    rows = int(_unit("rows", str(event.get("run_id", ""))) * 50000) + 1000
    weights = {"telemetry": 0.90, "machines": 0.02, "technicians": 0.01, "incidents": 0.07}
    tables = {t: int(rows * weights[t]) for t in BQ_SYNC_TABLES}
    # la version real hace upsert con la clave natural (machine_id+timestamp, incident_id) = idempotente
    emf.emit(NAMESPACE, {"RowsSynced": (rows, "Count")}, {"Env": _env()})
    return {"rows_synced": rows, "tables": tables, "mock": _mock()}


def train_zone(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """MOCK: la versión real lanzará el reentrenamiento (p. ej. BigQuery ML) de la zona."""
    zone = event["zone_id"]
    run = str(event.get("run_id", ""))
    mae = round(0.05 + _unit("cand", zone, run) * 0.10, 4)  # 0.05 - 0.15
    return {
        "zone_id": zone,
        "run_id": run,
        "candidate": {
            "model_id": "af-model-%s-%s" % (zone.lower(), run[:8] or "local"),
            "mae": mae,
            "mock": _mock(),
        },
    }


def evaluate_model(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Compara el MAE del candidato con el del modelo vigente. `promote` alimenta la Choice del ASL."""
    zone = event["zone_id"]
    candidate_mae = float(event["candidate"]["mae"])
    current_mae = round(0.06 + _unit("curr", zone) * 0.10, 4)  # 0.06 - 0.16
    improvement = round((current_mae - candidate_mae) / current_mae, 4)
    threshold = _min_improvement()
    out = dict(event)
    out["evaluation"] = {
        "current_mae": current_mae,
        "candidate_mae": candidate_mae,
        "improvement": improvement,
        "threshold": threshold,
        "promote": improvement >= threshold,
    }
    return out


def promote_model(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """MOCK: la versión real actualizará el modelo vigente (p. ej. parámetro SSM o alias)."""
    return {
        "zone_id": event["zone_id"],
        "promoted": True,
        "status": "OK",
        "model_id": event["candidate"]["model_id"],
        "improvement": event["evaluation"]["improvement"],
        "mock": _mock(),
    }


def backup_to_azure(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """MOCK: la version real copia a azure://af-backup/... usando el secreto arquifusion/{env}/azure-sas
    (IA-4 ya tiene part-a/backup/azure_backup: reutilizarlo en vez de duplicar)."""
    return {"copied": True, "target": "azure://af-backup/sf2/%s/" % event.get("run_id", "local"), "mock": _mock()}


def summarize(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    results = event.get("results") or []
    failed = sum(1 for r in results if r.get("status") == "FAILED")
    promoted = sum(1 for r in results if r.get("promoted") is True)
    kept = len(results) - failed - promoted
    emf.emit(
        NAMESPACE,
        {
            "ZonesProcessed": (len(results), "Count"),
            "ZonesPromoted": (promoted, "Count"),
            "ZonesKept": (kept, "Count"),
            "ZonesFailed": (failed, "Count"),
        },
        {"Env": _env()},
        properties={"run_id": str(event.get("run_id", ""))},
    )
    return {
        "run_id": event.get("run_id"),
        "rows_synced": event.get("rows_synced"),
        "zones_total": len(results),
        "promoted": promoted,
        "kept": kept,
        "failed": failed,
    }
