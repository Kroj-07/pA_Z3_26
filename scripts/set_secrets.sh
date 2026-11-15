#!/usr/bin/env bash
# Llena los secretos que vienen de fuera de AWS (Azure y GCP). Nada se escribe en archivos del repo.
#
#   scripts/set_secrets.sh [env] [region] [opciones]
#     --azure-sas-file RUTA     archivo con el token SAS de Azure (expiracion corta; permisos w,c; r,l para restaurar)
#     --gcp-key-file RUTA       JSON de la cuenta de servicio de GCP (solo publicar en Pub/Sub)
#     --gcp-project ID          actualiza /arquifusion/{env}/bridge/gcp-project-id
#     --azure-container-url URL actualiza /arquifusion/{env}/backup/azure-container-url (sin SAS)
#     --show-hmac-for-gcp       imprime el comando para guardar el HMAC en el Secret Manager de Google (Parte B)
#     --rotate-hmac             genera un HMAC nuevo (avisar a B: hay que actualizarlo en Google)
#     --rotate-jwt              genera una clave JWT nueva (los tokens anteriores dejan de valer)
set -euo pipefail
ENV_NAME="${1:-test}"; REGION="${2:-us-east-1}"; shift $(( $# >= 2 ? 2 : $# )) || true
SAS_FILE=""; GCP_FILE=""; GCP_PROJECT=""; AZ_URL=""; SHOW=0; ROT_HMAC=0; ROT_JWT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --azure-sas-file) SAS_FILE="$2"; shift 2;;
    --gcp-key-file) GCP_FILE="$2"; shift 2;;
    --gcp-project) GCP_PROJECT="$2"; shift 2;;
    --azure-container-url) AZ_URL="$2"; shift 2;;
    --show-hmac-for-gcp) SHOW=1; shift;;
    --rotate-hmac) ROT_HMAC=1; shift;;
    --rotate-jwt) ROT_JWT=1; shift;;
    *) echo "opcion desconocida: $1" >&2; exit 2;;
  esac
done
P="arquifusion/${ENV_NAME}"
put() { aws secretsmanager put-secret-value --secret-id "$1" --secret-string "file://$2" --region "$REGION" >/dev/null; echo "actualizado $1"; }

if [ -n "$SAS_FILE" ]; then
  [ -s "$SAS_FILE" ] || { echo "archivo vacio: $SAS_FILE" >&2; exit 2; }
  put "$P/azure-sas" "$SAS_FILE"
fi
if [ -n "$GCP_FILE" ]; then
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d.get('type')=='service_account' and d.get('private_key'), 'no parece una clave de cuenta de servicio'" "$GCP_FILE"
  put "$P/gcp-pubsub-key" "$GCP_FILE"
fi
if [ -n "$GCP_PROJECT" ]; then
  aws ssm put-parameter --name "/arquifusion/${ENV_NAME}/bridge/gcp-project-id" --type String --value "$GCP_PROJECT" --overwrite --region "$REGION" >/dev/null
  echo "actualizado SSM bridge/gcp-project-id"
fi
if [ -n "$AZ_URL" ]; then
  aws ssm put-parameter --name "/arquifusion/${ENV_NAME}/backup/azure-container-url" --type String --value "$AZ_URL" --overwrite --region "$REGION" >/dev/null
  echo "actualizado SSM backup/azure-container-url"
fi
rotate() {  # $1 nombre  $2 largo
  NEW=$(aws secretsmanager get-random-password --password-length "$2" --exclude-punctuation --query RandomPassword --output text --region "$REGION")
  aws secretsmanager put-secret-value --secret-id "$P/$1" --secret-string "$NEW" --region "$REGION" >/dev/null
  echo "rotado $P/$1"
}
[ "$ROT_HMAC" = 1 ] && rotate alert-hmac 48
[ "$ROT_JWT" = 1 ] && rotate jwt-signing 64

if [ "$SHOW" = 1 ]; then
  echo
  echo "Para la Parte B (NO pegues el valor en chats ni en el repo). En una terminal con gcloud:"
  echo
  echo "  aws secretsmanager get-secret-value --secret-id ${P}/alert-hmac --region ${REGION} \\"
  echo "      --query SecretString --output text | tr -d '\\n' | \\"
  echo "  gcloud secrets versions add alert-hmac --data-file=- --project <PROYECTO_GCP>"
  echo
  echo "(Si el secreto aun no existe en Google: gcloud secrets create alert-hmac --replication-policy=automatic)"
fi
echo "Pendientes: $(aws secretsmanager get-secret-value --secret-id "$P/azure-sas" --region "$REGION" --query SecretString --output text 2>/dev/null | grep -qx REEMPLAZAR && echo -n 'azure-sas ' || true)$(aws secretsmanager get-secret-value --secret-id "$P/gcp-pubsub-key" --region "$REGION" --query SecretString --output text 2>/dev/null | grep -qx REEMPLAZAR && echo -n 'gcp-pubsub-key' || true)"
