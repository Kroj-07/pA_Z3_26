# infra/bridge — Puente a Pub/Sub y respaldo a Azure (A10)

- `af-{env}-bridge-pubsub`: lee el stream de `Telemetry` (SSM `bridge/source-stream-arn`, lo publica `infra/data`) y publica
  el JSON de la lectura en el tópico `telemetry-raw`. La clave de GCP sale de `arquifusion/{env}/gcp-pubsub-key`;
  proyecto y tópico, de SSM `bridge/gcp-project-id` y `bridge/pubsub-topic` (`infra/security`).
- `af-{env}-backup-azure`: cada día copia el Parquet nuevo de S3 a Azure Blob con el SAS de `arquifusion/{env}/azure-sas`.
  Restaurar: `python part-a/backup/restore_from_azure.py --env test --dry-run`.

Parámetros: `Env`, `LabRoleArn`, `SourceKind` (`dynamodb` por defecto; `sqs` solo si alguien publica `bridge/source-queue-arn`),
`ScheduleExpression` (07:00 UTC), `ScheduleEngine` (`scheduler` o `events` si el Lab rechaza Scheduler).

Llenar los secretos: `scripts/set_secrets.sh test us-east-1 --gcp-key-file clave.json --azure-sas-file sas.txt --gcp-project <ID>`.

## Pendiente
- Sin despliegue real ni prueba contra Pub/Sub/Azure (necesita los datos de B y la cuenta de Azure).
- El SAS del contrato es de solo escritura; para restaurar hace falta uno con lectura y listado (`r`, `l`).
