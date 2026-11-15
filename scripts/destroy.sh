#!/usr/bin/env bash
# Borra la Parte A de un entorno. Vacia los buckets versionados antes (CloudFormation no puede).
#   scripts/destroy.sh [env] [region]      (pide confirmacion; para "shared" exige escribir el nombre)
set -euo pipefail
ENV_NAME="${1:-test}"; REGION="${2:-us-east-1}"
OTHER=$([ "$REGION" = "us-east-1" ] && echo us-west-2 || echo us-east-1)
read -r -p "Borrar af-${ENV_NAME}-root en ${REGION}? Escribe '${ENV_NAME}' para confirmar: " ANS
[ "$ANS" = "$ENV_NAME" ] || { echo "Cancelado."; exit 1; }

empty_bucket() {  # $1 bucket  $2 region
  local b="$1" r="$2"
  [ -n "$b" ] && [ "$b" != "None" ] || return 0
  echo "vaciando s3://$b"
  python3 - "$b" "$r" <<'PY'
import sys, boto3
b, r = sys.argv[1], sys.argv[2]
s3 = boto3.resource("s3", region_name=r)
try:
    s3.Bucket(b).object_versions.delete()
except Exception as e:
    print("aviso:", e)
PY
}
BUCKET=$(aws ssm get-parameter --name "/arquifusion/${ENV_NAME}/data/telemetry-bucket" --region "$REGION" --query Parameter.Value --output text 2>/dev/null || true)
REPLICA=$(aws ssm get-parameter --name "/arquifusion/${ENV_NAME}/data/replica-bucket" --region "$REGION" --query Parameter.Value --output text 2>/dev/null || true)
empty_bucket "$BUCKET" "$REGION"
empty_bucket "$REPLICA" "$OTHER"

aws cloudformation delete-stack --stack-name "af-${ENV_NAME}-root" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "af-${ENV_NAME}-root" --region "$REGION"
if aws cloudformation describe-stacks --stack-name "af-${ENV_NAME}-data-replica" --region "$OTHER" >/dev/null 2>&1; then
  RB=$(aws ssm get-parameter --name "/arquifusion/${ENV_NAME}/data/replica-bucket" --region "$OTHER" --query Parameter.Value --output text 2>/dev/null || true)
  empty_bucket "$RB" "$OTHER"
  aws cloudformation delete-stack --stack-name "af-${ENV_NAME}-data-replica" --region "$OTHER"
  aws cloudformation wait stack-delete-complete --stack-name "af-${ENV_NAME}-data-replica" --region "$OTHER"
fi
echo "Listo. Los secretos de Secrets Manager quedan 7 dias en borrado programado."
echo "Para recrearlos de inmediato: aws secretsmanager delete-secret --secret-id arquifusion/${ENV_NAME}/<nombre> --force-delete-without-recovery"
