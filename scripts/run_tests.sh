#!/usr/bin/env bash
# Corre todas las pruebas unitarias de la Parte A (sin AWS). Uso: scripts/run_tests.sh
# Requiere: pip install pyyaml boto3 pyarrow
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0
run() { echo "== $1"; shift; "$@" || FAIL=1; }
run "plantillas (reglas del Lab)" python3 scripts/check_templates.py
(cd part-a && run "API, autorizador, alertas, callbacks, notificaciones" python3 -m unittest api.test_api)
(cd part-a && run "hmac_utils" python3 -m unittest shared.test_hmac_utils)
(cd part-a/ingest && run "ingesta" python3 -m unittest test_app)
(cd part-a/sf2 && run "SF-2" python3 -m unittest tests.test_pipeline)
(cd load/runner && run "carga y caos" python3 -m unittest test_runner)
[ "$FAIL" = 0 ] && echo "TODO OK" || { echo "HAY FALLAS" >&2; exit 1; }
