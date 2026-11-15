# ArquiFusion Fleet Intelligence — Contrato técnico (v1.3)

Fuente de verdad entre las tres partes (A, B, C) y las IAs que construyen la Parte A.
Es la transcripción de la sección 6 del "Plan ganador v3" con los ajustes aprobados por el equipo.

**Cómo se cambia:** se propone por escrito. El cambio vale solo cuando los tres lo aceptan y se actualiza este archivo. Agregar un campo opcional se puede hacer avisando. Cambiar nombre, tipo o significado de un campo, o quitarlo, sube `schema_version`.

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
*(Mismo JSON del PDF)*

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

status admite: operating, idle, stopped, maintenance, fault.

POST /v1/telemetry acepta una lectura o un lote con el campo readings (arreglo).

Un mensaje inválido se rechaza con el error de la sección 7 y no detiene el lote.

3. Catálogos (A es dueño)
(Mismos campos del PDF)

4. Evento de alerta (B produce, A guarda y notifica, C muestra)
(Mismo JSON del PDF)

5. Escala de severidad
(Misma tabla del PDF)

6. API REST (prefijo /v1)
Método y ruta	Dueño	Descripción
POST /telemetry	A (IA-2)	Ingesta de una lectura o un lote (readings)
GET /machines?zone_id&status	A (IA-3)	Lista de máquinas con estado y última posición
GET /machines/{id}	A (IA-3)	Detalle de una máquina y su última lectura
GET /machines/{id}/telemetry?from&to&metrics	A (IA-3)	Serie de tiempo, paginada, máximo 1.000 puntos por respuesta
GET /machines/{id}/incidents	A (IA-3)	Historial de incidentes de la máquina
POST /incidents, PATCH /incidents/{id}	A (IA-3)	Crear y actualizar incidentes (cierre, intervenciones, repuestos)
GET /alerts?severity&zone_id&status	A (IA-3)	Alertas guardadas (las produce B)
POST /alerts	A (IA-3)	Recibe alertas de B (verifica HMAC)
GET /technicians, GET /inventory?zone_id	A (IA-3)	Técnicos con disponibilidad y stock por zona
GET /notifications?role&since	A (IA-3)	Notificaciones generadas por rol
POST /incidents/{id}/ack, /start, /close	C (los expone A)	Callbacks de SF-1; cada uno devuelve el estado actualizado
GET /predictions/risk?limit	B	Máquinas ordenadas por riesgo, con score y componente sospechoso
GET /predictions/maintenance	B	Fecha u horas recomendadas de mantenimiento por máquina
GET /analytics/fuel?machine_id	B	Consumo normalizado, comparación con pares, deriva
GET /analytics/utilization	B	KPIs de uso y clasificación
GET /analytics/spare-parts?zone_id	B	Demanda esperada, stock mínimo sugerido, consumo irregular
GET /analytics/common-cause	B	Grupos de máquinas con causa común probable
GET /ai/explain?alert_id&role	B	Devuelve role, text, model y grounded_on[] (IDs usados)
POST /ai/ask	B	Recibe question y role; devuelve answer y sources[]
POST /assignments/suggest	C	Recibe incident_ids y devuelve asignaciones sugeridas (sección 9)
POST /assignments/confirm	C	Confirma asignación; actualiza incidente y técnico vía API de A
Toda respuesta paginada devuelve next_token (o null).

7. Formato de error (uniforme)
json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "El campo metrics.engine_temp_c debe ser numérico",
    "details": { "machine_id": "MCH-000123", "field": "metrics.engine_temp_c" },
    "request_id": "req-9f2c..."
  }
}
Códigos: VALIDATION_ERROR (400), UNAUTHORIZED (401), FORBIDDEN (403), NOT_FOUND (404), CONFLICT (409), RATE_LIMITED (429), INTERNAL_ERROR (500).

8. Eventos y almacenamiento
EventBridge: af-{env}-arquifusion-events (publicado en SSM /arquifusion/{env}/events/bus-name). Eventos: alert.created (B publica; A guarda y notifica), incident.opened e incident.closed (A publica; B y C escuchan), assignment.confirmed (C publica; A notifica).

DynamoDB: Los nombres de las tablas se publican en SSM bajo /arquifusion/{env}/ddb/. Tablas: Machines, Telemetry (TTL 30 días), Alerts, Incidents, Technicians, Inventory.

S3: s3://af-{env}-arquifusion-data/telemetry/... (Parquet); /analytics/; /exports/.

Permisos: B solo lee telemetry y escribe analytics y alertas; C solo lee, salvo assignments; A es dueño de todas las tablas.

9. Asignación de técnicos (C)
(Mismo JSON del PDF)

10. Reglas de comunicación por rol
(Misma tabla del PDF)

11. Extensiones multi-nube y Step Functions
Tema	Regla
Telemetría a Google Cloud	La Lambda puente publica en el tópico Pub/Sub telemetry-raw el mismo JSON de la sección 2 más ingested_at.
Tablas BigQuery (dataset fleet)	telemetry, machines, technicians, incidents, alerts_out, model_runs.
Alertas de Google Cloud a AWS	POST /v1/alerts con cabeceras x-timestamp y x-signature. Se firma "{x-timestamp}.{cuerpo_crudo}" con HMAC-SHA256. Secreto: arquifusion/{env}/alert-hmac.
Secretos	En Secrets Manager: arquifusion/{env}/alert-hmac, arquifusion/{env}/jwt-signing (para el Lambda Authorizer de A), arquifusion/{env}/azure-sas.
Entornos	shared (compartido) y test (pruebas).
Autenticación de usuarios	Lambda Authorizer con JWT HS256 (claim role). La app de C envía Authorization: Bearer <jwt>.
Respaldo a Azure (A10)	SF-2 o Lambda copia a azure://af-backup/....
Callbacks de SF-1	POST /v1/incidents/{id}/ack, /start y /close (expuestos por A, orquestados por C).
Idempotencia	Clave natural (alert_id, incident_id, machine_id+timestamp).
12. Pendientes de acuerdo
Aceptación de B del cambio del secreto HMAC (firmar timestamp.cuerpo y usar arquifusion/{env}/alert-hmac). Ya fue comunicado.

Aceptación de C del uso de Lambda Authorizer (JWT) en lugar de Cognito para la app web.

Verificar que la cuenta sea Learner Lab (lo hace IA-1).

text

***

### ¿Qué haces con esto?
1. Copia todo el bloque de arriba.
2. En VS Code, abre tu archivo `contract/README.md` y **reemplaza todo** con esto.
3. Guarda. Ve a la terminal y haz commit en la rama `main`:
   ```bash
   git checkout main
   git add contract/README.md
   git commit -m "Contrato v1.3: Ajustes finales aprobados por el equipo"
   git push origin main
Pásale este nuevo contrato a todas las ramas de las IAs:

bash
git checkout ia2-ingesta && git merge main && git push origin ia2-ingesta
git checkout ia3-api && git merge main && git push origin ia3-api
git checkout ia4-puente && git merge main && git push origin ia4-puente
git checkout ia5-sf2 && git merge main && git push origin ia5-sf2
git checkout main