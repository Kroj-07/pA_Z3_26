#!/usr/bin/env python3
"""Modelo de costo por maquina/mes (reproducible). python cost_model.py [--interval 30] [--markdown]

Precios de lista us-east-1 en USD, SIN capa gratuita (estimacion conservadora).
VERIFICADOS en esta sesion contra fuentes de 2026: Lambda y DynamoDB on-demand.
DE REFERENCIA (precio de lista conocido, no re-verificado): el resto; confirmar en las paginas oficiales.
"""
import argparse

P = {  # precio unitario                                   estado
    "lambda_req_per_m": 0.20,            # verificado
    "lambda_gb_s": 0.0000166667,         # verificado (x86)
    "ddb_write_per_m": 0.625,            # verificado (on-demand, tras el recorte 2024)
    "ddb_read_per_m": 0.125,             # verificado
    "ddb_storage_gb": 0.25,              # verificado
    "apigw_http_per_m": 1.00,            # referencia (HTTP API)
    "sqs_per_m": 0.40,                   # referencia (estandar)
    "sfn_per_1k_transitions": 0.025,     # referencia (STANDARD)
    "cw_logs_ingest_gb": 0.50,           # referencia
    "cw_custom_metric": 0.30,            # referencia
    "cw_alarm": 0.10,                    # referencia (estandar)
    "cw_dashboard": 3.00,                # referencia
    "xray_trace_per_m": 5.00,            # referencia
    "pubsub_per_tib": 40.0,              # referencia (Google Cloud, Parte B)
    "s3_gb": 0.023,                      # referencia (S3 Standard)
    "s3_put_per_1k": 0.005,              # referencia
}


def model(machines, interval_s, xray_rate=0.01, ingest_log_kb=0.7):
    msgs = 30 * 24 * 3600 / interval_s          # mensajes por maquina al mes
    M = machines * msgs                          # mensajes totales
    batch = 10                                   # tamano de lote de la Lambda consumidora
    kb_msg = 0.5                                 # tamano medio del mensaje
    rows = {}
    rows["API Gateway HTTP (entrada)"] = M / 1e6 * P["apigw_http_per_m"]
    ingest_gbs = M * 0.128 * 0.050               # 128 MB x 50 ms
    rows["Lambda ingesta"] = M / 1e6 * P["lambda_req_per_m"] + ingest_gbs * P["lambda_gb_s"]
    rows["SQS (envio + recepcion + borrado)"] = M * (1 + 2 / batch) / 1e6 * P["sqs_per_m"]
    cons_inv = M / batch
    cons_gbs = cons_inv * 0.256 * 0.250          # 256 MB x 250 ms por lote
    rows["Lambda consumidora"] = cons_inv / 1e6 * P["lambda_req_per_m"] + cons_gbs * P["lambda_gb_s"]
    rows["DynamoDB escrituras (2 por mensaje)"] = M * 2 / 1e6 * P["ddb_write_per_m"]
    rows["DynamoDB lecturas (API/consultas, 0,1 por mensaje)"] = M * 0.1 / 1e6 * P["ddb_read_per_m"]
    rows["DynamoDB almacenamiento (TTL 30 dias)"] = M * kb_msg / 1024 / 1024 * P["ddb_storage_gb"]
    rows["Puente Pub/Sub (datos a Google, IA-4)"] = (M * kb_msg * 1024 / 1024**4) * P["pubsub_per_tib"]
    # Parquet en s3://af-{env}-arquifusion-data/telemetry/ (contrato 8): ~4x mas compacto, 1 objeto por ~1000 mensajes
    rows["S3 Parquet de telemetria (almacenamiento + PUT)"] = (M * kb_msg / 4 / 1024 / 1024) * P["s3_gb"] + (M / 1000 / 1000) * P["s3_put_per_1k"] * 1000 / 1000
    rows["CloudWatch Logs (ingesta + consumidora)"] = (M * ingest_log_kb + cons_inv * ingest_log_kb) / 1024 / 1024 * P["cw_logs_ingest_gb"]
    rows["X-Ray (trazas muestreadas)"] = M * xray_rate / 1e6 * P["xray_trace_per_m"]
    fixed = {
        "SF-2 (1 ejecucion/dia, ~25 transiciones)": 30 * 25 / 1000 * P["sfn_per_1k_transitions"],
        "CloudWatch metricas personalizadas (EMF, ~8)": 8 * P["cw_custom_metric"],
        "CloudWatch alarmas (8)": 8 * P["cw_alarm"],
        "CloudWatch dashboard (1)": P["cw_dashboard"],
    }
    return msgs, rows, fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=30, help="segundos entre mensajes por maquina")
    ap.add_argument("--xray-rate", type=float, default=0.01)
    a = ap.parse_args()
    sizes = (100, 1000, 5000)
    per = {n: model(n, a.interval, a.xray_rate) for n in sizes}
    msgs = per[100][0]
    print("Supuesto: 1 mensaje cada %.0f s por maquina = %s mensajes/maquina/mes\n" % (a.interval, f"{msgs:,.0f}"))
    print("| Componente | USD/maquina/mes (variable) |")
    print("|---|---:|")
    for k, v in per[1000][1].items():
        print("| %s | %.4f |" % (k, v / 1000))
    var = sum(per[1000][1].values()) / 1000
    print("| **Subtotal variable** | **%.4f** |\n" % var)
    print("| Componente fijo | USD/mes (total flota) |")
    print("|---|---:|")
    for k, v in per[1000][2].items():
        print("| %s | %.2f |" % (k, v))
    fx = sum(per[1000][2].values())
    print("| **Subtotal fijo** | **%.2f** |\n" % fx)
    print("| Flota | Variable/maq | Fijo/maq | **Total USD/maquina/mes** | Total flota USD/mes |")
    print("|---:|---:|---:|---:|---:|")
    for n in sizes:
        v = sum(per[n][1].values()) / n
        f = sum(per[n][2].values()) / n
        print("| %s | %.4f | %.4f | **%.4f** | %.2f |" % (f"{n:,}", v, f, v + f, (v + f) * n))


if __name__ == "__main__":
    main()
