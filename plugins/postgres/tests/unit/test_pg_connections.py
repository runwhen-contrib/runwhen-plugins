"""pg-connections/connections through its script, with psql answered by a fake: the limits, the
usable-slot percentage, the counts, and what a role without pg_read_all_stats is told."""

import json
from pathlib import Path

import jsonschema
import pytest
from pg_connections import connections as cn
from pg_connections.errors import RwError

SCHEMA = json.loads(
    (
        Path(__file__).parents[2] / "skills" / "pg-connections" / "schemas" / "connections.v1.json"
    ).read_text()
)

BY_STATE = {
    "active": 2,
    "idle": 5,
    "idleInTransaction": 1,
    "idleInTransactionAborted": 0,
    "fastpath": 0,
    "disabled": 0,
    "nullState": 0,
}


def group(items, total=None):
    return {"items": items, "total": len(items) if total is None else total}


def row(**over):
    base = {
        "fullView": True,
        "maxConnections": 100,
        "superuserReservedConnections": 3,
        "reservedConnections": 0,
        "inUse": 8,
        "hiddenSessions": 0,
        "byState": BY_STATE,
        "byBackendType": group(
            [
                {"backendType": "client backend", "count": 8},
                {"backendType": "walsender", "count": 1},
            ]
        ),
        "byDatabase": group(
            [{"database": "orders", "count": 7}, {"database": "postgres", "count": 1}]
        ),
        "byUserApplication": group(
            [
                {"user": "orders_app", "applicationName": "api", "count": 6},
                {"user": "orders_app", "applicationName": "", "count": 2},
            ]
        ),
        "oldestIdleInTransactionMs": 41000,
        "oldestTransactionMs": 52000,
        "waitingOnLock": 1,
    }
    base.update(over)
    return base


def answer(fake, **over):
    fake.on("FROM pg_stat_activity", stdout=json.dumps(row(**over)))


def run_connections(run_script, **kwargs):
    return run_script("connections", skill="pg-connections", **kwargs)


def test_a_healthy_instance_reports_the_limits_the_percentage_and_the_counts(run_script, fake_psql):
    answer(fake_psql)
    env = run_connections(run_script).envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["connections"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["visibility"] == "full" and out["hiddenSessions"] == 0
    assert (out["maxConnections"], out["superuserReservedConnections"]) == (100, 3)
    assert (out["reservedConnections"], out["usableConnections"]) == (0, 97)
    assert (out["inUse"], out["usedPercent"]) == (8, 8.2)
    assert out["byState"] == BY_STATE
    assert out["byBackendType"]["items"][1] == {"backendType": "walsender", "count": 1}
    assert out["byUserApplication"] == {
        "items": [
            {"user": "orders_app", "applicationName": "api", "count": 6},
            {"user": "orders_app", "applicationName": "", "count": 2},
        ],
        "total": 2,
        "omitted": 0,
    }
    assert (out["oldestIdleInTransactionMs"], out["oldestTransactionMs"]) == (41000, 52000)
    assert out["waitingOnLock"] == 1


def test_the_percentage_is_against_the_usable_limit_not_max_connections(run_script, fake_psql):
    answer(fake_psql, reservedConnections=5, inUse=46)
    out = run_connections(run_script).envelope["outputs"]["connections"]
    assert out["usableConnections"] == 92  # 100 - 3 superuser - 5 reserved
    assert out["usedPercent"] == 50.0


def test_a_server_without_reserved_connections_reports_null_and_uses_the_other_reserve(
    run_script, fake_psql
):
    answer(fake_psql, reservedConnections=None, inUse=97)
    out = run_connections(run_script).envelope["outputs"]["connections"]
    assert out["reservedConnections"] is None
    assert (out["usableConnections"], out["usedPercent"]) == (97, 100.0)
    jsonschema.Draft202012Validator(SCHEMA).validate(out)


def test_no_usable_slot_gives_no_percentage():
    facts = cn.shape(row(maxConnections=3, superuserReservedConnections=3, inUse=0))
    assert facts["usableConnections"] == 0 and facts["usedPercent"] is None


def test_a_role_that_cannot_see_other_sessions_is_partial_and_its_blind_spots_are_null(
    run_script, fake_psql
):
    answer(
        fake_psql,
        fullView=False,
        hiddenSessions=8,
        byState={**BY_STATE, "active": 0, "idle": 0, "idleInTransaction": 0, "nullState": 8},
        oldestIdleInTransactionMs=None,
        oldestTransactionMs=3,  # the role's own session: would understate the real oldest
        waitingOnLock=0,
    )
    env = run_connections(run_script).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("permission_denied", "pg_stat_activity")
    ]
    assert "pg_read_all_stats" in env["errors"][0]["message"]
    out = env["outputs"]["connections"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["visibility"] == "partial" and out["hiddenSessions"] == 8
    assert out["byState"]["nullState"] == 8
    assert out["oldestIdleInTransactionMs"] is None
    assert out["oldestTransactionMs"] is None  # never the visible subset's value
    assert out["waitingOnLock"] is None
    assert out["inUse"] == 8  # still every client session, hidden ones included


def test_the_statement_leaves_out_the_tasks_own_backend_and_reads_only(run_script, fake_psql):
    answer(fake_psql)
    run_connections(run_script)
    (call,) = fake_psql.calls
    assert "a.pid <> pg_backend_pid()" in call["sql"]
    assert call["argv"][:7] == ["psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1"]
    clauses = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "-c"]
    assert clauses[0].startswith("BEGIN READ ONLY; SET LOCAL statement_timeout = 30000; ")
    assert clauses[0].endswith("; COMMIT;")
    assert "{limit}" not in call["sql"] and "LIMIT 200" in call["sql"]


def test_max_items_cuts_each_group_and_reports_what_was_left_out(run_script, fake_psql):
    answer(fake_psql, byDatabase=group([{"database": "orders", "count": 7}], total=40))
    run = run_connections(run_script, settings={"maxItems": 1})
    out = run.envelope["outputs"]["connections"]
    assert out["byDatabase"] == {
        "items": [{"database": "orders", "count": 7}],
        "total": 40,
        "omitted": 39,
    }
    assert "LIMIT 1)" in fake_psql.calls[0]["sql"]


@pytest.mark.parametrize(
    "stderr, code",
    [
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  password authentication failed for user "app"',
            "permission_denied",
        ),
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  database "orders" does not exist',
            "not_found",
        ),
        (
            'psql: error: connection to server at "h", port 5432 failed: Connection refused',
            "unreachable",
        ),
        ("ERROR:  canceling statement due to statement timeout", "timeout"),
    ],
)
def test_a_failed_psql_fails_the_task_with_its_code_and_no_facts(
    run_script, fake_psql, stderr, code
):
    fake_psql.on("FROM pg_stat_activity", rc=2, stderr=stderr)
    run = run_connections(run_script)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == [code]
    assert not run.envelope.get("outputs")


def test_an_empty_answer_is_an_internal_error_not_an_empty_report(run_script, fake_psql):
    fake_psql.on("FROM pg_stat_activity", stdout="")
    run = run_connections(run_script)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == ["internal"]
    with pytest.raises(RwError):
        cn.shape(None)


@pytest.mark.parametrize(
    "settings", [{"maxItems": 0}, {"maxItems": True}, {"statementTimeout": "soon"}]
)
def test_a_bad_setting_is_invalid_input_before_any_query(run_script, fake_psql, settings):
    run = run_connections(run_script, settings=settings)
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]
    assert fake_psql.calls == []
