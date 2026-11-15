# part-a/ingest — Ingesta de telemetría (IA-2)

Lambda `app.py` (Python 3.12), disparada por SQS. Cada mensaje trae una lectura o un
lote (`{"readings": [...]}`) con el formato de la sección 2 del contrato.

## Qué hace

Por cada lectura:

1. **Valida** contra el contrato: `schema_version` 1.x, `machine_id` (`MCH-000123`),
   `timestamp` ISO 8601 UTC con `Z`, `location` (`lat`, `lon`, `zone_id`), `status`
   (`operating`, `idle`, `stopped`, `maintenance`, `fault`) y las 8 métricas. Las métricas
   ausentes se guardan como `null` (nunca `0`); los campos desconocidos se ignoran.
2. **Guarda en DynamoDB** (tabla Telemetry) con una escritura condicional: es idempotente por
   `machine_id` + `timestamp`. Un reenvío de otro mensaje se detecta y se omite.
3. **Guarda en S3** un Parquet por partición en
   `telemetry/year=YYYY/month=MM/day=DD/zone=ZONA-X/{messageId}.parquet`. La clave es
   determinista y se escribe con `If-None-Match: *`, así que un reintento no la reescribe.
4. **Actualiza la tabla Machines** (`last_seen`, `status`, `zone_id`, `location`,
   `last_metrics`, `operating_hours_total`) solo si la lectura es más reciente que la guardada.

Errores:

- **Lectura inválida:** se rechaza con el formato de error de la sección 7
  (`VALIDATION_ERROR`), se registra en el log y **no detiene el lote** ni se reintenta.
- **Falla transitoria** (DynamoDB, S3): el mensaje vuelve en `batchItemFailures` y SQS lo
  reintenta; agotados los reintentos va a la DLQ. Reintentar el mismo mensaje es seguro:
  la lectura sigue siendo "suya" y se completa la copia en S3 si faltaba.

## Variables de entorno

Los nombres no se escriben a mano: salen de SSM `/arquifusion/{env}/data/` (los publica
`infra/data/template.yaml`).

| Variable | Obligatoria | Valor (parámetro SSM) |
|---|---|---|
| `TELEMETRY_TABLE` | sí | `/arquifusion/{env}/data/telemetry-table` |
| `MACHINES_TABLE` | sí | `/arquifusion/{env}/data/machines-table` |
| `TELEMETRY_BUCKET` | sí | `/arquifusion/{env}/data/telemetry-bucket` |
| `TTL_DAYS` | no (30) | Días antes de que DynamoDB borre la lectura |
| `LOG_LEVEL` | no (`INFO`) | Nivel de log |

La función no usa secretos. Rol de ejecución: `LabRole`.

## Cómo probar

```bash
cd part-a/ingest
pip install -r requirements.txt
python -m unittest -v
```

49 pruebas, sin red ni credenciales: DynamoDB y S3 se simulan con objetos falsos. Cubren la
validación (campos, tipos, rangos, enum, faltantes), la idempotencia (reenvío, reintento del
mismo mensaje, duplicados dentro de un lote, fallo de S3 a medias), el estado de la máquina,
el TTL, la respuesta parcial por lote y el Parquet real (se omite si no hay `pyarrow`).

Prueba manual con un mock del contrato: `/contract/examples/telemetry.json` como cuerpo de un
mensaje SQS.

## Despliegue (resumen)

- Runtime `python3.12`, handler `app.lambda_handler`, rol `LabRole`.
- Origen de eventos SQS con `FunctionResponseTypes: [ReportBatchItemFailures]` y una DLQ.
- Capa `AWSSDKPandas-Python312` para `pyarrow` (no empaquetarlo en el zip).

## Pendiente

- **Falta la plantilla de despliegue de la Lambda** (función, cola SQS, DLQ, mapeo de eventos).
  No la creé porque la tarea pedía solo el código; irá en `part-a/ingest/template.yaml` y la
  anida `infra/root`. Quién crea la cola y cómo se publica su nombre en SSM está por acordar
  (¿IA-3, con `POST /v1/telemetry`, encola aquí?).
- **Nada se ha desplegado ni probado contra AWS real.** Solo hay pruebas con mocks.
- **boto3 del runtime:** `If-None-Match` en `put_object` exige boto3 >= 1.35.90. Si el runtime de
  Lambda trae uno anterior, hay que empaquetar boto3 o quitar la condición (queda la idempotencia
  de DynamoDB, que es la que manda).
- **Reglas que no están en el contrato** (conviene confirmarlas): `machine_id` = `MCH-` + 1 a 9
  dígitos; `zone_id` = `ZONA-` + mayúsculas/dígitos; el `timestamp` no admite fracciones de
  segundo; las métricas, salvo `engine_temp_c`, no pueden ser negativas; máximo 500 lecturas por
  mensaje.
- **Tabla Machines:** guardo `last_seen`, `last_metrics`, `updated_at` además de los campos del
  catálogo. IA-3 (API) debe conocerlos. Una máquina que no esté en el catálogo se crea con solo
  esos campos (sin `model` ni `type`).
- **Sin filtro de antigüedad:** se acepta cualquier `timestamp`, incluso futuro o muy viejo; una
  lectura de hace más de 30 días se guarda con un `ttl` ya vencido.
- **Parquet:** un archivo por mensaje y partición, así que habrá muchos archivos pequeños.
  Compactarlos es trabajo de SF-2.
