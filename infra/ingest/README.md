# infra/ingest — Ingesta (API HTTP → SQS + DLQ → Lambda)

`POST {ingest/api-url}/v1/telemetry` (JWT de rol `admin`) → API Gateway HTTP envía el cuerpo directo a SQS
(sin Lambda intermedia) → Lambda `af-{env}-ingest` (`part-a/ingest/app.py`) valida, deduplica, guarda en
DynamoDB (TTL 30 días) y en S3 (Parquet) y actualiza `Machines`. Respuesta parcial por lote; tras 5 intentos
el mensaje va a la DLQ `af-{env}-ingest-dlq`.

| Parámetro | Por defecto | Uso |
|---|---|---|
| `Env` | `test` | `test` o `shared` |
| `PandasLayerArn` | vacío | Capa pública AWS SDK for pandas (pyarrow). `scripts/resolve_pandas_layer.sh` |
| `MaxConcurrency` | 50 | Tope de la Lambda; cuida el crédito del Lab |
| `MaxReceiveCount` | 5 | Intentos antes de la DLQ |

SSM que publica (`/arquifusion/{env}/ingest/`): `function-name`, `consumer-function-name`, `queue-name`,
`queue-url`, `queue-arn`, `dlq-name`, `dlq-arn`, `mapping-uuid`, `api-url`. Lee de `data/`: `telemetry-table`,
`machines-table`, `telemetry-bucket`.

Probar: `scripts/smoke_test.sh test us-east-1`. Token de prueba: `python part-a/api/mint_token.py --env test --role admin --sub generador`.
Caos: `load/runner/chaos_sqs.py --env test` (apaga el consumidor, la cola retiene, se reactiva).

## Pendiente
- Sin despliegue real. La integración API Gateway → SQS usa LabRole como `CredentialsArn`; si el Lab la rechaza,
  reemplazarla por una Lambda que haga `sqs:SendMessage`.
- Si `scripts/resolve_pandas_layer.sh` falla, pasar el ARN a mano en `PANDAS_LAYER_ARN`.
