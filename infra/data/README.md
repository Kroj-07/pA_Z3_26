# infra/data — Persistencia (IA-2)

`template.yaml` (CloudFormation) crea las 6 tablas DynamoDB y el bucket S3 del contrato
(sección 8), y publica sus nombres en SSM bajo `/arquifusion/{env}/data/`.

## Qué crea

| Recurso | Nombre | Claves | Extras |
|---|---|---|---|
| Machines | `af-{env}-machines` | PK `machine_id` | PITR |
| Telemetry | `af-{env}-telemetry` | PK `machine_id`, SK `timestamp` | **TTL** en el atributo `ttl` (30 días); sin PITR |
| Alerts | `af-{env}-alerts` | PK `machine_id`, SK `created_at` | **GSI** `zone_id-created_at-index` y `severity-created_at-index` |
| Incidents | `af-{env}-incidents` | PK `machine_id`, SK `opened_at` | GSI `status-opened_at-index` |
| Technicians | `af-{env}-technicians` | PK `technician_id` | PITR |
| Inventory | `af-{env}-inventory` | PK `zone_id`, SK `part_id` | PITR |
| Bucket de datos | `af-{env}-data-{cuenta}` | `telemetry/`, `analytics/`, `exports/` | **Versionado**, cifrado AES256, sin acceso público, **replicación** opcional |
| Bucket réplica | `af-{env}-data-replica-{cuenta}` | — | Versionado; se crea con `StackRole=replica` |

Todas las tablas son `PAY_PER_REQUEST`. Sin EC2, RDS, NAT ni balanceadores.

## Parámetros

| Parámetro | Valores | Descripción |
|---|---|---|
| `Env` | `shared` o `test` (por defecto `test`) | Entorno |
| `StackRole` | `primary` (defecto) o `replica` | `replica` solo crea el bucket destino |
| `EnableReplication` | `false` (defecto) o `true` | Replicación S3 hacia el bucket réplica |
| `EnableGlobalTables` | `false` (defecto) o `true` | Alerts e Incidents como Global Tables (us-east-1 y us-west-2) |
| `LabRoleArn` | opcional | ARN de LabRole; vacío = se construye con la cuenta actual |

## Parámetros SSM publicados

`/arquifusion/{env}/data/` + `machines-table`, `telemetry-table`, `alerts-table`,
`incidents-table`, `technicians-table`, `inventory-table`, `telemetry-bucket`
(y `replica-bucket` en el stack réplica).

## Cómo probar y desplegar

```bash
pip install cfn-lint
cfn-lint infra/data/template.yaml

# 1) Bucket réplica, en la OTRA región
aws cloudformation deploy --template-file infra/data/template.yaml \
  --stack-name af-test-data-replica --region us-west-2 \
  --parameter-overrides Env=test StackRole=replica

# 2) Tablas y bucket principal
aws cloudformation deploy --template-file infra/data/template.yaml \
  --stack-name af-test-data --region us-east-1 \
  --parameter-overrides Env=test EnableReplication=true
```

Con `EnableReplication=false` basta el paso 2.

## Pendiente

- **No se ha desplegado en el Learner Lab.** `cfn-lint` pasa sin errores, nada más.
- **Replicación S3 y Global Tables:** el Lab puede bloquearlos. Por eso ambos arrancan
  apagados. LabRole debe poder replicar a la réplica; si no, la replicación falla al crear.
- **Cambiar `EnableGlobalTables` después del primer despliegue reemplaza Alerts e Incidents**
  (y se pierden los datos). Decidirlo antes de cargar nada.
- **Nombres frente al contrato:** el contrato dice `arquifusion-data`; uso `af-{env}-data-{cuenta}`
  por el prefijo `af-` y porque los buckets son únicos globalmente. Es la propuesta 3 de la
  sección 12 del contrato, aún sin aceptar.
- **Replicación de tablas catálogo:** solo Alerts e Incidents entran en Global Tables.
- **TTL en Telemetry:** DynamoDB borra con retraso (horas); no usar `ttl` como filtro exacto.
- Falta publicar los índices (GSI) en SSM; hoy se conocen por nombre fijo.
- El stack raíz (`infra/root`) todavía no anida este template.
