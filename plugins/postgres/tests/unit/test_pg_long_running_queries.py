"""pg-long-running-queries/long-running through its script, with psql answered by a fake: the
sessions it lists, the query text it withholds or normalizes, the cap and the partial case."""

import json
from pathlib import Path

import jsonschema
import pytest
from pg_long_running_queries import long_running as lr

SCHEMA = json.loads(
    (
        Path(__file__).parents[2]
        / "skills"
        / "pg-long-running-queries"
        / "schemas"
        / "long-running.v1.json"
    ).read_text()
)


def item(pid, duration, kind="active", **over):
    base = {
        "pid": pid,
        "database": "orders",
        "user": "orders_app",
        "applicationName": "api",
        "clientAddr": "10.0.0.7",
        "state": "active" if kind == "active" else "idle in transaction",
        "kind": kind,
        "durationMs": duration,
        "xactAgeMs": duration + 1000,
        "waitEventType": None,
        "waitEvent": None,
        "backendType": "client backend",
        "queryId": None,
        "blockedBy": [],
        "blockedByTruncated": False,
        "preparedTransactions": [],
    }
    base.update(over)
    return base


def answer(fake, items, total=None, **over):
    row = {
        "fullView": True,
        "hiddenSessions": 0,
        "total": len(items) if total is None else total,
        "items": items,
    }
    row.update(over)
    fake.on("FROM pg_stat_activity", stdout=json.dumps(row))


def run_long(run_script, settings=None, **inputs):
    return run_script("long_running", skill="pg-long-running-queries", settings=settings, **inputs)


def sql_of(fake):
    (call,) = fake.calls
    return call["sql"]


def test_it_lists_the_sessions_with_their_facts_and_validates_against_the_schema(
    run_script, fake_psql
):
    answer(
        fake_psql,
        [
            item(11, 900000, waitEventType="Lock", waitEvent="relation", blockedBy=[12]),
            item(12, 600000, kind="idleInTransaction", queryId="-4141", blockedBy=[]),
        ],
    )
    env = run_long(run_script).envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["longRunning"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["visibility"] == "full" and out["minDurationMs"] == 300000
    assert (out["total"], out["omitted"], out["truncated"]) == (2, 0, False)
    first, second = out["items"]
    assert first["pid"] == 11 and first["blockedBy"] == [12] and first["waitEventType"] == "Lock"
    assert second["kind"] == "idleInTransaction" and second["queryId"] == "-4141"
    assert "query" not in first and out["queryTextIncluded"] is False


def test_min_duration_goes_to_the_server_in_milliseconds(run_script, fake_psql):
    answer(fake_psql, [])
    run_long(run_script, min_duration="90s")
    assert "interval '1 millisecond' * 90000" in sql_of(fake_psql)
    fake_psql.calls.clear()
    run_long(run_script)  # the default, 5m
    assert "interval '1 millisecond' * 300000" in sql_of(fake_psql)


def test_the_statement_excludes_the_tasks_own_backend_and_walsenders_and_computes_on_the_server(
    run_script, fake_psql
):
    answer(fake_psql, [])
    run_long(run_script)
    sql = sql_of(fake_psql)
    assert "a.pid <> pg_backend_pid()" in sql
    assert "a.backend_type <> 'walsender'" in sql
    assert "now() - a.query_start" in sql and "now() - a.state_change" in sql
    assert "now() - a.xact_start" in sql
    assert "ORDER BY dur DESC" in sql and "LIMIT 200" in sql
    assert "query_id" in sql and "pg_blocking_pids(h.pid)" in sql
    assert "'{" not in sql and "{min_ms}" not in sql and "{limit}" not in sql


def test_a_bad_duration_is_invalid_input_before_any_query(run_script, fake_psql):
    run = run_long(run_script, min_duration="five minutes")
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]
    assert fake_psql.calls == []


def test_query_text_is_not_even_selected_by_default(run_script, fake_psql):
    answer(fake_psql, [item(1, 600000)])
    run_long(run_script)
    assert "a.query" not in sql_of(fake_psql).replace("a.query_start", "").replace("a.query_id", "")
    assert "'query'" not in sql_of(fake_psql)


def test_query_text_is_normalized_inside_the_statement_so_raw_text_never_leaves_the_database(
    run_script, fake_psql
):
    answer(fake_psql, [item(1, 600000, query="select ?", queryTruncated=False)])
    run_long(run_script, settings={"includeQueryText": True})
    sql = sql_of(fake_psql)
    assert sql.count("regexp_replace(") == 7  # dollar, literals, unclosed, numbers + 3 clean-ups
    assert "'query', left(query_norm, 1024)" in sql
    assert "'query', query\n" not in sql and "'query', query," not in sql
    # the regexes run on the capped rows only, after the filter
    assert sql.index("regexp_replace(") > sql.index("hits AS")
    # off by default: no normalization, no text
    fake_psql.calls.clear()
    run_long(run_script)
    assert "regexp_replace(" not in sql_of(fake_psql)


def test_a_cut_made_by_the_statement_is_reported_as_truncated(run_script, fake_psql):
    answer(
        fake_psql,
        [item(1, 900000, query="select ?", queryTruncated=True), item(2, 800000, query="select ?")],
    )
    out = run_long(run_script, settings={"includeQueryText": True}).envelope["outputs"]
    a, b = out["longRunning"]["items"]
    assert (a["query"], a["queryTruncated"]) == ("select ?", True)
    assert (b["query"], b["queryTruncated"]) == ("select ?", False)


def test_query_text_is_normalized_and_cut_when_the_setting_is_on(run_script, fake_psql):
    raw = "select * from users where email = 'bob@example.com' and id = 42"
    long_raw = "select " + ", ".join(f"col_{i} = 'secret{i}'" for i in range(300))
    answer(
        fake_psql,
        [item(1, 900000, query=raw), item(2, 800000, query=long_raw), item(3, 700000, query=None)],
    )
    run = run_long(run_script, settings={"includeQueryText": True})
    out = run.envelope["outputs"]["longRunning"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["queryTextIncluded"] is True
    a, b, c = out["items"]
    assert a["query"] == "select * from users where email = ? and id = ?"
    assert a["queryTruncated"] is False
    assert len(b["query"].encode("utf-8")) <= 1024 and b["queryTruncated"] is True
    assert "secret" not in b["query"]
    assert (c["query"], c["queryTruncated"]) == (None, False)
    assert "left(a.query, 8192) AS query" in sql_of(fake_psql)
    # the Python pass is the second barrier: this is text a (hypothetical) server did not normalize
    # nothing the server sent as raw text is in the result
    assert "bob@example.com" not in json.dumps(run.envelope)


def test_the_list_is_capped_at_max_items_with_truncated_true(run_script, fake_psql):
    answer(fake_psql, [item(i, 900000 - i) for i in range(1, 4)], total=57)
    run = run_long(run_script, settings={"maxItems": 3})
    out = run.envelope["outputs"]["longRunning"]
    assert (len(out["items"]), out["total"], out["omitted"], out["truncated"]) == (3, 57, 54, True)
    assert "LIMIT 3)" in sql_of(fake_psql)
    assert [i["pid"] for i in out["items"]] == [1, 2, 3]  # the server's longest-first order


def test_none_found_is_an_empty_full_visibility_list(run_script, fake_psql):
    answer(fake_psql, [])
    run = run_long(run_script, min_duration="1h")
    out = run.envelope["outputs"]["longRunning"]
    assert run.envelope["status"] == "ok"
    assert (out["items"], out["total"], out["truncated"], out["visibility"]) == (
        [],
        0,
        False,
        "full",
    )


def test_a_role_that_cannot_see_other_sessions_is_partial_with_a_lower_bound(run_script, fake_psql):
    answer(fake_psql, [], fullView=False, hiddenSessions=8)
    run = run_long(run_script)
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("permission_denied", "pg_stat_activity")
    ]
    out = env["outputs"]["longRunning"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert (out["visibility"], out["hiddenSessions"], out["items"]) == ("partial", 8, [])
    assert "lower bound" in env["errors"][0]["message"]


@pytest.mark.parametrize(
    "stderr, code",
    [
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  password authentication failed for user "app"',
            "permission_denied",
        ),
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  database "nope" does not exist',
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
    run = run_long(run_script)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == [code]
    assert not run.envelope.get("outputs")


@pytest.mark.parametrize(
    "settings",
    [{"includeQueryText": "yes"}, {"includeQueryText": 1}, {"maxItems": 0}],
)
def test_a_bad_setting_is_invalid_input_before_any_query(run_script, fake_psql, settings):
    run = run_long(run_script, settings=settings)
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]
    assert fake_psql.calls == []


def test_min_duration_ms_rounds_and_has_a_floor():
    assert lr.min_duration_ms("5m") == 300000
    assert lr.min_duration_ms("1h30m") == 5400000
    assert lr.min_duration_ms("500ms") == 500


PREPARED = {"gid": "migrate-7", "prepared": 93000, "owner": "deploy", "database": "orders"}


def test_a_session_blocked_by_a_prepared_transaction_lists_it_beside_pid_zero(
    run_script, fake_psql
):
    answer(
        fake_psql,
        [
            item(
                11,
                900000,
                waitEventType="Lock",
                waitEvent="relation",
                blockedBy=[0],
                preparedTransactions=[PREPARED],
            )
        ],
    )
    out = run_long(run_script).envelope["outputs"]["longRunning"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    (blocked,) = out["items"]
    assert blocked["blockedBy"] == [0] and blocked["preparedTransactions"] == [PREPARED]


def test_blocked_by_is_cut_at_50_in_the_statement_and_says_so(run_script, fake_psql):
    answer(fake_psql, [item(11, 900000, blockedBy=list(range(100, 150)), blockedByTruncated=True)])
    out = run_long(run_script).envelope["outputs"]["longRunning"]
    jsonschema.Draft202012Validator(SCHEMA).validate(out)
    assert out["items"][0]["blockedByTruncated"] is True and len(out["items"][0]["blockedBy"]) == 50
    sql = sql_of(fake_psql)
    assert "bp.pids[1:50]" in sql and "cardinality(bp.pids) > 50" in sql
    assert sql.count("pg_blocking_pids(") == 1  # once, for the capped rows only


def test_the_prepared_transactions_are_looked_up_only_for_a_session_blocked_by_pid_zero(
    run_script, fake_psql
):
    answer(fake_psql, [])
    run_long(run_script)
    sql = sql_of(fake_psql)
    assert "CASE WHEN 0 = ANY(bp.pids)" in sql
    assert "FROM pg_prepared_xacts" in sql and "t.pid IS NULL AND t.granted" in sql
    assert "t.transactionid = p.transaction" in sql and "hl.pid IS NULL AND hl.granted" in sql


def test_the_statement_flags_a_text_the_server_itself_cut(run_script, fake_psql):
    answer(fake_psql, [item(1, 900000, query="select ?", queryTruncated=True)])
    run_long(run_script, settings={"includeQueryText": True})
    sql = sql_of(fake_psql)
    # the server keeps track_activity_query_size - 1 bytes, in whole characters (up to 3 fewer);
    # the setting is read from pg_settings in bytes (current_setting returns "1kB")
    assert "octet_length(a.query) >= (SELECT setting::int FROM pg_settings" in sql
    assert "WHERE name = 'track_activity_query_size') - 4" in sql
    assert "OR query_cut_by_server" in sql
    # off by default: no text, no flag
    fake_psql.calls.clear()
    run_long(run_script)
    assert "query_cut_by_server" not in sql_of(fake_psql)
