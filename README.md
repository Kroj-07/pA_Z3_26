# ArquiFusion Fleet Intelligence — Parte A (núcleo AWS)

## Desplegar (Learner Lab, `us-east-1` o `us-west-2`)
```bash
scripts/run_tests.sh                       # pruebas locales
scripts/deploy.sh test us-east-1           # un comando: seguridad, datos, ingesta, eventos, API, puente, SF-2, observabilidad
scripts/set_secrets.sh test us-east-1 --gcp-key-file clave.json --azure-sas-file sas.txt --show-hmac-for-gcp
scripts/smoke_test.sh test us-east-1       # ingesta -> DynamoDB -> API, alerta firmada
scripts/destroy.sh test us-east-1          # limpiar
```
Variables opcionales de `deploy.sh`: `PANDAS_LAYER_ARN`, `GCP_PROJECT_ID`, `AZURE_CONTAINER_URL`, `ALARM_EMAIL`,
`SES_SENDER_EMAIL`, `INCIDENT_WORKFLOW_ARN`, `ENABLE_GLOBAL_TABLES`, `ENABLE_REPLICATION`.

## Cobertura de la Parte A
| Requisito del plan | Dónde |
|---|---|
| Ingesta (API Gateway, SQS+DLQ, Lambda idempotente) | `infra/ingest`, `part-a/ingest` |
| DynamoDB (PITR, TTL, Global Tables), S3 Parquet versionado + replicación | `infra/data` |
| API REST `/v1` con autorizador JWT y callbacks | `infra/api`, `part-a/api` |
| Secretos, firma HMAC, tokens | `infra/security`, `part-a/shared`, `part-a/security` |
| Puente a Pub/Sub y camino inverso firmado | `infra/bridge`, `part-a/bridge` |
| SF-2 (pipeline diario) | `part-a/sf2` |
| Notificaciones SNS/SES y EventBridge | `infra/api/events-template.yaml`, `part-a/events` |
| Respaldo a Azure Blob (A10) y restauración | `part-a/backup` |
| Observabilidad, costos | `part-a/observability` |
| Carga y caos | `load/runner` |
Contrato: `contract/README.md`. Estado: `STATUS.md`.
