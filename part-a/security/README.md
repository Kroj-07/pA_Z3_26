# part-a/security — Seguridad (IA-1)

Estado: **sin implementar todavía.** Aquí vivirá lo que la Parte A debe resolver de seguridad.

## Alcance previsto (contrato v1.2)

- Secretos solo en Secrets Manager con nombre `arquifusion/{env}/{nombre}`; configuración no
  secreta en SSM bajo `/arquifusion/{env}/`.
- Verificación de alertas entrantes (`POST /v1/alerts`) con HMAC usando
  `../shared/hmac_utils.py`, idempotente por `alert_id`.
- Tokens con expiración y autorizador por rol (Cognito, o autorizador Lambda si el Lab lo bloquea).
- Todo corre con LabRole: el mínimo privilegio se documenta pero no se promete.

## Variables de entorno

Ninguna por ahora.

## Cómo probar

No aplica todavía. La librería HMAC se prueba en `part-a/shared` con `python -m unittest -v`.

## Pendiente

- Todo lo anterior: no hay plantilla ni código en esta carpeta.
- Nombre del secreto HMAC (`arquifusion/alert-hmac` o `arquifusion/{env}/alert-hmac`).
- Confirmar si el Learner Lab permite Cognito y Secrets Manager.
- Existe un archivo vacío `README copy.md` en esta carpeta; parece un duplicado accidental y se
  deja sin tocar hasta que el equipo decida.
