# Parte A - API REST /v1, eventos y notificaciones (IA-3)

Archivos de esta entrega

| Ruta | Qué es |
|---|---|
| `infra/api/template.yaml` | SAM: API Gateway `/v1`, 4 Lambdas (lectura, alertas, callbacks, autorizador) |
| `infra/api/events-template.yaml` | SAM: bus `af-{env}-arquifusion-events`, reglas, topicos SNS, tabla de notificaciones, notificador |
| `part-a/api/app.py` | Lectura (GET), paginación `next_token`, errores 6.8, alcance por rol |
| `part-a/api/alerts.py` | `POST /alerts` (HMAC `timestamp.cuerpo`) |
| `part-a/api/incident_callbacks.py` | `POST /incidents/{id}/ack|start|close` (reanudan SF-1) |
| `part-a/api/authorizer.py` | Autorizador Lambda (JWT HS256 + matriz de roles) |
| `part-a/api/http_utils.py` | Errores, respuestas, roles (compartido) |
| `part-a/api/mint_token.py` | Genera JWT de prueba/demo |
| `part-a/events/notifier.py` | Notificaciones por rol (tabla 6.9) y resúmenes |
| `part-a/api/test_api.py` | 73 pruebas unitarias (sin AWS) |

## Endpoints (prefijo `/v1`)

| Método y ruta | Roles | Notas |
|---|---|---|
| `GET /machines` | todos | `status`, `zone_id`, `limit`, `next_token` |
| `GET /machines/{id}` | todos | incluye `last_reading` |
| `GET /machines/{id}/telemetry` | todos | `from`, `to` (ISO UTC), `metrics` (coma), `order` (asc/desc), `limit` ≤ 1000 |
| `GET /machines/{id}/incidents` | todos | `status` |
| `GET /alerts` | todos | `severity`, `status`, `zone_id`, `machine_id` (más recientes primero) |
| `GET /technicians` | maintenance, supervisor, admin, management | `zone_id`, `availability`, `skill` |
| `GET /incidents` | todos | `status`, `machine_id`, `zone_id` |
| `GET /inventory` | technician, maintenance, supervisor, admin, management | `zone_id`, `part_id`, `below_min=true` |
| `GET /notifications` | todos (solo su rol; `admin` lee cualquiera) | `role`, `since` (ISO UTC) |
| `POST /alerts` | firma HMAC (sin JWT) | 201 creada, 200 duplicada/republicada |
| `POST /incidents/{id}/ack` | technician, maintenance, supervisor, admin | reanuda SF-1 |
| `POST /incidents/{id}/start` | idem | `assigned` → `in_progress` |
| `POST /incidents/{id}/close` | idem | cuerpo: `actions[]`, `parts_replaced[{part_id, qty}]` |

Listas: `{"items": [...], "next_token": "..." | null}`. `limit` por defecto 50 (máx. 200; telemetría 100/1000).
El `next_token` es opaco y está atado a los filtros: si cambian, responde 400.
Errores (6.8): `{"error": {"code", "message", "details", "request_id"}}` con códigos
`VALIDATION_ERROR 400`, `UNAUTHORIZED 401`, `FORBIDDEN 403`, `NOT_FOUND 404`, `CONFLICT 409`,
`RATE_LIMITED 429`, `INTERNAL_ERROR 500`.

## Probar con curl

```bash
API=$(aws ssm get-parameter --name /arquifusion/dev/api/base-url --query Parameter.Value --output text)

# token de prueba (usa el secreto arquifusion/dev/jwt-signing)
TOKEN=$(python part-a/api/mint_token.py --env dev --role maintenance --sub maria --zone ZONA-NORTE-02)

curl -s "$API/machines?limit=5" -H "Authorization: Bearer $TOKEN"
curl -s "$API/alerts?zone_id=ZONA-NORTE-02&severity=high&limit=2" -H "Authorization: Bearer $TOKEN"
# siguiente página
curl -s "$API/alerts?zone_id=ZONA-NORTE-02&severity=high&limit=2&next_token=<token>" -H "Authorization: Bearer $TOKEN"
curl -s "$API/machines/MCH-000123/telemetry?from=2026-11-14T00:00:00Z&metrics=engine_temp_c,vibration_mm_s" -H "Authorization: Bearer $TOKEN"
curl -s "$API/inventory?zone_id=ZONA-NORTE-02&below_min=true" -H "Authorization: Bearer $TOKEN"

# POST /alerts firmado: HMAC-SHA256 hex sobre "<timestamp>.<cuerpo>"
BODY=$(cat contract/examples/alert.json)
TS=$(date +%s)
SECRET=$(aws secretsmanager get-secret-value --secret-id arquifusion/dev/alert-hmac --query SecretString --output text)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
curl -s -X POST "$API/alerts" -H "Content-Type: application/json" \
  -H "x-timestamp: $TS" -H "x-signature: $SIG" -d "$BODY"

# callbacks (como técnico)
TECH=$(python part-a/api/mint_token.py --env dev --role technician --sub TEC-0007 --technician-id TEC-0007 --zone ZONA-NORTE-02)
curl -s -X POST "$API/incidents/INC-000045/ack" -H "Authorization: Bearer $TECH"
curl -s -X POST "$API/incidents/INC-000045/close" -H "Authorization: Bearer $TECH" \
  -d '{"actions":["cambio de bomba"],"parts_replaced":[{"part_id":"PRT-0031","qty":1}]}'
```

Si `x-signature` lleva un prefijo (por ejemplo `sha256=`), lo maneja `shared/hmac_utils.py`.

## Pruebas

```bash
cd part-a && python -m unittest api.test_api -v      # sin dependencias
```

Si `shared/hmac_utils.py` no está en el repo, las pruebas usan una implementación equivalente y lo indican con `USING_FAKE_HMAC`.

## Despliegue (Learner Lab: LabRole, `us-east-1` o `us-west-2`)

```bash
sam build  -t infra/api/events-template.yaml && sam deploy -t infra/api/events-template.yaml \
   --stack-name af-dev-events --parameter-overrides Env=dev --region us-east-1 --resolve-s3
sam build  -t infra/api/template.yaml && sam deploy -t infra/api/template.yaml \
   --stack-name af-dev-api --parameter-overrides Env=dev --region us-east-1 --resolve-s3
```

Orden: root/persistencia (IA-2) → `events-template` → `template`. Hay que volver a desplegar `template.yaml`
si cambia un valor en SSM, porque los nombres se resuelven al desplegar.

## Variables de entorno (las pone SAM)

- Todas: `ENV`, `LOG_LEVEL`, `ALLOWED_ORIGIN`.
- `api-read`: `TABLE_MACHINES|ALERTS|INCIDENTS|TECHNICIANS|TELEMETRY|INVENTORY|NOTIFICATIONS` (de SSM),
  `ALERTS_ZONE_INDEX=zone_id-created_at-index`, `ALERTS_SEVERITY_INDEX=severity-created_at-index`, `INCIDENTS_STATUS_INDEX=status-opened_at-index`,
  `MACHINES_BY_ZONE_INDEX` (opcional), `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE`, `TELEMETRY_DEFAULT_PAGE_SIZE`, `TELEMETRY_MAX_PAGE_SIZE`.
- `api-alerts`: `TABLE_ALERTS`, `EVENT_BUS_NAME` (SSM), `HMAC_SECRET_ID=arquifusion/{env}/alert-hmac`, `HMAC_MAX_SKEW_SECONDS=300`.
- `api-callbacks`: `TABLE_INCIDENTS`, `INCIDENTS_BY_ID_INDEX=incident_id-index`.
- `api-authorizer`: `JWT_SECRET_ID=arquifusion/{env}/jwt-signing`.
- `notifier`: `TABLE_NOTIFICATIONS`, `DIGEST_INDEX`, `TOPIC_{ROL}`, `SES_SENDER` (vacío = sin correo), `SSM_PREFIX`,
  `MASSIVE_MIN_MACHINES`, `NOTIFICATION_TTL_DAYS`.

**SSM que se lee** (`/arquifusion/{env}/`): `ddb/{machines,alerts,incidents,technicians,telemetry,inventory}-table`, `events/bus-name`.
**SSM que se publica**: `api/base-url`, `events/{bus-name,bus-arn,dlq-arn}`, `ddb/notifications-table`, `sns/{rol}-topic-arn`.
**Secrets Manager**: `arquifusion/{env}/alert-hmac`, `arquifusion/{env}/jwt-signing`.
**Opcional (correo)**: SSM `notify/email/{rol}` (lista separada por comas) + `SesSenderEmail` verificado en SES.

JWT: HS256, claims `sub`, `role`, `exp` (obligatorios), `zone_id`, `machine_ids[]`, `technician_id` (opcionales).
Roles: `operator`, `technician`, `maintenance`, `supervisor`, `admin`, `management`.

## Cambios al contrato (aprobados ya)

Códigos `UNAUTHORIZED` (401) y `FORBIDDEN` (403); secreto `jwt-signing`; claves SSM `ddb/*` y `events/*`; bus `af-{env}-arquifusion-events`.

## Propuestas nuevas, por confirmar

1. **Persistencia (IA-2) - índices y atributos que la API da por hechos:** Alerts: GSI `zone_id-created_at-index` (zone_id, created_at) y
   `severity-created_at-index` (severity, created_at); atributos `status` (open/acknowledged/resolved) y `event_published`.
   Incidents: GSI `status-opened_at-index` (status, opened_at) y `incident_id-index` (incident_id); atributo `zone_id` desnormalizado.
   Tablas Telemetry e Inventory publicadas en SSM como `ddb/telemetry-table` y `ddb/inventory-table`.
2. **Incidentes (C, SF-1):** guarda `ack_task_token` y `close_task_token` en el item; `/close` NO pone `closed`
   (lo hace SF-1 tras descontar repuestos); atributos `acknowledged_at`, `started_at`, `close_requested_at`.
3. **Notificaciones:** tabla nueva `af-{env}-notifications` (la crea `events-template.yaml`) y endpoint `GET /notifications`.
4. **Roles en el JWT:** códigos en inglés (arriba) para los 6 roles de la tabla 6.9.
5. **`shared/hmac_utils.py` (IA-1):** se asume `verify_signature(secret, timestamp, body, signature) -> bool`
   (o `verify_hmac` / `verify`). Si el nombre o los argumentos son otros, hay que ajustar `_verify` en `alerts.py` (una función).
   `x-timestamp` se acepta en epoch (s o ms) o ISO 8601.
6. **SF-3 vs notificador:** el notificador ya aplica las reglas 6.9 y envía por SNS/SES. Para no duplicar avisos,
   propongo que SF-3 no se suscriba a `alert.created` (o que lo llame a él). Por eso `IncidentWorkflowArn` solo conecta SF-1.
7. **Sin severidad coherente:** si `severity` no coincide con la banda de `score` (6.5) se acepta y se deja un warning en logs.

## Qué quedó pendiente

- `POST/PATCH /incidents` y `assignment.confirmed` (no asignados a esta entrega).
- No hay forma de emitir JWT en producción: `mint_token.py` sirve para pruebas y demo (no hay login).
- Teléfonos de técnicos: SNS publica por tópico; el SMS directo necesita un campo de teléfono en Technicians.
- `LabRole` debe confiar en `events.amazonaws.com` para que la regla dispare SF-1 (por verificar en el Lab). Si no, hace falta una Lambda arrancadora.
- Cognito no se usa; si el Lab lo permite se puede cambiar el autorizador.
- SES en modo sandbox: solo correos a direcciones verificadas.
- Reglas de la tabla 6.9 de resúmenes por turno/día/semana: horarios fijados en UTC (Lima = UTC-5); ajustar en `events-template.yaml`.
- Sin `sam validate`/`cfn-lint` ni despliegue real en este entorno (solo se validó la sintaxis YAML y las pruebas unitarias).
- Paginación de `GET /machines` con `scan` (cómodo hasta unos miles de máquinas); con `MACHINES_BY_ZONE_INDEX` se consulta por zona.
