# SF-2 — Pipeline analítico (IA-5)

Step Functions STANDARD `af-{env}-sf2-pipeline`: `ApplyDefaults → SyncToBigQuery → ListZones → ProcessZones (Map) → BackupToAzure → Summarize`.
Dentro del Map, por zona: `TrainZoneModel → EvaluateModel → PromoteOrKeep (Choice) → PromoteModel | KeepCurrentModel`.
Un fallo en una zona se captura (`ZoneFailed`) y no tumba a las demás; un fallo de sync o de lista sí termina en `PipelineFailed`.

## Archivos
`template.yaml` (SAM) · `statemachine.asl.json` · `functions/pipeline.py` (6 handlers mock) · `functions/emf.py` (copia de `observability/emf.py`) · `tests/`

## Variables (parámetros SAM → entorno de las Lambdas)
| Parámetro | Defecto | Uso |
|---|---|---|
| `Env` | test | `shared` o `test` (contrato §11); va en nombres `af-{env}-...` y en la ruta SSM |
| `LabRoleArn` | — (obligatorio) | rol de todo (Learner Lab no deja crear roles) |
| `Mock` | true | BigQuery y entrenamiento simulados |
| `Zones` | ZONA-NORTE-01,02 / SUR-01 | zonas del Map |
| `PromoteMinImprovement` | 0.02 | mejora relativa mínima del MAE para promover |
| `ScheduleEnabled` / `ScheduleExpression` | false / rate(1 day) | disparo diario (apagado por defecto) |

Publica en SSM: `/arquifusion/{env}/sf2/state-machine-arn` y `.../state-machine-name`.

## Probar
```
python -m unittest discover -s part-a/sf2/tests -v        # offline: handlers + estructura del ASL
sam build -t part-a/sf2/template.yaml
sam deploy -t part-a/sf2/template.yaml --stack-name af-sf2-test --region us-east-1 \
  --parameter-overrides LabRoleArn=arn:aws:iam::<ACCOUNT_ID>:role/LabRole --capabilities CAPABILITY_IAM
aws stepfunctions start-execution --state-machine-arn <arn> --input '{}'                      # camino feliz
aws stepfunctions start-execution --state-machine-arn <arn> --input '{"force_failure":true}'  # prueba Retry/Catch
```

## Nombres de las tablas (para la versión real del sync)
`infra/data/template.yaml` (IA-2) las publica en SSM como `/arquifusion/{env}/data/{machines,telemetry,alerts,incidents,technicians,inventory}-table`. La versión real de `sync_bigquery` debe leerlas de ahí. Ojo: el contrato §8 dice `/arquifusion/{env}/ddb/`; ver nota de discrepancia abajo.

## Pendiente
- Discrepancia contrato vs. IA-2 (no es mi carpeta, solo aviso): SSM `/data/` en vez de `/ddb/`, y bucket `af-{env}-data-{cuenta}` en vez de `af-{env}-arquifusion-data`. Alguien debe corregir el template o proponer el cambio de contrato.
- `BackupToAzure` (contrato §11, A10) es mock y no tumba el pipeline si falla: reutilizar `part-a/backup/azure_backup` de IA-4 (secreto `arquifusion/{env}/azure-sas`).
- Tablas BigQuery `fleet`: el sync cubre telemetry, machines, technicians e incidents; `model_runs` (una fila por zona y corrida) falta en la versión real.
- Sustituir los mocks: `sync_bigquery` (DynamoDB → BigQuery, credencial de Google en Secrets Manager) y `train_zone`/`promote_model`.

- No se ejecutaron `cfn-lint` ni `sam validate` (el entorno donde se escribió no los tiene): correrlos antes del PR.
