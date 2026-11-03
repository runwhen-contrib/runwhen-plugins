import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runwhen_capability.testing import make_context, run_main

# At run time a skill's lib/ is on sys.path (CAP-12); its package is named after the skill.
for lib in sorted((Path(__file__).parents[2] / "skills").glob("*/lib")):
    sys.path.insert(0, str(lib))

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "pg-base"
DATA = Path(__file__).parent / "data" / "pg_base"

SETTINGS = {
    "maxItems": 200,
    "connectTimeout": "10s",
    "statementTimeout": "30s",
    "includeQueryText": False,  # declared by pg-long-running-queries only; harmless elsewhere
}
INSTANCE = {
    "path": "postgres/instances/orders-db",
    "type": "postgres/instance",
    "name": "orders-db",
}


def load_script(name, skill="pg-base"):
    spec = importlib.util.spec_from_file_location(
        f"{skill.replace('-', '_')}_{name}", PLUGIN / "skills" / skill / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# what pg.session_sql builds: the task's SQL is between the SET LOCAL and the COMMIT
SCRIPT = re.compile(
    r"BEGIN READ ONLY; SET LOCAL statement_timeout = (?P<ms>\d+); (?P<sql>.*); COMMIT;", re.S
)


class FakePsql:
    """Stands in for ctx.run: answers psql from the first handler whose text is in the SQL.

    Like the SDK's ctx.run under v13 it would raise on a non-zero exit, but pg.psql_json must ask
    for check=False and classify itself, so a call that does not pass check=False fails here."""

    def __init__(self):
        self.handlers = []
        self.calls = []  # {"argv", "sql", "database", "env", "timeout"}

    def on(self, contains, stdout="", rc=0, stderr=""):
        self.handlers.append((contains, subprocess.CompletedProcess([], rc, stdout, stderr)))
        return self

    def sqls(self):
        return [c["sql"] for c in self.calls]

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False, "psql_json must pass check=False and classify the exit itself"
        assert argv[0] == "psql"
        clauses = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        assert len(clauses) == 1, "one -c: the whole script is one simple query"
        script = clauses[0]
        match = SCRIPT.fullmatch(script)
        assert match, f"not a read-only transaction with a local statement timeout: {script[:120]}"
        sql = match["sql"]
        database = argv[argv.index("-d") + 1] if "-d" in argv else None
        self.calls.append(
            {
                "argv": list(argv),
                "script": script,
                "sql": sql,
                "database": database,
                "env": env,
                "timeout": timeout,
            }
        )
        for contains, result in self.handlers:
            if contains in sql:
                return subprocess.CompletedProcess(
                    argv, result.returncode, result.stdout, result.stderr
                )
        raise AssertionError(f"no fake answer for SQL: {sql[:200]}")


@pytest.fixture
def fake_psql():
    return FakePsql()


@pytest.fixture
def data():
    def load(name):
        return (DATA / name).read_text()

    return load


@pytest.fixture
def run_script(fake_psql):
    """run_script("discover", **inputs): the script's main under the real v13 context, psql
    answered by `fake_psql`. `skill=` picks another postgres skill (default pg-base). Returns the
    result envelope, the errors and the files written under $RW_OUT."""

    def run(script, *, skill="pg-base", resource=INSTANCE, settings=None, **inputs):
        main = load_script(script, skill).main
        with make_context(resource=resource, settings={**SETTINGS, **(settings or {})}) as tc:
            tc.ctx.run = fake_psql
            envelope = run_main(main, tc, **inputs)
            files = {p.name: p.read_text() for p in Path(tc.out_dir).iterdir()}
            return SimpleNamespace(envelope=envelope, errors=list(tc.errors), files=files)

    return run
