#!/usr/bin/env bash
# Imprime el ARN de la capa publica AWS SDK for pandas (trae pyarrow) para la Lambda de ingesta
# (python3.12, arm64). Uso:  LAYER=$(scripts/resolve_pandas_layer.sh us-east-1)
# Si el Learner Lab bloquea lambda:ListLayerVersions, copia el ARN de la lista oficial
#   https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
# y pasalo con PANDAS_LAYER_ARN=... a scripts/deploy.sh.
set -euo pipefail
REGION="${1:-us-east-1}"
NAME="arn:aws:lambda:${REGION}:336392948345:layer:AWSSDKPandas-Python312-Arm64"
if ! ARN=$(aws lambda list-layer-versions --layer-name "$NAME" --region "$REGION" \
      --max-items 1 --query 'LayerVersions[0].LayerVersionArn' --output text 2>/dev/null) \
   || [ -z "$ARN" ] || [ "$ARN" = "None" ]; then
  echo "No pude resolver la capa $NAME en $REGION. Define PANDAS_LAYER_ARN a mano." >&2
  exit 1
fi
echo "$ARN"
