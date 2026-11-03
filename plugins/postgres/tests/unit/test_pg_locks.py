"""pg-locks through its scripts, with psql answered by a fake: blocking chains and root blockers,
the lock summary, partial visibility, failures, bounds and the declared permissions."""

import json

import pytest
from pg_locks import blocking as bl
from pg_locks import summary as sm
from support_locks_replication import FAILURES, assert_valid, make_runner, tasks

SKILL = "pg-locks"


@pytest.fixture
def run_script(fake_psql):
    return make_runner(SKILL, fake_psql)


def waiter(pid, blocked_by, wait_ms=1000, **kw):
    row = {
        "pid": pid,
        "datname": "orders",
        "usename": "app",
        "applicationName": "api",
        "state": "active",
        "waitMs": wait_ms,
        "lockType": "relation",
        "lockMode": "AccessExclusiveLock",
        "lockDatabase": "orders",
        "relationOid": 16403,
        "relation": "public.lock_fixture",
        "blockedBy": blocked_by,
    }
    row.update(kw)
    return row


def root(pid, state="idle in transaction", xact_age=60000):
    return {
        "pid": pid,
        "datname": "orders",
        "usename": "app",
        "applicationName": "api",
        "state": state,
        "xactAgeMs": xact_age,
    }


def raw_blocking(waiters, blockers, full=True, database="orders"):
    return {"fullView": full, "database": database, "waiters": waiters, "blockers": blockers}


# the fixture shape: 441 holds, 454 waits for 441, 470 waits for 441 and 454
CHAIN = raw_blocking(
    [waiter(454, [441], 5000), waiter(470, [441, 454], 4000)],
    [root(441)],
)


def test_depth_counts_hops_to_a_root_blocker():
    out = bl.build_blocking(CHAIN, 200)
    by_pid = {w["pid"]: w for w in out["waiters"]["items"]}
    assert by_pid[454]["depth"] == 1 and by_pid[470]["depth"] == 2
    assert by_pid[470]["blockedBy"] == [441, 454]
    assert by_pid[470]["rootBlockerPids"] == [441]
    assert out["maxDepth"] == 2


def test_a_root_blocker_has_its_state_age_and_the_number_of_waiters_behind_it():
    out = bl.build_blocking(CHAIN, 200)
    assert out["rootBlockers"]["items"] == [
        {
            "pid": 441,
            "datname": "orders",
            "usename": "app",
            "applicationName": "api",
            "state": "idle in transaction",
            "xactAgeMs": 60000,
            "blockedWaiters": 2,
        }
    ]


def test_two_chains_list_their_roots_most_waiters_first():
    raw = raw_blocking(
        [waiter(10, [1]), waiter(11, [2]), waiter(12, [2]), waiter(13, [12])],
        [root(1, xact_age=999999), root(2, state="active", xact_age=5)],
    )
    out = bl.build_blocking(raw, 200)
    assert [(r["pid"], r["blockedWaiters"]) for r in out["rootBlockers"]["items"]] == [
        (2, 3),
        (1, 1),
    ]
    assert {w["pid"]: w["depth"] for w in out["waiters"]["items"]} == {10: 1, 11: 1, 12: 1, 13: 2}


def test_a_loop_of_waiters_terminates():
    out = bl.build_blocking(raw_blocking([waiter(1, [2]), waiter(2, [1])], []), 200)
    assert len(out["waiters"]["items"]) == 2 and out["rootBlockers"]["total"] == 0


def test_no_waiters_is_an_empty_list_with_zero_total_and_a_null_depth():
    out = bl.build_blocking(raw_blocking([], []), 200)
    assert out["waiters"] == {"items": [], "total": 0, "omitted": 0, "truncated": False}
    assert out["rootBlockers"]["total"] == 0 and out["maxDepth"] is None
    assert out["visibility"] == "full"


def test_lists_are_cut_at_max_items_and_say_so():
    raw = raw_blocking([waiter(100 + i, [1]) for i in range(5)], [root(1)])
    out = bl.build_blocking(raw, 2)
    assert len(out["waiters"]["items"]) == 2
    assert (out["waiters"]["total"], out["waiters"]["omitted"], out["waiters"]["truncated"]) == (
        5,
        3,
        True,
    )
    assert out["rootBlockers"]["items"][0]["blockedWaiters"] == 5  # counts are before the cap


def test_blocking_script_with_a_chain_is_ok_and_matches_its_schema(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(CHAIN))
    run = run_script("blocking")
    env = run.envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["blocking"]
    assert out["visibility"] == "full" and out["waiters"]["total"] == 2
    assert_valid(SKILL, "blocking", "blocking", out)


def test_blocking_script_without_pg_monitor_is_partial_and_names_the_view(run_script, fake_psql):
    hidden = raw_blocking(
        [waiter(454, [441], state=None)], [root(441, state=None, xact_age=None)], full=False
    )
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(hidden))
    env = run_script("blocking").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [
        ("permission_denied", "pg_stat_activity")
    ]
    out = env["outputs"]["blocking"]
    assert out["visibility"] == "partial"
    assert out["rootBlockers"]["items"][0]["state"] is None
    assert_valid(SKILL, "blocking", "blocking", out)


def test_partial_visibility_with_nothing_found_is_still_partial(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(raw_blocking([], [], full=False)))
    env = run_script("blocking").envelope
    assert env["status"] == "partial" and env["outputs"]["blocking"]["visibility"] == "partial"


def test_blocking_script_reads_no_query_text_and_stays_read_only(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(CHAIN))
    run_script("blocking")
    call = fake_psql.calls[0]
    assert call["script"].startswith("BEGIN READ ONLY; SET LOCAL statement_timeout = ")
    assert "query" not in call["sql"].replace("pg_stat_activity", "")
    assert "pg_backend_pid()" in call["sql"]


@pytest.mark.parametrize(("stderr", "code"), FAILURES)
@pytest.mark.parametrize("script", ["blocking", "lock_summary"])
def test_a_failed_psql_fails_the_run_with_its_code(run_script, fake_psql, script, stderr, code):
    fake_psql.on("pg_", rc=2, stderr=stderr)
    fake_psql.on("max_locks_per_transaction", rc=2, stderr=stderr)
    env = run_script(script).envelope
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == [code]


def test_a_missing_psql_is_a_dependency_error(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", rc=127, stderr="psql: not found")
    assert [e["code"] for e in run_script("blocking").envelope["errors"]] == ["dependency"]


@pytest.mark.parametrize("script", ["blocking", "lock_summary"])
def test_bad_settings_are_refused_before_any_query(run_script, fake_psql, script):
    for settings in ({"maxItems": 0}, {"statementTimeout": "soon"}, {"connectTimeout": "0s"}):
        run = run_script(script, settings=settings)
        assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"], settings
    assert fake_psql.calls == []


RAW_SUMMARY = {
    "total": 10,
    "granted": 8,
    "waiting": 2,
    "waitingBackends": 2,
    "longestWaitMs": 90731,
    "byMode": [
        {"mode": "AccessExclusiveLock", "granted": True, "count": 1},
        {"mode": "AccessExclusiveLock", "granted": False, "count": 2},
        {"mode": "ExclusiveLock", "granted": True, "count": 7},
    ],
    "maxLocksPerTransaction": 64,
    "maxConnections": 100,
    "maxPreparedTransactions": 0,
    "capacity": 6400,
    "tableEntries": 640,
    "fastPathLocks": 4,
}


def test_lock_summary_reports_modes_the_longest_wait_and_the_table_fill(run_script, fake_psql):
    fake_psql.on("max_locks_per_transaction", stdout=json.dumps(RAW_SUMMARY))
    env = run_script("lock_summary").envelope
    assert env["status"] == "ok" and env["errors"] == []
    locks, table = env["outputs"]["locks"], env["outputs"]["lockTable"]
    assert locks["longestWaitMs"] == 90731 and locks["waitingBackends"] == 2
    assert {"mode": "AccessExclusiveLock", "granted": False, "count": 2} in locks["byMode"]["items"]
    assert table["capacity"] == 6400 and table["estimatedFillRatio"] == 0.1
    assert_valid(SKILL, "lock-summary", "locks", locks)
    assert_valid(SKILL, "lock-summary", "lockTable", table)


def test_lock_summary_with_nothing_waiting_has_a_null_longest_wait():
    out = sm.build_summary(
        {**RAW_SUMMARY, "waiting": 0, "waitingBackends": 0, "longestWaitMs": None, "byMode": []},
        200,
    )
    assert out["locks"]["longestWaitMs"] is None
    assert out["locks"]["byMode"]["items"] == []


def test_lock_summary_excludes_its_own_backend_and_snapshots_once(fake_psql, run_script):
    fake_psql.on("max_locks_per_transaction", stdout=json.dumps(RAW_SUMMARY))
    run_script("lock_summary")
    sql = fake_psql.calls[0]["sql"]
    assert "pid IS DISTINCT FROM pg_backend_pid()" in sql and "AS MATERIALIZED" in sql


def test_a_zero_capacity_gives_a_null_fill_ratio():
    out = sm.build_summary({**RAW_SUMMARY, "capacity": 0, "tableEntries": 0}, 200)
    assert out["lockTable"]["estimatedFillRatio"] is None


def test_a_non_json_object_answer_is_an_internal_error(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout="null")
    assert [e["code"] for e in run_script("blocking").envelope["errors"]] == ["internal"]


# --- the declared permissions ---------------------------------------------------------------

TRACKED = ("pg_blocking_pids",)


def declared_functions(name):
    return {
        p["function"].split(".")[1] for p in tasks(SKILL)[name]["permissions"] if "function" in p
    }


def test_blocking_declares_the_functions_its_sql_calls_and_pg_monitor():
    assert declared_functions("blocking") == {f for f in TRACKED if f + "(" in bl.BLOCKING_SQL}
    assert {"role": "pg_monitor"} in tasks(SKILL)["blocking"]["permissions"]


def test_lock_summary_needs_no_grant_and_calls_no_restricted_function():
    assert tasks(SKILL)["lock-summary"]["permissions"] == []
    assert "pg_blocking_pids(" not in sm.SUMMARY_SQL and "pg_stat_activity" not in sm.SUMMARY_SQL


def test_each_task_names_its_script_and_every_output_has_a_schema():
    for name, task in tasks(SKILL).items():
        assert task["readOnly"] is True, name
        for out in task["outputs"].values():
            assert out["schema"].endswith(".v1.json")
