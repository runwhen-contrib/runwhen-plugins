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


def raw_blocking(
    waiters, blockers, full=True, database="orders", version=160015, prepared=(), shown=None
):
    """The statement's row: `edges` for every waiter (pid, blockers, prepared gids), `waiters`
    with the text columns for the first `shown` of them (the SQL cap), the root blockers."""
    edges = [
        {"pid": w["pid"], "blockedBy": w["blockedBy"], "prepared": w.get("prepared", [])}
        for w in waiters
    ]
    rows = [
        {k: v for k, v in w.items() if k not in ("blockedBy", "prepared")}
        for w in (waiters if shown is None else waiters[:shown])
    ]
    return {
        "fullView": full,
        "serverVersionNum": version,
        "database": database,
        "edges": edges,
        "waiters": rows,
        "blockers": blockers,
        "preparedTransactions": list(prepared),
    }


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
            "preparedTransaction": None,
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
    assert out["waitTimesUnavailable"] is False
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


def test_the_sql_cuts_the_rows_that_carry_text_but_the_chains_use_every_waiter():
    # 5 waiters wait; the statement returned the text of the 2 longest. Depth, the roots and the
    # waiters behind each root come from all five.
    raw = raw_blocking(
        [waiter(100 + i, [1], wait_ms=9000 - i) for i in range(4)] + [waiter(200, [100])],
        [root(1)],
        shown=2,
    )
    out = bl.build_blocking(raw, 2)
    assert [w["pid"] for w in out["waiters"]["items"]] == [100, 101]
    assert (out["waiters"]["total"], out["waiters"]["omitted"], out["waiters"]["truncated"]) == (
        5,
        3,
        True,
    )
    assert out["maxDepth"] == 2
    assert out["rootBlockers"]["items"][0]["blockedWaiters"] == 5


def test_blocking_sql_applies_the_cap_in_the_statement():
    sql = bl.blocking_sql(7)
    assert sql.count("LIMIT 7") == 3 and "{limit}" not in sql
    assert "FROM edges ORDER BY wait_ms DESC NULLS LAST, pid LIMIT 7" in sql
    assert "ORDER BY x.direct DESC, a.xact_start NULLS LAST, x.pid LIMIT 7" in sql
    assert "AS MATERIALIZED" in sql  # pg_locks is read once


def test_blocked_by_and_root_blocker_pids_are_cut_at_50_with_explicit_flags():
    many = list(range(1000, 1060))
    raw = raw_blocking(
        [waiter(1, many), waiter(2, [3]), waiter(3, [4])], [root(p) for p in many] + [root(4)]
    )
    out = bl.build_blocking(raw, 200)
    by_pid = {w["pid"]: w for w in out["waiters"]["items"]}
    one = by_pid[1]
    assert one["blockedBy"] == many[:50] and one["blockedByTruncated"] is True
    assert one["rootBlockerPids"] == many[:50] and one["rootBlockerPidsTruncated"] is True
    assert by_pid[2]["blockedByTruncated"] is False
    assert by_pid[2]["rootBlockerPidsTruncated"] is False and by_pid[2]["rootBlockerPids"] == [4]
    assert out["rootBlockers"]["total"] == 61  # every root, not the 50 a waiter lists


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
    assert "{limit}" not in call["sql"] and "LIMIT 200" in call["sql"]


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
    "serverVersionNum": 160015,
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


def test_lock_summary_on_13_has_a_null_longest_wait_and_says_why():
    out = sm.build_summary({**RAW_SUMMARY, "serverVersionNum": 130012}, 200)
    assert out["locks"]["longestWaitMs"] is None and out["locks"]["waitTimesUnavailable"] is True
    assert out["locks"]["waiting"] == 2  # the counts do not need waitstart
    assert sm.build_summary(RAW_SUMMARY, 200)["locks"]["waitTimesUnavailable"] is False


def test_a_zero_capacity_gives_a_null_fill_ratio():
    out = sm.build_summary({**RAW_SUMMARY, "capacity": 0, "tableEntries": 0}, 200)
    assert out["lockTable"]["estimatedFillRatio"] is None


def test_a_non_json_object_answer_is_an_internal_error(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout="null")
    assert [e["code"] for e in run_script("blocking").envelope["errors"]] == ["internal"]


# --- prepared transactions --------------------------------------------------------------------

GX = {"gid": "migrate-7", "prepared": 93000, "owner": "deploy", "database": "orders"}
GY = {"gid": "migrate-8", "prepared": 41000, "owner": "deploy", "database": "billing"}


def test_a_prepared_transaction_is_a_root_blocker_with_its_gid_age_owner_and_database():
    raw = raw_blocking([waiter(454, [0], prepared=["migrate-7"])], [], prepared=[GX])
    out = bl.build_blocking(raw, 200)
    (root_,) = out["rootBlockers"]["items"]
    assert root_["pid"] == 0 and root_["blockedWaiters"] == 1
    assert root_["preparedTransaction"] == GX
    # never an all-null root blocker: its database and owner are the transaction's own
    assert (root_["datname"], root_["usename"]) == ("orders", "deploy")
    (w,) = out["waiters"]["items"]
    assert w["blockedBy"] == [0] and w["rootBlockerPids"] == [0] and w["depth"] == 1
    assert w["preparedTransactions"] == [GX]


def test_a_chain_behind_a_prepared_transaction_counts_every_waiter_behind_it():
    raw = raw_blocking(
        [waiter(1, [0], prepared=["migrate-7"]), waiter(2, [1]), waiter(3, [2, 1])],
        [],
        prepared=[GX],
    )
    out = bl.build_blocking(raw, 200)
    (root_,) = out["rootBlockers"]["items"]
    assert (root_["pid"], root_["blockedWaiters"]) == (0, 3)
    assert out["maxDepth"] == 3


def test_two_prepared_transactions_are_two_root_blockers_each_with_its_own_gid():
    raw = raw_blocking(
        [
            waiter(1, [0, 0], prepared=["migrate-7", "migrate-8"]),
            waiter(2, [0], prepared=["migrate-8"]),
        ],
        [],
        prepared=[GX, GY],
    )
    out = bl.build_blocking(raw, 200)
    roots = {r["preparedTransaction"]["gid"]: r for r in out["rootBlockers"]["items"]}
    assert set(roots) == {"migrate-7", "migrate-8"}
    assert (roots["migrate-7"]["blockedWaiters"], roots["migrate-8"]["blockedWaiters"]) == (1, 2)
    assert roots["migrate-8"]["datname"] == "billing"


def test_a_prepared_transaction_that_ended_between_the_reads_is_still_a_pid_zero_root():
    out = bl.build_blocking(raw_blocking([waiter(1, [0])], []), 200)
    (root_,) = out["rootBlockers"]["items"]
    assert root_["pid"] == 0 and root_["blockedWaiters"] == 1
    assert root_["preparedTransaction"] == {
        "gid": None,
        "prepared": None,
        "owner": None,
        "database": None,
    }


def test_a_session_root_and_a_prepared_root_are_listed_together(run_script, fake_psql):
    raw = raw_blocking(
        [waiter(1, [0], prepared=["migrate-7"]), waiter(2, [441])], [root(441)], prepared=[GX]
    )
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(raw))
    out = run_script("blocking").envelope["outputs"]["blocking"]
    assert sorted(r["pid"] for r in out["rootBlockers"]["items"]) == [0, 441]
    assert [r["preparedTransaction"] is None for r in out["rootBlockers"]["items"]].count(True) == 1
    assert_valid(SKILL, "blocking", "blocking", out)


def test_the_statement_finds_a_prepared_transaction_through_the_granted_lock_rows_without_a_pid():
    sql = bl.blocking_sql(200)
    assert "FROM pg_prepared_xacts p" in sql
    assert "t.pid IS NULL AND t.granted AND t.locktype = 'transactionid'" in sql
    assert "t.transactionid = p.transaction" in sql
    assert "pr.vxid = h.virtualtransaction" in sql
    assert "WHERE 0 = ANY(e.blockers)" in sql and "b.pid <> 0" in sql  # pid 0 is no session root


# --- server versions --------------------------------------------------------------------------


def test_postgres_13_has_no_waitstart_so_wait_times_are_null_and_say_why():
    raw = raw_blocking([waiter(454, [441], 5000)], [root(441)], version=130012)
    out = bl.build_blocking(raw, 200)
    assert out["waitTimesUnavailable"] is True
    assert out["waiters"]["items"][0]["waitMs"] is None
    assert out["rootBlockers"]["total"] == 1  # everything else is still reported


def test_waitstart_is_read_through_to_jsonb_so_the_statements_run_on_13():
    for sql in (bl.blocking_sql(5), sm.SUMMARY_SQL):
        assert "(to_jsonb(" in sql and "->> 'waitstart')::timestamptz" in sql
        assert "current_setting('server_version_num')::int" in sql


def test_below_13_the_run_fails_with_dependency(run_script, fake_psql):
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(raw_blocking([], [], version=120022)))
    fake_psql.on(
        "max_locks_per_transaction", stdout=json.dumps({**RAW_SUMMARY, "serverVersionNum": 120022})
    )
    for script in ("blocking", "lock_summary"):
        env = run_script(script).envelope
        assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["dependency"]
        assert "PostgreSQL 13 or later required" in env["errors"][0]["message"]


def test_a_missing_version_is_an_internal_error(run_script, fake_psql):
    raw = raw_blocking([], [])
    del raw["serverVersionNum"]
    fake_psql.on("pg_blocking_pids", stdout=json.dumps(raw))
    assert [e["code"] for e in run_script("blocking").envelope["errors"]] == ["internal"]


# --- the declared permissions ---------------------------------------------------------------
# which functions each task declares is checked against its SQL in test_pg_copies.py


def test_blocking_declares_pg_read_all_stats_and_lock_summary_no_role():
    assert {"role": "pg_read_all_stats"} in tasks(SKILL)["blocking"]["permissions"]
    assert not [p for p in tasks(SKILL)["lock-summary"]["permissions"] if "role" in p]


def test_each_task_names_its_script_and_every_output_has_a_schema():
    for name, task in tasks(SKILL).items():
        assert task["readOnly"] is True, name
        for out in task["outputs"].values():
            assert out["schema"].endswith(".v1.json")
