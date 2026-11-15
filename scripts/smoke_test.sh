#!/usr/bin/env bash
# Prueba de humo de la Parte A ya desplegada: ingesta -> cola -> DynamoDB/S3 -> API, y alerta firmada.
#   scripts/smoke_test.sh [env] [region]
# Usa credenciales del Lab y emite un JWT de rol admin con part-a/api/mint_token.py.
set -euo pipefail
ENV_NAME="${1:-test}"; REGION="${2:-us-east-1}"
cd "$(dirname "$0")/.."
ssm() { aws ssm get-parameter --name "/arquifusion/${ENV_NAME}/$1" --region "$REGION" --query Parameter.Value --output text; }
ok() { echo "  OK   $*"; }
fail() { echo "  FALLA $*" >&2; FAILED=1; }
FAILED=0

echo "== Parametros SSM"
for n in ingest/queue-url ingest/api-url data/telemetry-table data/machines-table data/alerts-table events/bus-name; do
  ssm "$n" >/dev/null && ok "$n" || fail "$n"
done

INGEST_URL="$(ssm ingest/api-url)/v1/telemetry"
TOKEN=$(python3 part-a/api/mint_token.py --env "$ENV_NAME" --role admin --sub smoke)
MID="MCH-999001"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BODY=$(cat <<JSON
{"readings":[{"schema_version":"1.0","machine_id":"${MID}","timestamp":"${TS}",
"location":{"lat":-12.0464,"lon":-77.0428,"zone_id":"ZONA-NORTE-02"},"status":"operating",
"metrics":{"engine_temp_c":92.4,"vibration_mm_s":4.8,"pressure_bar":6.1,"fuel_rate_lph":31.2,
"load_kg":12400,"speed_kmh":18.5,"engine_rpm":1850,"operating_hours_total":3240.7}}]}
JSON
)

echo "== Ingesta sin token (debe ser 401/403)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$INGEST_URL" -d "$BODY")
case "$CODE" in 401|403) ok "rechazada ($CODE)";; *) fail "se esperaba 401/403 y fue $CODE";; esac

echo "== Ingesta con token admin (debe ser 200)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$INGEST_URL" -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' -d "$BODY")
[ "$CODE" = 200 ] && ok "aceptada" || fail "HTTP $CODE"

echo "== Lectura en DynamoDB (espera hasta 60 s)"
TABLE=$(ssm data/telemetry-table)
FOUND=0
for i in $(seq 1 12); do
  N=$(aws dynamodb query --table-name "$TABLE" --region "$REGION" --key-condition-expression 'machine_id = :m' \
      --expression-attribute-values "{\":m\":{\"S\":\"${MID}\"}}" --select COUNT --query Count --output text)
  [ "$N" -ge 1 ] && { FOUND=1; break; }
  sleep 5
done
[ "$FOUND" = 1 ] && ok "lectura guardada" || fail "la lectura no aparecio en $TABLE"

API_URL=$(ssm api/base-url 2>/dev/null || true)
if [ -n "$API_URL" ]; then
  echo "== API REST: GET /machines/${MID}"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "${API_URL%/}/machines/${MID}" -H "Authorization: Bearer ${TOKEN}")
  [ "$CODE" = 200 ] && ok "200" || fail "HTTP $CODE"

  echo "== Alerta firmada (POST /alerts)"
  SECRET=$(aws secretsmanager get-secret-value --secret-id "arquifusion/${ENV_NAME}/alert-hmac" --region "$REGION" --query SecretString --output text)
  ALERT=$(python3 - <<PY
import json
a=json.load(open("contract/examples/alert.json"))
a["alert_id"]="ALT-999001"; a["machine_id"]="${MID}"
print(json.dumps(a,separators=(",",":")))
PY
)
  ATS=$(date +%s)
  SIG=$(printf '%s.%s' "$ATS" "$ALERT" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API_URL%/}/alerts" -H 'Content-Type: application/json' \
         -H "x-timestamp: ${ATS}" -H "x-signature: ${SIG}" -d "$ALERT")
  case "$CODE" in 200|201) ok "alerta aceptada ($CODE)";; *) fail "alerta HTTP $CODE";; esac
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API_URL%/}/alerts" -H 'Content-Type: application/json' \
         -H "x-timestamp: ${ATS}" -H "x-signature: 00bad" -d "$ALERT")
  [ "$CODE" = 401 ] && ok "firma invalida rechazada (401)" || fail "firma invalida dio $CODE (esperado 401)"
else
  echo "  (ssm api/base-url no existe: se omite la prueba de la API REST)"
fi

echo
[ "$FAILED" = 0 ] && echo "Humo OK" || { echo "Humo con fallas" >&2; exit 1; }
