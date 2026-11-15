# Observabilidad (IA-5)

`template.yaml` (SAM/CloudFormation): tópico SNS `af-{env}-alarms`, 8 alarmas, dashboard `af-{env}-fleet`, regla de muestreo y grupo de X-Ray.
`emf.py`: helper de métricas personalizadas (EMF) sin llamadas a la API. `cost_model.py` + `costs.md`: costo por máquina al mes.

## Alarmas
| Alarma | Qué detecta |
|---|---|
| af-ingest-errors | Lambda de ingesta con errores (2 de 3 min) |
| af-ingest-latency-p95 | p95 de Duration > `IngestLatencyP95Ms` (3 de 5 min) |
| af-ingest-throttles | concurrencia agotada |
| af-queue-age | mensaje más viejo > `QueueAgeSeconds` (la que dispara el caos del consumidor) |
| af-dlq-depth | hay mensajes en la DLQ |
| af-sf2-executions-failed / af-sf2-zones-failed | fallo del pipeline / de zonas (métrica EMF `ZonesFailed`) |

## Lee de SSM
El contrato §8 solo define `/arquifusion/{env}/ddb/` y `/events/bus-name`. **Estos cuatro nombres no están en el contrato: son supuestos, confirmar con IA-2** (o ajustar a lo que publique `infra/`):
`/arquifusion/{env}/ingest/function-name`, `ingest/queue-name`, `ingest/dlq-name`, `sf2/state-machine-arn`.
El despliegue falla si alguno no existe: desplegar después de ingesta y SF-2.

## Parámetros
`Env` (`shared` o `test`), `AlarmEmail` (vacío = sin suscripción), `IngestLatencyP95Ms` (1000), `QueueAgeSeconds` (120), `XRayFixedRate` (0.01).

## Usar EMF
```python
import emf
emf.emit("ArquiFusion/Ingest", {"MessagesAccepted": (n, "Count")}, {"Env": "test"})
```
`part-a/sf2/functions/emf.py` es copia de este archivo: si lo cambias, `cp part-a/observability/emf.py part-a/sf2/functions/emf.py`.

## Probar
```
python part-a/observability/cost_model.py
sam deploy -t part-a/observability/template.yaml --stack-name af-obs-test --region us-east-1 \
  --parameter-overrides AlarmEmail=tu@correo.com --capabilities CAPABILITY_IAM
```

## Pendiente
- Sin alarma de API Gateway (falta el id del API en SSM). Sin métricas EMF de ingesta (son de IA-2: usar `emf.py`).
- `AWS::XRay::SamplingRule/Group` puede estar restringido en Learner Lab; si falla, comentar esos dos recursos (las Lambdas igual envían trazas con `Tracing: Active`).
- No se ejecutó `cfn-lint`. Las referencias `{{resolve:ssm:...}}` dentro del cuerpo del dashboard conviene comprobarlas en el primer despliegue.
