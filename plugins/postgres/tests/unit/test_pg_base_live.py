"""The shared psql wrapper and the facts statement against the real local servers.

Needs the docker harness (tests/docker/up.sh) on 127.0.0.1:55432 and a psql client; skipped
when either is missing. Nothing here changes the server: the role tests run inside a
transaction that is rolled back."""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from pg_base import access as ax
from pg_base import pg
from pg_base.errors import RwError

PSQL = shutil.which("psql") or next(
    (
        p
        for p in ("/opt/homebrew/opt/libpq/bin/psql", "/usr/local/opt/libpq/bin/psql")
        if os.path.exists(p)
    ),
    None,
)
# throwaway test values of tests/docker/common.sh
LIBPQ = {
    "PGHOST": "127.0.0.1",
    "PGPORT": "55432",
    "PGUSER": "postgres",
    "PGPASSWORD": "rw-test-superuser",
    "PGDATABASE": "postgres",
    "PGSSLMODE": "disable",
}


def reachable():
    if not PSQL:
        return False
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-c", "SELECT 1"],
        env={**os.environ, **LIBPQ, "PGCONNECT_TIMEOUT": "2"},
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(not reachable(), reason="the docker harness is not running")


def real_run(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
    assert check is False
    done = subprocess.run(
        [PSQL, *argv[1:]],
        env={**os.environ, **LIBPQ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)


def ctx(**settings):
    values = {"connect_timeout": "5s", "statement_timeout": "30s", **settings}
    return SimpleNamespace(run=real_run, settings=SimpleNamespace(**values))


def test_the_value_comes_back_alone_not_the_transaction_commands():
    # -A -t -q print the SELECT's value only: no BEGIN, SET or COMMIT tags
    assert pg.psql_json(ctx(), "SELECT json_build_object('a', 1, 'b', 'x')") == {"a": 1, "b": "x"}


def test_the_statement_cannot_write():
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx(), "CREATE TEMP TABLE rw_probe (a int)")
    assert "read-only transaction" in err.value.message


def test_the_transaction_is_read_only_and_the_timeout_is_local_to_it():
    sql = (
        "SELECT json_build_object('ro', current_setting('transaction_read_only'),"
        " 'ms', current_setting('statement_timeout'))"
    )
    row = pg.psql_json(ctx(statement_timeout="12s"), sql)
    assert row == {"ro": "on", "ms": "12s"}
    # nothing was set on the session: a second call on the default setting sees its own value
    assert pg.psql_json(ctx(), sql)["ms"] == "30s"


def test_a_statement_that_overruns_is_cancelled_as_timeout():
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx(statement_timeout="300ms"), "SELECT json_build_object('a', pg_sleep(5))")
    assert err.value.code == "timeout"


def run_sql_rolled_back(setup, sql):
    script = f"BEGIN;\n{setup}\n{sql};\nROLLBACK;"
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-c", script],
        env={**os.environ, **LIBPQ},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(next(ln for ln in done.stdout.splitlines() if ln.startswith("{")))


NAMES = ["Mixed.Case", 'we"ird', "a.b", "A.B", "pg_monitor", "PG_MONITOR", "ghost"]


def test_role_names_are_matched_exactly_not_parsed_as_identifiers():
    setup = (
        'CREATE ROLE "Mixed.Case"; CREATE ROLE "we""ird"; CREATE ROLE "a.b";\n'
        'GRANT "Mixed.Case" TO rw_monitor; GRANT "we""ird" TO rw_monitor;\n'
        "SET ROLE rw_monitor;"
    )
    facts = run_sql_rolled_back(setup, ax.facts_sql(set(NAMES), set()))
    by_role = {r["role"]: r for r in facts["roles"]}
    assert {k: v["exists"] for k, v in by_role.items()} == {
        "Mixed.Case": True,
        'we"ird': True,
        "a.b": True,
        "A.B": False,  # to_regrole('A.B') would fold the case or split on the dot
        "pg_monitor": True,
        "PG_MONITOR": False,  # case-folding would have resolved it to pg_monitor
        "ghost": False,
    }
    assert by_role["Mixed.Case"]["usage"] and by_role['we"ird']["usage"]
    assert by_role["pg_monitor"]["usage"]
    assert not by_role["a.b"]["usage"] and not by_role["PG_MONITOR"]["usage"]
    for rule, expected in [
        ({"role": "Mixed.Case"}, "granted"),
        ({"role": 'we"ird'}, "granted"),
        ({"role": "a.b"}, "denied"),
        ({"role": "PG_MONITOR"}, "denied"),
    ]:
        assert ax.verdict(ax.parse_rule(rule)[0], facts)[0] == expected, rule
