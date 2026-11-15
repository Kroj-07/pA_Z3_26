#!/usr/bin/env python3
"""Revisa las plantillas SAM/CloudFormation contra las reglas del Learner Lab y del contrato.

Sin AWS ni SAM CLI (solo PyYAML). No sustituye a `sam validate --lint`, pero atrapa lo que mas
suele romper `sam deploy` en el Lab:

  1. Ningun recurso IAM (AWS::IAM::*): el Lab no deja crear roles.
  2. Toda funcion usa un Role explicito (LabRole) o lo hereda de Globals.
  3. Nada de servicios con costo fijo (EC2, RDS, NAT, balanceadores, Kinesis, OpenSearch...).
  4. Todo nombre SSM que una plantilla lee con {{resolve:ssm:...}} lo publica alguna plantilla.
  5. Cada CodeUri / TemplateURL local existe.
  6. Los recursos con nombre fijo llevan el prefijo af-.

Uso:  python scripts/check_templates.py            (sale con codigo 1 si hay errores)
"""
import glob
import os
import re
import sys

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FORBIDDEN = (
    "AWS::EC2::", "AWS::RDS::", "AWS::ElasticLoadBalancing", "AWS::ElasticLoadBalancingV2::",
    "AWS::ElastiCache::", "AWS::OpenSearchService::", "AWS::Elasticsearch::", "AWS::Kinesis::",
    "AWS::ECS::", "AWS::EKS::", "AWS::MSK::", "AWS::Redshift::", "AWS::Budgets::", "AWS::Cognito::",
)
NAMED = {  # recurso -> propiedad con el nombre fijo
    "AWS::DynamoDB::Table": "TableName",
    "AWS::DynamoDB::GlobalTable": "TableName",
    "AWS::SQS::Queue": "QueueName",
    "AWS::SNS::Topic": "TopicName",
    "AWS::Serverless::Function": "FunctionName",
    "AWS::Lambda::Function": "FunctionName",
    "AWS::Events::EventBus": "Name",
    "AWS::Serverless::StateMachine": "Name",
    "AWS::StepFunctions::StateMachine": "StateMachineName",
}
TEMPLATES = [
    "infra/root/template.yaml", "infra/security/template.yaml", "infra/data/template.yaml",
    "infra/ingest/template.yaml", "infra/api/template.yaml", "infra/api/events-template.yaml",
    "infra/bridge/template.yaml", "part-a/sf2/template.yaml", "part-a/observability/template.yaml",
]


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {"Fn::" + suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"Fn::" + suffix: loader.construct_sequence(node, deep=True)}
    return {"Fn::" + suffix: loader.construct_mapping(node, deep=True)}


Loader.add_multi_constructor("!", _tag)


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


def norm(name):
    """Normaliza ${Env} / {env} / {ENV} a {env} para comparar nombres SSM."""
    return re.sub(r"\$?\{(Env|env|ENV)\}", "{env}", name)


def main():
    errors, warnings = [], []
    published, read, cond_read = set(), [], []
    for rel in TEMPLATES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            errors.append("%s: no existe" % rel)
            continue
        with open(path, encoding="utf-8") as fh:
            doc = yaml.load(fh, Loader=Loader)
        base = os.path.dirname(path)
        res = doc.get("Resources", {})
        globals_fn = (doc.get("Globals", {}) or {}).get("Function", {}) or {}
        for lid, r in res.items():
            t = r.get("Type", "")
            props = r.get("Properties", {}) or {}
            if t.startswith("AWS::IAM::"):
                errors.append("%s: %s es un recurso IAM (el Lab no lo permite)" % (rel, lid))
            if any(t.startswith(f) for f in FORBIDDEN):
                errors.append("%s: %s (%s) tiene costo fijo o no es serverless" % (rel, lid, t))
            if t == "AWS::Serverless::Function" and "Role" not in props and "Role" not in globals_fn:
                errors.append("%s: %s no define Role (debe ser LabRole)" % (rel, lid))
            if t in ("AWS::Serverless::StateMachine",) and "Role" not in props:
                errors.append("%s: %s no define Role (debe ser LabRole)" % (rel, lid))
            prop = NAMED.get(t)
            if prop and prop in props:
                val = props[prop]
                text = " ".join(strings(val))
                if "af-" not in text and "af-" not in str(val):
                    errors.append("%s: %s: %s no lleva el prefijo af-" % (rel, lid, prop))
            if t == "AWS::SSM::Parameter":
                name = props.get("Name")
                for s in strings(name):
                    published.add(norm(s))
            for key in ("CodeUri", "TemplateURL"):
                val = props.get(key)
                if isinstance(val, str) and not val.startswith(("s3://", "http")):
                    if not os.path.exists(os.path.normpath(os.path.join(base, val))):
                        errors.append("%s: %s.%s apunta a una ruta inexistente: %s" % (rel, lid, key, val))
            if key == "CodeUri" and t == "AWS::Serverless::Function":
                pass
        gcode = globals_fn.get("CodeUri")
        if isinstance(gcode, str) and not os.path.exists(os.path.normpath(os.path.join(base, gcode))):
            errors.append("%s: Globals.CodeUri inexistente: %s" % (rel, gcode))
        for lid, r in res.items():
            conditional = "Condition" in r  # rama opcional: solo se avisa
            for s in strings(r):
                for m in re.finditer(r"resolve:ssm:(/(?:[^}]|\$\{\w+\})+)\}\}", s):
                    (cond_read if conditional else read).append((rel, norm(m.group(1))))
    for rel, name in sorted(set(read)):
        if name not in published:
            errors.append("SSM %s se lee en %s pero ninguna plantilla lo publica" % (name, rel))
    for rel, name in sorted(set(cond_read)):
        if name not in published:
            warnings.append("SSM %s (rama condicional de %s) no se publica; solo se necesita si se activa esa rama" % (name, rel))
    print("%d plantillas, %d parametros SSM publicados, %d leidos con resolve, %d errores"
          % (len(TEMPLATES), len(published), len(set(read)), len(errors)))
    for w in warnings:
        print("AVISO  " + w)
    for e in errors:
        print("ERROR  " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
