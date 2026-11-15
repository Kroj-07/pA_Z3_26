# part-a/shared — Utilidades compartidas (IA-1)

## hmac_utils.py

Firma y verificación HMAC-SHA256 del contrato v1.2 (sección 11). Solo biblioteca estándar.

- Mensaje firmado: `"{timestamp}.{cuerpo_crudo}"`.
- Cabeceras: `x-timestamp` y `x-signature` (hex en minúsculas).
- Ventana de ±300 segundos.

| Función | Uso |
|---|---|
| `sign_hmac(secret, body, *, timestamp=None)` | Devuelve `(timestamp, signature)` |
| `verify_hmac(secret, body, timestamp, signature, *, now=None, window_seconds=300)` | `True` o `False`; nunca lanza por entradas inválidas |
| `build_headers(secret, body)` | Cabeceras listas para enviar |
| `extract_hmac_headers(headers)` | Lee las dos cabeceras sin importar mayúsculas |

Ejemplo de verificación en una Lambda detrás de API Gateway:

```python
from hmac_utils import extract_hmac_headers, verify_hmac

ts, sig = extract_hmac_headers(event.get("headers"))
body = base64.b64decode(event["body"]) if event.get("isBase64Encoded") else event["body"]
if not verify_hmac(secret, body, ts, sig):
    return {"statusCode": 401, ...}
```

Reglas importantes: verificar con los **bytes crudos** recibidos (nunca volver a serializar el
JSON) y firmar el texto exacto que viaja en `x-timestamp`.

## Variables de entorno

Ninguna. El secreto no se lee aquí: quien llama lo obtiene de Secrets Manager
(`arquifusion/{env}/alert-hmac`) o del Secret Manager de Google y lo pasa a la función.

## Cómo probar

```bash
cd part-a/shared
python -m unittest -v
```

## Pendiente

- **Formato de `x-timestamp`:** el contrato no lo define. Se genera epoch en segundos y se
  aceptan también fechas ISO 8601 con zona. B (Cloud Run) debe firmar el mismo texto que envía.
- Actualizar la sección 11 de `/contract/README.md`: la firma cubre `"{x-timestamp}.{cuerpo_crudo}"`
  y no solo el cuerpo. Requiere la aceptación de B.
- Nombre del secreto: `arquifusion/alert-hmac` frente a `arquifusion/{env}/alert-hmac`.
