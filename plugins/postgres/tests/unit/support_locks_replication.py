"""Shared by the pg-locks and pg-replication-lag unit tests: run one of their scripts under the real
v13 context with psql answered by the FakePsql of conftest."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import yaml
from conftest import INSTANCE, SETTINGS
from runwhen_capability.testing import make_context, run_main

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"


def load_script(skill, name):
    spec = importlib.util.spec_from_file_location(
        f"{skill.replace('-', '_')}_{name}", SKILLS / skill / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_runner(skill, fake_psql):
    def run(script, *, resource=INSTANCE, settings=None, **inputs):
        main = load_script(skill, script).main
        with make_context(resource=resource, settings={**SETTINGS, **(settings or {})}) as tc:
            tc.ctx.run = fake_psql
            envelope = run_main(main, tc, **inputs)
            return SimpleNamespace(envelope=envelope, errors=list(tc.errors))

    return run


def tasks(skill):
    doc = yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())
    return {t["name"]: t for t in doc["tasks"]}


def output_schema(skill, task, output):
    ref = tasks(skill)[task]["outputs"][output]["schema"]
    import json

    return json.loads((SKILLS / skill / ref).read_text())


def assert_valid(skill, task, output, value):
    schema = output_schema(skill, task, output)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(value, schema, cls=jsonschema.Draft202012Validator)


FAILURES = [
    (
        'psql: error: connection to server at "h", port 5432 failed: FATAL:  password authentication failed for user "app"',
        "permission_denied",
    ),
    (
        'psql: error: connection to server at "h", port 5432 failed: Connection refused',
        "unreachable",
    ),
    (
        'psql: error: connection to server at "h", port 5432 failed: FATAL:  database "orders" does not exist',
        "not_found",
    ),
    ("ERROR:  canceling statement due to statement timeout", "timeout"),
]
