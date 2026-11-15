#!/usr/bin/env bash
# Despliegue de la Parte A en UN comando (Learner Lab: LabRole, us-east-1 / us-west-2, sin CDK).
#
#   scripts/deploy.sh [env] [region]      env: test (por defecto) | shared ; region: us-east-1 | us-west-2
#
# Variables opcionales:
#   PANDAS_LAYER_ARN     ARN de la capa pyarrow (si no, se resuelve con scripts/resolve_pandas_layer.sh)
#   GCP_PROJECT_ID       proyecto de Google Cloud (lo da la Parte B)
#   AZURE_CONTAINER_URL  https://<cuenta>.blob.core.windows.net/<contenedor>   (sin SAS)
#   ALARM_EMAIL, SES_SENDER_EMAIL, INCIDENT_WORKFLOW_ARN, ALLOWED_ORIGIN
#   ENABLE_GLOBAL_TABLES=true|false (solo primer despliegue), ENABLE_REPLICATION=true|false
#
# Despues:  scripts/set_secrets.sh <env> <region>   y   scripts/smoke_test.sh <env> <region>
set -euo pipefail

ENV_NAME="${1:-test}"
REGION="${2:-us-east-1}"
cd "$(dirname "$0")/.."

case "$ENV_NAME" in test|shared) ;; *) echo "env invalido: $ENV_NAME (test|shared)" >&2; exit 2;; esac
case "$REGION" in us-east-1|us-west-2) ;; *) echo "region invalida: $REGION (Learner Lab: us-east-1|us-west-2)" >&2; exit 2;; esac
command -v sam >/dev/null || { echo "Falta SAM CLI (pip install aws-sam-cli)" >&2; exit 2; }
command -v aws >/dev/null || { echo "Falta AWS CLI" >&2; exit 2; }

echo "== 1/5 Reglas del Lab sobre las plantillas"
python3 scripts/check_templates.py

echo "== 2/5 Cuenta"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
echo "cuenta $ACCOUNT, region $REGION, entorno $ENV_NAME"

echo "== 3/5 Capa de pyarrow"
LAYER="${PANDAS_LAYER_ARN:-}"
if [ -z "$LAYER" ]; then
  LAYER=$(scripts/resolve_pandas_layer.sh "$REGION") || { echo "Continuo sin capa: la ingesta no podra escribir Parquet." >&2; LAYER=""; }
fi
echo "capa: ${LAYER:-ninguna}"

OTHER=$([ "$REGION" = "us-east-1" ] && echo us-west-2 || echo us-east-1)
GLOBAL="${ENABLE_GLOBAL_TABLES:-false}"
REPL="${ENABLE_REPLICATION:-false}"

if [ "$REPL" = "true" ]; then
  echo "== 3b Stack de replica en $OTHER (bucket destino de la replicacion de S3)"
  sam deploy -t infra/data/template.yaml --stack-name "af-${ENV_NAME}-data-replica" --region "$OTHER" \
    --parameter-overrides "Env=${ENV_NAME}" "StackRole=replica" "LabRoleArn=arn:aws:iam::${ACCOUNT}:role/LabRole" \
    --resolve-s3 --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --no-confirm-changeset --no-fail-on-empty-changeset
fi

echo "== 4/5 Stack raiz (anida seguridad, datos, ingesta, eventos, API, puente, SF-2, observabilidad)"
OVERRIDES=("Env=${ENV_NAME}" "EnableGlobalTables=${GLOBAL}" "EnableReplication=${REPL}")
[ -n "$LAYER" ] && OVERRIDES+=("PandasLayerArn=${LAYER}")
[ -n "${GCP_PROJECT_ID:-}" ] && OVERRIDES+=("GcpProjectId=${GCP_PROJECT_ID}")
[ -n "${AZURE_CONTAINER_URL:-}" ] && OVERRIDES+=("AzureContainerUrl=${AZURE_CONTAINER_URL}")
[ -n "${ALARM_EMAIL:-}" ] && OVERRIDES+=("AlarmEmail=${ALARM_EMAIL}")
[ -n "${SES_SENDER_EMAIL:-}" ] && OVERRIDES+=("SesSenderEmail=${SES_SENDER_EMAIL}")
[ -n "${INCIDENT_WORKFLOW_ARN:-}" ] && OVERRIDES+=("IncidentWorkflowArn=${INCIDENT_WORKFLOW_ARN}")
[ -n "${ALLOWED_ORIGIN:-}" ] && OVERRIDES+=("AllowedOrigin=${ALLOWED_ORIGIN}")

sam deploy -t infra/root/template.yaml --stack-name "af-${ENV_NAME}-root" --region "$REGION" \
  --parameter-overrides "${OVERRIDES[@]}" \
  --resolve-s3 --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-confirm-changeset --no-fail-on-empty-changeset

echo "== 5/5 Resultado"
aws cloudformation describe-stacks --stack-name "af-${ENV_NAME}-root" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output table
echo
echo "Siguiente: scripts/set_secrets.sh ${ENV_NAME} ${REGION} --show-hmac-for-gcp"
echo "           scripts/smoke_test.sh ${ENV_NAME} ${REGION}"
