# Contrato de Interfaces - ArquiFusion Fleet Intelligence
Este es el documento oficial. Ninguna IA puede cambiar esto sin aprobación.

# ArquiFusion Fleet Intelligence — Contrato técnico (v1.2)

Fuente de verdad entre las tres partes (A, B, C) y las IAs que construyen la Parte A.
Es la transcripción de la sección 6 del "Plan ganador v3". El PDF es un documento
interno y no se publica; este archivo es el que vale en el repositorio.

**Cómo se cambia:** se propone por escrito. El cambio vale solo cuando los tres lo
aceptan y se actualiza este archivo. Agregar un campo opcional se puede hacer
avisando. Cambiar nombre, tipo o significado de un campo, o quitarlo, sube
`schema_version`.

## 1. Convenciones generales

| Tema | Regla |
|---|---|
| Nombres | `snake_case` en campos JSON. IDs en mayúsculas con prefijo: `MCH-000123` (máquina), `TEC-0007` (técnico), `INC-000045` (incidente), `ALT-000812` (alerta), `PRT-0031` (repuesto), `ZONA-NORTE-02` (zona) |
| Fechas | ISO 8601 en UTC con `Z` (ej. `2026-11-14T15:04:05Z`). La interfaz convierte a hora de Lima (UTC-5) |
| Unidades | Temperatura °C, vibración mm/s, presión bar, combustible L/h, carga kg, velocidad km/h, RPM, horas en horas decimales, coordenadas WGS84 (lat, lon) |
| Versionado | Cada mensaje lleva `schema_version` (inicia en `"1.0"`). Cambios que rompen compatibilidad suben la versión mayor y se avisa. Rutas de la API bajo `/v1` |
| Valores faltantes | Se envía `null`, nunca cadena vacía ni `0` como "sin dato" |
| Idioma | Campos y enumeraciones en inglés; textos para humanos (resumen, razón) en español |

## 2. Mensaje de telemetría (A recibe, B consume)

```json
{
  "schema_version": "1.0",
  "machine_id": "MCH-000123",
  "timestamp": "2026-11-14T15:04:05Z",
  "location": { "lat": -12.0464, "lon": -77.0428, "zone_id": "ZONA-NORTE-02" },
  "status": "operating",
  "metrics": {
    "engine_temp_c": 92.4,
    "vibration_mm_s": 4.8,
    "pressure_bar": 6.1,
    "fuel_rate_lph": 31.2,
    "load_kg": 12400,
    "speed_kmh": 18.5,
    "engine_rpm": 1850,
    "operating_hours_total": 3240.7
  }
}
```

- `status` admite: `operating`, `idle`, `stopped`, `maintenance`, `fault`.
- `POST /v1/telemetry` acepta una lectura o un lote con el campo `readings` (arreglo).
- Un mensaje inválido se rechaza con el error de la sección 7 y **no detiene el lote**.

## 3. Catálogos (A es dueño)

| Entidad | Campos mínimos |
|---|---|
| Machine | `machine_id`, `model`, `type` (excavator, truck, loader...), `sector`, `zone_id`, `status`, `last_seen`, `operating_hours_total`, `last_maintenance_at` |
| Technician | `technician_id`, `name`, `skills[]` (hydraulics, engine, electrical...), `zone_id`, `location{lat,lon}`, `availability` (available, busy, off_shift), `shift_end` |
| SparePart | `part_id`, `name`, `component`, `compatible_models[]` |
| Inventory | `zone_id`, `part_id`, `stock`, `min_stock`, `updated_at` |
| Incident | `incident_id`, `machine_id`, `opened_at`, `closed_at`, `status` (open, assigned, in_progress, closed), `severity`, `downtime_minutes`, `technician_id`, `actions[]`, `parts_replaced[{part_id, qty}]`, `recurrence_of`, `alert_ids[]` |

## 4. Evento de alerta (B produce, A guarda y notifica, C muestra)

```json
{
  "schema_version": "1.0",
  "alert_id": "ALT-000812",
  "machine_id": "MCH-000123",
  "zone_id": "ZONA-NORTE-02",
  "created_at": "2026-11-14T15:10:00Z",
  "type": "predictive_failure",
  "severity": "high",
  "score": 82,
  "confidence": 0.78,
  "summary": "Temperatura y vibración en aumento sostenido durante 3 días",
  "suspected_component": "cooling_system",
  "recommended_action": "inspect_within_24h",
  "evidence": [
    { "metric": "engine_temp_c", "observed": 104.2, "expected": 91.0, "window": "6h", "trend": "rising" },
    { "metric": "vibration_mm_s", "observed": 7.9, "expected": 4.6, "window": "6h", "trend": "rising" }
  ],
  "related_machine_ids": []
}
```

- `type` admite: `predictive_failure`, `anomaly`, `fuel_drift`, `low_utilization`, `overuse`, `common_cause`, `maintenance_due`.
- `recommended_action` admite: `monitor`, `inspect_within_72h`, `inspect_within_24h`, `stop_and_inspect_now`.
- En `common_cause`, `related_machine_ids` lista las máquinas del grupo y se agrega `common_factor` (zone, fuel_batch, component...).

## 5. Escala de severidad (común a todos)

| severity | score | Significado | Acción esperada |
|---|---|---|---|
| info | 0-24 | Dato curioso, sin riesgo | Solo se registra |
| low | 25-49 | Desviación menor | Se muestra en el panel; sin aviso |
| medium | 50-74 | Posible problema en desarrollo | Aviso a mantenimiento; revisar en 72 h |
| high | 75-89 | Riesgo alto de falla | Aviso inmediato; sugerir asignación de técnico |
| critical | 90-100 | Falla inminente o en curso | Aviso a todos los roles relevantes; detener y revisar ya |

## 6. API REST (prefijo `/v1`)

| Método y ruta | Dueño | Descripción |
|---|---|---|
| `POST /telemetry` | A | Ingesta de una lectura o un lote (`readings`) |
| `GET /machines?zone_id&status` | A | Lista de máquinas con estado y última posición |
| `GET /machines/{id}` | A | Detalle de una máquina y su última lectura |
| `GET /machines/{id}/telemetry?from&to&metrics` | A | Serie de tiempo, paginada, máximo 1.000 puntos por respuesta |
| `GET /machines/{id}/incidents` | A | Historial de incidentes de la máquina |
| `POST /incidents`, `PATCH /incidents/{id}` | A | Crear y actualizar incidentes (cierre, intervenciones, repuestos) |
| `GET /alerts?severity&zone_id&status` | A | Alertas guardadas (las produce B) |
| `POST /alerts` | A | Recibe alertas de B (también por evento) |
| `GET /technicians`, `GET /inventory?zone_id` | A | Técnicos con disponibilidad y stock por zona |
| `GET /notifications?role&since` | A | Notificaciones generadas por rol |
| `POST /incidents/{id}/ack`, `/start`, `/close` | C (los expone A) | Callbacks de SF-1; cada uno devuelve el estado actualizado |
| `GET /predictions/risk?limit` | B | Máquinas ordenadas por riesgo, con score y componente sospechoso |
| `GET /predictions/maintenance` | B | Fecha u horas recomendadas de mantenimiento por máquina |
| `GET /analytics/fuel?machine_id` | B | Consumo normalizado, comparación con pares, deriva |
| `GET /analytics/utilization` | B | KPIs de uso y clasificación |
| `GET /analytics/spare-parts?zone_id` | B | Demanda esperada, stock mínimo sugerido, consumo irregular |
| `GET /analytics/common-cause` | B | Grupos de máquinas con causa común probable |
| `GET /ai/explain?alert_id&role` | B | Devuelve `role`, `text`, `model` y `grounded_on[]` (IDs usados) |
| `POST /ai/ask` | B | Recibe `question` y `role`; devuelve `answer` y `sources[]` |
| `POST /assignments/suggest` | C | Recibe `incident_ids` y devuelve asignaciones sugeridas (sección 9) |
| `POST /assignments/confirm` | C | Confirma asignación; actualiza incidente y técnico vía API de A |

Toda respuesta paginada devuelve `next_token` (o `null`).

## 7. Formato de error (uniforme)

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "El campo metrics.engine_temp_c debe ser numérico",
    "details": { "machine_id": "MCH-000123", "field": "metrics.engine_temp_c" },
    "request_id": "req-9f2c..."
  }
}
```

Códigos: `VALIDATION_ERROR` (400), `NOT_FOUND` (404), `CONFLICT` (409), `RATE_LIMITED` (429), `INTERNAL_ERROR` (500).

## 8. Eventos y almacenamiento

- **EventBridge** `arquifusion-events`: `alert.created` (B publica; A guarda y notifica), `incident.opened` e `incident.closed` (A publica; B y C escuchan), `assignment.confirmed` (C publica; A notifica).
- **DynamoDB:** Machines (PK `machine_id`); Telemetry (PK `machine_id`, SK `timestamp`; TTL 30 días); Alerts (PK `machine_id`, SK `created_at`; GSI `zone_id+created_at` y `severity+created_at`); Incidents (PK `machine_id`, SK `opened_at`; GSI por `status`); Technicians (PK `technician_id`); Inventory (PK `zone_id`, SK `part_id`).
- **S3:** `s3://arquifusion-data/telemetry/year=YYYY/month=MM/day=DD/zone=ZONA-X/` (Parquet); `/analytics/` (resultados de B); `/exports/` (CSV para el jurado).
- **Permisos:** B solo lee telemetry y escribe analytics y alertas; C solo lee, salvo assignments; A es dueño de todas las tablas. Donde difiera de la sección 11, prevalece la sección 11.

## 9. Asignación de técnicos (C)

`POST /v1/assignments/suggest`

```json
{ "incident_ids": ["INC-000045", "INC-000046"] }
```

`200 OK`

```json
{
  "generated_at": "2026-11-14T15:20:00Z",
  "assignments": [
    { "incident_id": "INC-000045", "technician_id": "TEC-0007", "eta_minutes": 35,
      "score": 0.91, "reason": "Especialista en sistema de enfriamiento, a 4 km, disponible" }
  ],
  "unassigned": [
    { "incident_id": "INC-000046", "reason": "Sin técnicos disponibles con la especialidad requerida" }
  ]
}
```

## 10. Reglas de comunicación por rol (A implementa, C muestra)

| Rol | Qué recibe | Urgencia / canal |
|---|---|---|
| Operador | Alertas de su propia máquina con medium o más, en lenguaje simple y acción inmediata | Inmediato; app o SMS |
| Técnico | Incidentes asignados con evidencia, componente sospechoso, repuestos y ubicación | Inmediato; app o SMS |
| Responsable de mantenimiento | Alertas medium o más de su zona, cola de incidentes y sugerencias de asignación | Inmediato para high y critical; resumen por hora para el resto |
| Supervisor | Resumen de zona: máquinas detenidas, incidentes abiertos, causa común | high y critical inmediato; resumen cada turno |
| Administrador de operación | KPIs de disponibilidad, productividad, combustible; repuestos críticos | critical inmediato; resumen diario |
| Gerencia | KPIs de la empresa, tendencias y costos de inactividad | Solo critical masivo; resumen semanal |

## 11. Extensiones multi-nube y Step Functions (v1.2)

Estas reglas se suman a las anteriores. Cambio que exige aceptación explícita de B:
el secreto HMAC también vive en el Secret Manager de Google y Cloud Run ya no asume un rol de AWS.

| Tema | Regla |
|---|---|
| Telemetría a Google Cloud | La Lambda puente publica en el tópico Pub/Sub `telemetry-raw` el mismo JSON de la sección 2 más `ingested_at`. Una suscripción de Pub/Sub a BigQuery escribe en `fleet.telemetry` |
| Tablas BigQuery (dataset `fleet`) | `telemetry` (particionada por `DATE(timestamp)`, agrupada por `machine_id`; `metrics` aplanado), `machines`, `technicians`, `incidents` (sincronizadas por SF-2), `alerts_out`, `model_runs` (`model`, `trained_at`, `precision`, `recall`, `mean_lead_time_h`, `promoted`) |
| Alertas de Google Cloud a AWS | `POST /v1/alerts` con cabeceras `x-timestamp` y `x-signature` (HMAC-SHA256 del cuerpo con el secreto compartido `arquifusion/alert-hmac`, guardado en AWS Secrets Manager para verificar y en el Secret Manager de Google para firmar). Se rechaza si la marca de tiempo tiene más de 5 minutos. Idempotente por `alert_id` |
| Secretos de Cloud Run | Lee el secreto HMAC y sus credenciales de su propio Secret Manager de Google; ya no asume roles de AWS. Ningún valor se sube al repositorio |
| Callbacks de SF-1 | SF-1 guarda el token en el ítem del incidente. Endpoints `POST /v1/incidents/{id}/ack`, `/start` y `/close` (dueño C; los expone la API de A) |
| Nombres y secretos | Secretos en Secrets Manager con nombre `arquifusion/{env}/{nombre}`; configuración no secreta en SSM bajo `/arquifusion/{env}/`. Prefijo `af-` en recursos. Un solo entorno compartido más uno de pruebas |
| Respaldo a Azure (A10) | SF-2 o una Lambda programada copia a Azure Blob el Parquet nuevo y un volcado de catálogos, en `azure://af-backup/{tabla}/year=YYYY/month=MM/day=DD/`. Credencial: token SAS con expiración y solo escritura en el secreto `arquifusion/{env}/azure-sas`. Inmutabilidad activada si el plan lo permite |
| Credenciales del Learner Lab | Rotan: ningún componente fuera de AWS guarda claves de AWS. Los servicios de AWS usan `LabRole`. Si algo externo necesita llegar a AWS, llama a la API REST con firma HMAC |
| Idempotencia | Toda escritura usa clave natural (`alert_id`, `incident_id`, `machine_id+timestamp`). Reenviar nunca duplica |
| Objetivos medibles | Latencia p95 de ingesta, tiempo de la falla inyectada a la notificación, pérdida de datos en la prueba de caos igual a 0, costo por máquina al mes. Cada parte reporta sus números a A |
| Repositorio | `/contract`, `/part-a`, `/part-b`, `/part-c`, `/infra`, `/load-tests`. README mínimo. Prohibido subir claves; escaneo de secretos activo |

## 12. Pendientes de acuerdo (propuestas, NO vigentes hasta que acepten A, B y C)

1. **Qué cubre la firma HMAC.** La sección 11 dice "HMAC-SHA256 del cuerpo". Si solo se firma el cuerpo, `x-timestamp` no está autenticado y la ventana de 5 minutos no protege contra repetición. Propuesta: firmar `"{x-timestamp}.{cuerpo_crudo}"`. Requiere que B lo implemente igual en Cloud Run.
2. **Nombre del secreto HMAC.** La sección 11 usa `arquifusion/alert-hmac` y también la convención `arquifusion/{env}/{nombre}`. Propuesta: `arquifusion/{env}/alert-hmac`.
3. **Nombres vs prefijo `af-`.** La sección 8 nombra `arquifusion-data` y `arquifusion-events`, y la 11 exige prefijo `af-`. Propuesta: `af-{env}-...` para todo, con los nombres publicados en SSM `/arquifusion/{env}/` (los buckets S3 son únicos globalmente).
4. **Nombres de los entornos.** El contrato dice "uno compartido más uno de pruebas" sin nombrarlos.
5. **Ubicación de los mocks.** Las reglas dicen `/contract/examples`; el repo tiene `/examples` en la raíz. Tampoco coinciden `/load-tests` (repo: `/load`) ni `/part-b` y `/part-c` (aún no existen).
6. **Aceptación de B** del cambio del secreto HMAC (ya figura en STATUS.md).
7. **Verificar que la cuenta sea Learner Lab** (lo hace IA-1).