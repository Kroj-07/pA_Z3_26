# Runner de carga y caos (IA-5)

Solo biblioteca estándar (el caos usa `boto3` al ejecutarse de verdad).

## Carga: `load_test.py`
```
python load_test.py --profile 100 --dry-run                         # sin red
export AF_INGEST_URL=https://<api-id>.execute-api.us-east-1.amazonaws.com/v1/telemetry
export AF_JWT=...                 # opcional: Authorization: Bearer (ver part-a/api/mint_token.py)
export AF_HMAC_SECRET=...        # opcional: x-timestamp/x-signature; sale de Secrets Manager, nunca del repo
python load_test.py --profile all --duration 60 --report report.json  # 100, 1000 y 5000 máquinas
```
Mensajes según contrato v1.3 §2 (`MCH-000123`, `location.zone_id`, 8 métricas). `--batch N` envía `{"readings":[...]}` con N lecturas por petición.
Cada máquina envía cada `--interval` s (10 por defecto) con desfase propio: 5.000 máquinas ≈ 500 peticiones/s. Si el reporte dice `el cliente se saturó`, la limitación es de tu equipo, no de la nube: sube `--concurrency` o reparte la carga entre varias terminales. Sale con código 1 si el error supera `--max-error-rate` (1 %) o el p95 supera `--max-p95-ms` (1000).

## Caos: `chaos_sqs.py`
Deshabilita el event source mapping 60 s, mide cola y antigüedad del mensaje más viejo, lo rehabilita (siempre, en `finally`) y mide cuánto tarda en vaciarse.
```
python chaos_sqs.py --dry-run --function-name <lambda> --queue-url <url>
python chaos_sqs.py --env test --region us-east-1 --duration 60 --report chaos.json
# en otra terminal, a la vez:  python load_test.py --profile 1000 --duration 150
```
Resultado esperado: sube `af-queue-age`, la cola se acumula y tras reactivar se vacía sin mensajes en la DLQ.

## Probar
`python -m unittest discover -s load/test/runner -v` (10 pruebas offline: servidor local y clientes simulados).

## Supuestos a confirmar contra el contrato
La autenticación de `POST /v1/telemetry` (el contrato no la fija: JWT y/o HMAC son opcionales aquí) y SSM `/arquifusion/{env}/ingest/consumer-function-name` y `ingest/queue-url`.

## Pendiente
No se corrió contra AWS real (sin red ni credenciales donde se escribió).
