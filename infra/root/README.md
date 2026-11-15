# infra/root — Stack raíz (IA-1)

Plantilla raíz de CloudFormation/SAM. Hoy solo publica configuración común en SSM;
cuando las demás IAs entreguen sus plantillas, se descomentan sus stacks anidados.

## Parámetros y variables

| Parámetro | Valores | Descripción |
|---|---|---|
| `Env` | `shared` o `test` (por defecto `test`) | Entorno |
| `LabRoleName` | por defecto `LabRole` | Nombre del rol único del Learner Lab |

Parámetros SSM que publica (los demás los leen, no escriben nombres a mano):

| Nombre | Contenido |
|---|---|
| `/arquifusion/{env}/root/env` | `shared` o `test` |
| `/arquifusion/{env}/root/lab-role-arn` | ARN de LabRole. Toda Lambda, máquina de estados y API lo usa como rol |
| `/arquifusion/{env}/root/region` | Región del despliegue |

Los stacks anidados reciben `Env` y `LabRoleArn` como parámetros. No se crean roles propios.

## Cómo probar

```bash
pip install cfn-lint
cfn-lint infra/root/template.yaml
sam validate --lint -t infra/root/template.yaml   # si tienes SAM CLI

sam deploy -t infra/root/template.yaml --stack-name af-test-root \
  --region us-east-1 --parameter-overrides Env=test \
  --resolve-s3 --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND
```

Para verificar el resultado: `aws ssm get-parameters-by-path --path /arquifusion/test/root/`.
Nombre de stack propuesto: `af-{env}-root`. La plantilla rechaza cualquier región que no sea
us-east-1 o us-west-2.

## Pendiente

- **Sin despliegue real aún.** La plantilla pasa `cfn-lint` sin errores, pero no se ha
  desplegado en el Learner Lab ni se ha corrido `sam validate`. Falta confirmar que la cuenta es Learner Lab y que permite
  SSM, Step Functions, Secrets Manager, Global Tables, SES y Cognito.
- `sam deploy --resolve-s3` crea un bucket de artefactos administrado por SAM. Si el Lab lo
  bloquea, crear uno a mano y usar `--s3-bucket`.
- Rutas y nombres de los stacks anidados: cada IA debe confirmar los suyos.
- Nombres de recursos del contrato (`arquifusion-data`, `arquifusion-events`) frente al prefijo
  `af-`: propuesta de usar `af-{env}-...` y publicarlos en SSM. Pendiente de acuerdo.
- El nombre del secreto HMAC (`arquifusion/{env}/alert-hmac`) sigue pendiente de acuerdo.
