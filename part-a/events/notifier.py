"""ArquiFusion - Parte A - Lambda de notificaciones por rol (Python 3.12).

Handler: ``events.notifier.handler``. Dos modos de entrada:

  1. Evento de EventBridge (alert.created, incident.opened, incident.closed): se decide a quien
     avisar segun la tabla 6.9 del contrato (plan_deliveries), se guarda cada aviso en la tabla
     de notificaciones (bandeja de la app: GET /v1/notifications) y se envia por SNS (+ SES).
  2. Evento programado {"mode": "digest", "period": "hourly|shift|daily|weekly"}: envia el
     resumen acumulado de los roles que reciben resumenes en ese periodo.

Reglas implementadas (6.9; codigos de rol en ingles):
  operator     alertas medium+ de su maquina                       inmediato
  technician   incidentes asignados a el (incident.opened)         inmediato
  maintenance  alertas high/critical inmediato; medium en resumen por hora; incidentes nuevos
               high/critical inmediato; cierres y recurrencias (inmediato la recurrencia)
  supervisor   high/critical inmediato; medium y cierres en resumen por turno
  admin        critical inmediato; high y cierres en resumen diario
  management   critical masivo inmediato; critical/causa comun en resumen semanal

Idempotencia: cada aviso tiene clave determinista (recipient_role + sk) y se escribe con
condicion; EventBridge puede entregar mas de una vez sin duplicar avisos ya enviados.
Si el envio falla se marca el aviso como "failed" y se relanza el error para que Lambda reintente.
"""
import json
import logging
import os
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SEV_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
ROLES = ("operator", "technician", "maintenance", "supervisor", "admin", "management")
DIGEST_ROLE_PERIOD = {"maintenance": "hourly", "supervisor": "shift", "admin": "daily", "management": "weekly"}
MAX_DIGEST_ITEMS = 500

ACTION_ES = {
    "monitor": "seguir monitoreando",
    "inspect_within_72h": "revisar dentro de 72 horas",
    "inspect_within_24h": "revisar dentro de 24 horas",
    "stop_and_inspect_now": "detener la maquina y revisarla ahora",
}
TYPE_ES = {
    "predictive_failure": "riesgo de falla",
    "anomaly": "comportamiento anomalo",
    "fuel_drift": "deriva de combustible",
    "low_utilization": "baja utilizacion",
    "overuse": "sobreuso",
    "common_cause": "causa comun en varias maquinas",
    "maintenance_due": "mantenimiento requerido",
}
PERIOD_ES = {"hourly": "por hora", "shift": "por turno", "daily": "diario", "weekly": "semanal"}


# ------------------------------------------------------------------ clientes AWS (perezosos)
_clients = {}
_table = None


def _client(name):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def get_table():
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NOTIFICATIONS"])
    return _table


def _code(exc):
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")


# ------------------------------------------------------------------ reglas 6.9
def _sev(detail):
    return SEV_RANK.get(detail.get("severity"), 2)  # sin severidad se trata como medium


def is_massive(detail):
    minimum = int(os.environ.get("MASSIVE_MIN_MACHINES", "3"))
    related = detail.get("related_machine_ids") or []
    return detail.get("type") == "common_cause" or len(related) >= minimum


def plan_deliveries(detail_type, detail):
    """Devuelve [(rol, modo)] con modo 'immediate' o 'digest:<periodo>'."""
    sev = _sev(detail)
    plan = []
    if detail_type == "alert.created":
        if sev >= SEV_RANK["medium"]:
            plan.append(("operator", "immediate"))
        if sev >= SEV_RANK["high"]:
            plan.append(("maintenance", "immediate"))
            plan.append(("supervisor", "immediate"))
        elif sev == SEV_RANK["medium"]:
            plan.append(("maintenance", "digest:hourly"))
            plan.append(("supervisor", "digest:shift"))
        if sev == SEV_RANK["critical"]:
            plan.append(("admin", "immediate"))
            plan.append(("management", "immediate" if is_massive(detail) else "digest:weekly"))
        elif sev == SEV_RANK["high"]:
            plan.append(("admin", "digest:daily"))
            if detail.get("type") == "common_cause":
                plan.append(("management", "digest:weekly"))
    elif detail_type == "incident.opened":
        if detail.get("technician_id"):
            plan.append(("technician", "immediate"))
        if sev >= SEV_RANK["high"]:
            plan.append(("maintenance", "immediate"))
            plan.append(("supervisor", "immediate"))
        else:
            plan.append(("maintenance", "digest:hourly"))
            plan.append(("supervisor", "digest:shift"))
    elif detail_type == "incident.closed":
        plan.append(("maintenance", "immediate" if detail.get("recurrence_of") else "digest:hourly"))
        plan.append(("supervisor", "digest:shift"))
        plan.append(("admin", "digest:daily"))
    return plan


# ------------------------------------------------------------------ mensajes por rol
def _ascii(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _evidence_lines(detail, limit=3):
    lines = []
    for ev in (detail.get("evidence") or [])[:limit]:
        lines.append(
            "- %s: %s (esperado %s, ventana %s, tendencia %s)"
            % (ev.get("metric"), ev.get("observed"), ev.get("expected"), ev.get("window", "n/d"), ev.get("trend", "n/d"))
        )
    return lines


def render(role, detail_type, detail):
    """Devuelve (asunto, cuerpo) segun el rol. Los textos son para personas (espanol)."""
    sev = (detail.get("severity") or "medium").upper()
    machine = detail.get("machine_id", "?")
    zone = detail.get("zone_id", "n/d")
    if detail_type == "alert.created":
        kind = TYPE_ES.get(detail.get("type"), detail.get("type", "alerta"))
        action = ACTION_ES.get(detail.get("recommended_action"), "revisar")
        subject = "[%s] %s: %s" % (sev, machine, kind)
        summary = detail.get("summary", "")
        if role == "operator":
            body = "Maquina %s: %s.\nQue hacer: %s." % (machine, summary, action)
        elif role == "maintenance":
            body = "Alerta %s en %s (%s).\n%s\nPuntaje %s, confianza %s. Accion sugerida: %s.\nComponente sospechoso: %s." % (
                detail.get("alert_id"), machine, zone, summary, detail.get("score"), detail.get("confidence"), action,
                detail.get("suspected_component") or "sin identificar",
            )
        elif role == "supervisor":
            body = "Zona %s: %s en %s (%s). Accion: %s." % (zone, kind, machine, sev, action)
        elif role == "admin":
            body = "%s en %s, zona %s. %s" % (kind, machine, zone, summary)
        else:  # management
            related = detail.get("related_machine_ids") or []
            extra = " Afecta a %d maquinas." % len(related) if related else ""
            body = "%s en zona %s (%s).%s" % (kind, zone, sev, extra)
        return subject, body
    if detail_type == "incident.opened":
        subject = "[%s] Incidente %s en %s" % (sev, detail.get("incident_id"), machine)
        if role == "technician":
            lines = [
                "Incidente %s en la maquina %s, zona %s." % (detail.get("incident_id"), machine, zone),
                "Componente sospechoso: %s." % (detail.get("suspected_component") or "sin identificar"),
            ]
            lines += _evidence_lines(detail)
            parts = detail.get("suggested_parts") or []
            if parts:
                lines.append("Repuestos sugeridos: %s." % ", ".join(str(p) for p in parts))
            lines.append("Confirma desde la app para iniciar la atencion.")
            return subject, "\n".join(lines)
        return subject, "Incidente %s abierto en %s (zona %s), tecnico: %s." % (
            detail.get("incident_id"), machine, zone, detail.get("technician_id") or "sin asignar"
        )
    subject = "Incidente %s cerrado en %s" % (detail.get("incident_id"), machine)
    recurrence = " Repite el incidente %s." % detail["recurrence_of"] if detail.get("recurrence_of") else ""
    return subject, "Incidente %s en %s (zona %s) cerrado; detenida %s min.%s" % (
        detail.get("incident_id"), machine, zone, detail.get("downtime_minutes", "n/d"), recurrence
    )


# ------------------------------------------------------------------ envio
_email_cache = {}


def email_recipients(role):
    """Correos del rol desde SSM /arquifusion/{env}/notify/email/{role} (StringList, opcional)."""
    if role in _email_cache:
        return _email_cache[role]
    name = "%s/notify/email/%s" % (os.environ.get("SSM_PREFIX", "/arquifusion/dev"), role)
    try:
        value = _client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
        recipients = [e.strip() for e in value.split(",") if e.strip()]
    except Exception as exc:  # noqa: BLE001
        if _code(exc) != "ParameterNotFound":
            logger.warning("no se pudo leer %s: %s", name, _code(exc))
        recipients = []
    _email_cache[role] = recipients
    return recipients


def send_message(role, subject, body, attrs):
    """Publica en el topico del rol (SNS) y, si hay remitente y destinatarios, por SES."""
    channels = []
    topic = os.environ.get("TOPIC_%s" % role.upper())
    if topic:
        message_attrs = {k: {"DataType": "String", "StringValue": str(v)} for k, v in attrs.items() if v not in (None, "")}
        _client("sns").publish(TopicArn=topic, Subject=_ascii(subject)[:100], Message=body, MessageAttributes=message_attrs)
        channels.append("sns")
    sender = os.environ.get("SES_SENDER")
    recipients = email_recipients(role) if sender else []
    if recipients:
        try:
            _client("ses").send_email(
                Source=sender,
                Destination={"ToAddresses": recipients[:50]},
                Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}},
            )
            channels.append("ses")
        except Exception as exc:  # noqa: BLE001  (SES en sandbox rechaza correos no verificados)
            logger.warning("SES fallo para %s: %s", role, _code(exc))
    return channels


# ------------------------------------------------------------------ registro de avisos
def _ts(detail_type, detail, event):
    if detail_type == "alert.created":
        return detail.get("created_at")
    if detail_type == "incident.opened":
        return detail.get("opened_at")
    return detail.get("closed_at") or detail.get("close_requested_at") or event.get("time")


def _ref_id(detail_type, detail):
    return detail.get("alert_id") if detail_type == "alert.created" else detail.get("incident_id")


def process_event(event):
    detail_type = event.get("detail-type")
    detail = event.get("detail") or {}
    if detail_type not in ("alert.created", "incident.opened", "incident.closed"):
        logger.info("evento ignorado: %s", detail_type)
        return {"ignored": detail_type}
    ref = _ref_id(detail_type, detail)
    stamp = _ts(detail_type, detail, event) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = get_table()
    ttl = int(time.time()) + int(os.environ.get("NOTIFICATION_TTL_DAYS", "30")) * 86400
    results = []
    for role, mode in plan_deliveries(detail_type, detail):
        immediate = mode == "immediate"
        subject, body = render(role, detail_type, detail)
        record = {
            "recipient_role": role,
            "sk": "%s#%s#%s" % (stamp, detail_type, ref),
            "created_at": stamp,
            "kind": "alert" if detail_type == "alert.created" else "incident",
            "event_type": detail_type,
            "alert_id": detail.get("alert_id"),
            "incident_id": detail.get("incident_id"),
            "machine_id": detail.get("machine_id"),
            "zone_id": detail.get("zone_id"),
            "technician_id": detail.get("technician_id"),
            "severity": detail.get("severity"),
            "title": subject,
            "body": body,
            "delivery": "immediate" if immediate else "digest",
            "status": "pending" if immediate else "queued",
            "ttl": ttl,
        }
        if not immediate:
            record["digest_period"] = mode.split(":", 1)[1]
        record = {k: v for k, v in record.items() if v is not None}
        try:
            table.put_item(Item=record, ConditionExpression="attribute_not_exists(recipient_role) AND attribute_not_exists(sk)")
        except Exception as exc:  # noqa: BLE001
            if _code(exc) != "ConditionalCheckFailedException":
                raise
            existing = table.get_item(Key={"recipient_role": role, "sk": record["sk"]}, ConsistentRead=True).get("Item") or {}
            if not immediate or existing.get("status") == "sent":
                results.append({"role": role, "mode": mode, "result": "duplicate"})
                continue  # ya registrado (resumen) o ya enviado
        if not immediate:
            results.append({"role": role, "mode": mode, "result": "queued"})
            continue
        attrs = {
            "role": role, "severity": detail.get("severity"), "zone_id": detail.get("zone_id"),
            "machine_id": detail.get("machine_id"), "technician_id": detail.get("technician_id"), "kind": record["kind"],
        }
        try:
            channels = send_message(role, subject, body, attrs)
        except Exception as exc:  # noqa: BLE001
            table.update_item(
                Key={"recipient_role": role, "sk": record["sk"]},
                UpdateExpression="SET #s = :s, last_error = :e",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "failed", ":e": (_code(exc) or type(exc).__name__)[:200]},
            )
            raise  # Lambda reintenta y, tras agotar reintentos, el evento va a la DLQ
        table.update_item(
            Key={"recipient_role": role, "sk": record["sk"]},
            UpdateExpression="SET #s = :s, channels = :c, sent_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "sent", ":c": channels, ":t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        results.append({"role": role, "mode": mode, "result": "sent", "channels": channels})
    return {"event": detail_type, "ref": ref, "deliveries": results}


# ------------------------------------------------------------------ resumenes
def build_digest(role, period, items):
    counts = {}
    for it in items:
        sev = it.get("severity") or "n/d"
        counts[sev] = counts.get(sev, 0) + 1
    order = ["critical", "high", "medium", "low", "info", "n/d"]
    resumen = ", ".join("%d %s" % (counts[s], s) for s in order if s in counts)
    subject = "Resumen %s (%s): %d avisos" % (PERIOD_ES.get(period, period), role, len(items))
    lines = ["Resumen %s para %s: %d avisos (%s)." % (PERIOD_ES.get(period, period), role, len(items), resumen)]
    for it in items[:20]:
        lines.append("- %s | %s | %s" % (it.get("severity", "n/d"), it.get("machine_id", "n/d"), it.get("title", "")))
    if len(items) > 20:
        lines.append("... y %d mas." % (len(items) - 20))
    return subject, "\n".join(lines)


def run_digest(period):
    table = get_table()
    index = os.environ.get("DIGEST_INDEX", "by_pending_digest")
    summary = {}
    for role in [r for r, p in DIGEST_ROLE_PERIOD.items() if p == period]:
        items, start = [], None
        while len(items) < MAX_DIGEST_ITEMS:
            kwargs = {
                "IndexName": index,
                "KeyConditionExpression": "digest_period = :p",
                "FilterExpression": "recipient_role = :r",
                "ExpressionAttributeValues": {":p": period, ":r": role},
            }
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        items = items[:MAX_DIGEST_ITEMS]
        if not items:
            summary[role] = 0
            continue
        subject, body = build_digest(role, period, items)
        send_message(role, subject, body, {"role": role, "kind": "digest", "period": period})
        sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for it in items:  # al quitar digest_period el aviso sale del indice (indice disperso)
            table.update_item(
                Key={"recipient_role": it["recipient_role"], "sk": it["sk"]},
                UpdateExpression="SET #s = :s, digest_sent_at = :t REMOVE digest_period",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "digested", ":t": sent_at},
            )
        summary[role] = len(items)
    return {"digest": period, "sent": summary}


# ------------------------------------------------------------------ handler
def handler(event, context=None):
    if event.get("mode") == "digest":
        period = event.get("period")
        if period not in PERIOD_ES:
            raise ValueError("period invalido: %s" % period)
        result = run_digest(period)
    else:
        result = process_event(event)
    logger.info(json.dumps(result, default=str))
    return result
