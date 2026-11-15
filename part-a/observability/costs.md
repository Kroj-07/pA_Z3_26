# Costos estimados por maquina al mes — ArquiFusion Parte A

Generado con `python part-a/observability/cost_model.py` (cambia `--interval` o `--xray-rate` y se recalcula).
Precios de lista **us-east-1**, en USD, **sin capa gratuita** (estimación conservadora).

## Estado de los precios

- **Verificados en esta sesión** contra fuentes de 2026: Lambda ($0,20 por millón de peticiones, $0,0000166667 por GB-s en x86) y DynamoDB on-demand ($0,625 por millón de escrituras, $0,125 por millón de lecturas, $0,25 por GB-mes de almacenamiento).
- **De referencia, no re-verificados** (precio de lista que conozco): API Gateway HTTP, SQS, Step Functions, CloudWatch, X-Ray, S3 y Pub/Sub. Confirmar en las páginas oficiales antes de presentar la cifra final. Cada uno está en `cost_model.py`, diccionario `P`.

## Supuestos

| Supuesto | Valor | Por qué importa |
|---|---|---|
| Frecuencia por máquina | 1 mensaje cada 30 s = 86.400 al mes | Es el factor que más mueve el costo: todo lo variable es lineal en esto |
| Tamaño de mensaje | 0,5 KB | Cabe en 1 WRU de DynamoDB |
| Entrada | API Gateway HTTP (`POST /v1/telemetry`, contrato §6) → Lambda ingesta → SQS | El endpoint lo fija el contrato; el uso de SQS lo asume este modelo (la edad de cola y la DLQ dependen de ello) |
| Consumidor | Lambda por lote de 10, 256 MB, 250 ms | |
| Escrituras DynamoDB | 2 por mensaje (histórico + último estado) | |
| Retención | TTL de 30 días (contrato §8) | Sin TTL el almacenamiento crece cada mes |
| S3 | Parquet de telemetría, ~4x más compacto, 1 objeto por ~1.000 mensajes | Contrato §8 prevé `telemetry/` en Parquet; el tamaño es un supuesto |
| Logs | 0,7 KB por invocación | Bajar el nivel a WARN reduce esta fila |
| X-Ray | 1 % de muestreo | Ver nota abajo |

Supuesto: 1 mensaje cada 30 s por maquina = 86,400 mensajes/maquina/mes

| Componente | USD/maquina/mes (variable) |
|---|---:|
| API Gateway HTTP (entrada) | 0.0864 |
| Lambda ingesta | 0.0265 |
| SQS (envio + recepcion + borrado) | 0.0415 |
| Lambda consumidora | 0.0109 |
| DynamoDB escrituras (2 por mensaje) | 0.1080 |
| DynamoDB lecturas (API/consultas, 0,1 por mensaje) | 0.0011 |
| DynamoDB almacenamiento (TTL 30 dias) | 0.0103 |
| Puente Pub/Sub (datos a Google, IA-4) | 0.0016 |
| S3 Parquet de telemetria (almacenamiento + PUT) | 0.0007 |
| CloudWatch Logs (ingesta + consumidora) | 0.0317 |
| X-Ray (trazas muestreadas) | 0.0043 |
| **Subtotal variable** | **0.3230** |

| Componente fijo | USD/mes (total flota) |
|---|---:|
| SF-2 (1 ejecucion/dia, ~25 transiciones) | 0.02 |
| CloudWatch metricas personalizadas (EMF, ~8) | 2.40 |
| CloudWatch alarmas (8) | 0.80 |
| CloudWatch dashboard (1) | 3.00 |
| **Subtotal fijo** | **6.22** |

| Flota | Variable/maq | Fijo/maq | **Total USD/maquina/mes** | Total flota USD/mes |
|---:|---:|---:|---:|---:|
| 100 | 0.3230 | 0.0622 | **0.3852** | 38.52 |
| 1,000 | 0.3230 | 0.0062 | **0.3292** | 329.23 |
| 5,000 | 0.3230 | 0.0012 | **0.3243** | 1621.28 |

## Lectura rápida

- **Costo total aproximado: US$ 0,32 a 0,38 por máquina al mes**, según el tamaño de la flota. A 5.000 máquinas son unos US$ 1.600 al mes.
- Lo fijo (métricas, alarmas, dashboard) pesa solo con flotas pequeñas: a 100 máquinas añade ~US$ 0,06 por máquina; a 5.000 es despreciable.
- **DynamoDB escrituras es lo más caro** (~1/3 del total). Una escritura por mensaje en vez de dos bajaría ~US$ 0,05 por máquina.
- **API Gateway** es el segundo. Es un costo de entrada que depende de la decisión de arquitectura de IA-2/IA-3.
- **X-Ray es sensible al muestreo**: al 5 % costaría unos US$ 0,02 por máquina, cinco veces más que al 1 %. Por eso `observability/template.yaml` lo deja en 1 %.
- **Lo que no incluye**: BigQuery y el respaldo en Azure Blob (Parte B / IA-4, contrato §11) no están modelados; son pequeños frente a lo anterior pero no cero. Tampoco transferencia de datos saliente.
- **Opcionales de `infra/data/template.yaml` (IA-2), apagados por defecto y NO modelados**: replicación S3 entre regiones (`EnableReplication`: transferencia entre regiones + PUT de replicación) y Global Tables de Alerts/Incidents (`EnableGlobalTables`: escrituras replicadas, más caras). Activarlos sube el costo; avísame y los agrego al modelo.
- Las tablas Machines, Alerts, Incidents, Technicians e Inventory tienen PITR (respaldo continuo, se cobra por GB-mes); son tablas pequeñas, así que se ignoran. Telemetry no tiene PITR (el histórico vive en S3).
- El SF-2 corre una vez al día con unas 25 transiciones: centavos al mes.

## Reproducir

```
python part-a/observability/cost_model.py                 # 30 s entre mensajes
python part-a/observability/cost_model.py --interval 10   # 1 mensaje cada 10 s
python part-a/observability/cost_model.py --xray-rate 0.05
```
