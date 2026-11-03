"""The health skills' statements against the real local servers (the docker harness of
tests/docker/): what a fake psql cannot show, like a prepared transaction that has no backend, the
server's own cut of a statement, or a standby's receiver.

Needs the harness on 127.0.0.1:55432 (primary) and :55433 (standby), docker and a psql client;
skipped when any is missing. Every test cleans up what it started: a prepared transaction is
rolled back even when the test fails."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from pg_connections import connections as cn
from pg_locks import blocking as bl
from pg_locks.pg import session_sql
from pg_long_running_queries import long_running as lr
from pg_replication_lag import replication as rp

DOCKER = Path(__file__).parents[1] / "docker"
PSQL = shutil.which("psql") or next(
    (
        p
        for p in ("/opt/homebrew/opt/libpq/bin/psql", "/usr/local/opt/libpq/bin/psql")
        if os.path.exists(p)
    ),
    None,
)
PASSWORDS = {"rw_monitor": "rw-test-monitor", "rw_noperm": "rw-test-noperm"}  # throwaway values


def libpq(user="rw_monitor", port=55432, database="orders"):
    return {
        **os.environ,
        "PGHOST": "127.0.0.1",
        "PGPORT": str(port),
        "PGUSER": user,
        "PGPASSWORD": PASSWORDS[user],
        "PGDATABASE": database,
        "PGSSLMODE": "disable",
        "PGCONNECT_TIMEOUT": "2",
    }


def query(sql, **where):
    """The one JSON value `sql` returns, in the read-only wrapper the tasks use."""
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-c", session_sql(sql, 20)],
        env=libpq(**where),
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def reachable(port):
    if not PSQL or not shutil.which("docker"):
        return False
    done = subprocess.run(
        [PSQL, "-X", "-A", "-t", "-q", "-c", "SELECT 1"],
        env=libpq(port=port),
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


pytestmark = pytest.mark.skipif(
    not (reachable(55432) and reachable(55433)), reason="the docker harness is not running"
)


@pytest.fixture
def prepared_chain():
    """A PREPARE TRANSACTION holding ACCESS EXCLUSIVE on lock_fixture, and a waiter behind it."""
    script = str(DOCKER / "hold-prepared.sh")
    try:
        subprocess.run([script, "start"], check=True, capture_output=True, timeout=120)
        yield
    finally:  # always: a prepared transaction outlives every connection
        subprocess.run([script, "stop"], capture_output=True, timeout=120)


def terminate_live_sessions():
    """End the rw-fixture-live backends as the superuser (a killed client leaves pg_sleep running)."""
    subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            "PGPASSWORD=rw-test-superuser",
            "rw-pg-primary",
            "psql",
            "-X",
            "-q",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'rw-fixture-live'",
        ],
        capture_output=True,
        timeout=60,
    )


@pytest.fixture
def statement_session():
    """Start `psql -c <sql>` as rw_monitor with application_name rw-fixture-live; end it after."""
    started = []

    def start(sql):
        proc = subprocess.Popen(
            [PSQL, "-X", "-q", "-A", "-t", "-c", sql],
            env={**libpq(), "PGAPPNAME": "rw-fixture-live"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(proc)
        for _ in range(50):  # until the backend shows up
            seen = query(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'rw-fixture-live'"
            )
            if seen == 1:
                return proc
            time.sleep(0.2)
        raise AssertionError("the session did not start")

    terminate_live_sessions()
    yield start
    for proc in started:
        proc.kill()
        proc.wait()
    terminate_live_sessions()


def test_a_prepared_transaction_blocks_and_is_reported_with_its_gid(prepared_chain):
    out = bl.build_blocking(query(bl.blocking_sql(200)), 200)
    assert out["waitTimesUnavailable"] is False
    (waiter,) = [
        w for w in out["waiters"]["items"] if w["applicationName"] == "rw-fixture-prepared"
    ]
    assert waiter["blockedBy"] == [0] and waiter["rootBlockerPids"] == [0]
    assert waiter["relation"] == "public.lock_fixture" and waiter["waitMs"] > 0
    (prepared,) = waiter["preparedTransactions"]
    assert prepared["gid"] == "rw_fixture_prepared" and prepared["owner"] == "postgres"
    assert prepared["database"] == "orders" and prepared["prepared"] > 0
    (root,) = [r for r in out["rootBlockers"]["items"] if r["pid"] == 0]
    assert root["preparedTransaction"] == prepared and root["blockedWaiters"] == 1
    assert (root["datname"], root["usename"]) == ("orders", "postgres")  # never all null


def test_long_running_marks_pid_zero_and_lists_the_prepared_transaction(prepared_chain):
    row = query(lr.long_running_sql(1, 200, False))
    (item,) = [i for i in row["items"] if i["applicationName"] == "rw-fixture-prepared"]
    assert item["blockedBy"] == [0] and item["blockedByTruncated"] is False
    (prepared,) = item["preparedTransactions"]
    assert prepared["gid"] == "rw_fixture_prepared" and prepared["database"] == "orders"


def test_a_statement_the_server_cut_is_flagged_truncated(statement_session):
    long_text = "x" * 1500
    statement_session(f"select pg_sleep(30), '{long_text}'")
    row = query(lr.long_running_sql(1, 200, True))
    (item,) = [i for i in row["items"] if i["applicationName"] == "rw-fixture-live"]
    assert item["queryTruncated"] is True  # track_activity_query_size is 1024 bytes
    assert long_text not in json.dumps(item)


def test_a_short_statement_is_not_flagged_truncated(statement_session):
    statement_session("select pg_sleep(30), 'short'")
    row = query(lr.long_running_sql(1, 200, True))
    (item,) = [i for i in row["items"] if i["applicationName"] == "rw-fixture-live"]
    assert item["queryTruncated"] is False and item["query"] == "select pg_sleep(?), ?"


def test_the_track_activity_query_size_setting_is_read_in_bytes_from_pg_settings():
    assert query(
        "SELECT json_build_object('size', (SELECT setting::int FROM pg_settings "
        "WHERE name = 'track_activity_query_size'))"
    ) == {"size": 1024}


def test_a_standby_reports_its_receivers_liveness():
    sql = rp.standby_sql(160015)
    row = query(sql, port=55433)
    receiver = row["receiver"]
    assert receiver["status"] == "streaming"
    assert isinstance(receiver["lastMsgReceiptAgeMs"], int) and receiver["lastMsgReceiptAgeMs"] >= 0
    assert receiver["latestEndLsn"] and row["receiveLsn"]
    assert row["replayPauseState"] == "not paused"


def test_the_statement_for_a_server_below_14_runs_and_has_a_null_pause_state():
    row = query(rp.standby_sql(130000), port=55433)
    assert row["replayPauseState"] is None and row["replayPaused"] is False


def test_the_role_statement_reads_the_version_and_the_primary_statement_is_capped():
    role = query(rp.ROLE_SQL)
    assert role["serverVersionNum"] >= 130000 and role["inRecovery"] is False
    row = query(rp.primary_sql(1))
    assert row["replicas"]["total"] >= 1 and len(row["replicas"]["items"]) == 1
    assert row["slots"]["total"] >= 1 and len(row["slots"]["items"]) <= 1
    assert row["currentLsn"]


def test_a_role_that_cannot_see_other_sessions_counts_none_of_them_as_in_use(statement_session):
    statement_session("select pg_sleep(30)")
    row = query(cn.connections_sql(5), user="rw_noperm", database="postgres")
    assert row["fullView"] is False
    assert row["inUse"] == 0  # the one visible row is the task's own, which is left out
    assert row["hiddenSessions"] >= 1
    assert row["byBackendType"]["items"][0]["backendType"] is None  # the rows it cannot read
    full = query(cn.connections_sql(5))
    assert full["fullView"] is True and full["inUse"] >= 1 and full["hiddenSessions"] == 0
