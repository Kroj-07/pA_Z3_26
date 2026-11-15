# STATUS — ArquiFusion Fleet Intelligence (Parte A)

Última actualización: 2026-11-15

| Frente | Hecho | En curso | Bloqueado por |
|---|---|---|---|
| Fundación (raíz, seguridad, scripts, CI) | Raíz anida los 8 stacks; secretos y SSM; `deploy.sh`, `destroy.sh`, `set_secrets.sh`, `smoke_test.sh`, `check_templates.py`, CI | — | Despliegue real en el Lab |
| Ingesta | Código con pruebas; plantilla API HTTP → SQS+DLQ → Lambda | — | Confirmar integración directa API Gateway → SQS con LabRole |
| Datos | 6 tablas, S3, Global Tables, stream de Telemetry, índice `incident_id-index` | — | — |
| API | Endpoints, autorizador (REST y HTTP), alertas HMAC, callbacks; 77 pruebas | — | Aceptación de B (HMAC) y C (JWT) |
| Puente y respaldo | Código y plantilla; fuente = stream de Telemetry | — | Clave GCP, SAS de Azure, proyecto de B |
| SF-2 | Máquina de estados con mocks | Versión real de `sync_bigquery` | Tablas BigQuery de B |

## Decisiones y cambios de contrato
- Contrato v1.3: HMAC sobre `timestamp.cuerpo`, secreto `arquifusion/{env}/alert-hmac`; JWT HS256 en vez de Cognito.
- SSM de tablas bajo `/data/` (no `/ddb/`); entornos `test` y `shared`; ingesta con su propio API HTTP (JWT `admin`).
- El puente lee el stream de Telemetry (datos ya validados), no la cola cruda.

## Pendientes de confirmar
- Que la cuenta sea Learner Lab (SES, Step Functions, Global Tables, EventBridge Scheduler, `events.amazonaws.com` en LabRole).
- Que B acepte el cambio del secreto HMAC; que C acepte el autorizador JWT.
- `sam validate --lint` y un despliegue real: nada se ha probado contra AWS.
