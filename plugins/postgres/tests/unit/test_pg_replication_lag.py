"""pg-replication-lag through its script, with psql answered by a fake: the role detection, a primary's
replicas and slots, a standby's receiver and replay position, partial visibility, failures."""

import json

import pytest
from pg_replication_lag import replication as rp
from support_locks_replication import FAILURES, assert_valid, make_runner, tasks

SKILL = "pg-replication-lag"


@pytest.fixture
def run_script(fake_psql):
    return make_runner(SKILL, fake_psql)


def role(in_recovery, full=True, version=160015):
    return {
        "inRecovery": in_recovery,
        "fullView": full,
        "database": "orders",
        "serverVersionNum": version,
    }


def replica(**kw):
    row = {
        "pid": 114,
        "usename": "rw_replicator",
        "applicationName": "walreceiver",
        "clientAddr": "172.20.0.3",
        "state": "streaming",
        "syncState": "async",
        "writeLagMs": 1,
        "flushLagMs": 2,
        "replayLagMs": 3,
        "sentLsnBytes": 0,
        "writeLsnBytes": 0,
        "flushLsnBytes": 0,
        "replayLsnBytes": 128,
    }
    row.update(kw)
    return row


def slot(name="rw_standby", active=True, retained=0, **kw):
    row = {
        "slotName": name,
        "slotType": "physical",
        "database": None,
        "active": active,
        "activePid": 114 if active else None,
        "walStatus": "reserved",
        "retainedWalBytes": retained,
        "safeWalSize": None,
    }
    row.update(kw)
    return row


def block(items, total=None):
    """What the statement returns for a list: the rows it kept and the count it found."""
    return {"total": len(items) if total is None else total, "items": items}


def primary_raw(replicas=None, slots=None, replicas_total=None, slots_total=None):
    replicas = [replica()] if replicas is None else replicas
    slots = [slot()] if slots is None else slots
    return {
        "currentLsn": "0/3069898",
        "replicas": block(replicas, replicas_total),
        "slots": block(slots, slots_total),
    }


STANDBY_RAW = {
    "receiver": {
        "status": "streaming",
        "senderHost": "rw-pg-primary",
        "senderPort": 5432,
        "slotName": "rw_standby",
        "lastMsgReceiptAgeMs": 4200,
        "latestEndLsn": "0/3069898",
    },
    "receiveLsn": "0/3069898",
    "replayLsn": "0/3069898",
    "receivedNotReplayedBytes": 0,
    "receiveEqualsReplay": True,
    "replayDelayMs": 111021,
    "replayPaused": False,
    "replayPauseState": "not paused",
}


def answer(fake, in_recovery, full=True, raw=None):
    fake.on("pg_is_in_recovery()", stdout=json.dumps(role(in_recovery, full)))
    if in_recovery:
        fake.on("pg_stat_wal_receiver", stdout=json.dumps(STANDBY_RAW if raw is None else raw))
    else:
        fake.on("pg_stat_replication", stdout=json.dumps(primary_raw() if raw is None else raw))


def test_a_primary_reports_replicas_and_slots_and_matches_its_schema(run_script, fake_psql):
    answer(fake_psql, False)
    env = run_script("replication_status").envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["replication"]
    assert out["role"] == "primary" and out["visibility"] == "full" and out["standby"] is None
    assert out["primary"]["replicas"]["items"][0]["replayLsnBytes"] == 128
    assert out["primary"]["slots"]["items"][0]["slotName"] == "rw_standby"
    assert_valid(SKILL, "replication-status", "replication", out)


def test_the_primary_query_runs_only_on_a_primary(run_script, fake_psql):
    answer(fake_psql, True)
    run_script("replication_status")
    assert not any("pg_current_wal_lsn" in s for s in fake_psql.sqls())


def test_a_primary_with_no_replica_and_no_slot_is_facts_not_errors(run_script, fake_psql):
    answer(fake_psql, False, raw=primary_raw(replicas=[], slots=[]))
    env = run_script("replication_status").envelope
    assert env["status"] == "ok" and env["errors"] == []
    prim = env["outputs"]["replication"]["primary"]
    assert prim["replicas"] == {"items": [], "total": 0, "omitted": 0, "truncated": False}
    assert prim["slots"]["total"] == 0


def test_an_inactive_slot_shows_its_retained_wal(run_script, fake_psql):
    raw = primary_raw(slots=[slot("rw_standby"), slot("rw_fixture_idle", False, 16777216)])
    answer(fake_psql, False, raw=raw)
    slots = run_script("replication_status").envelope["outputs"]["replication"]["primary"]["slots"]
    idle = next(s for s in slots["items"] if s["slotName"] == "rw_fixture_idle")
    assert idle["active"] is False and idle["retainedWalBytes"] == 16777216


def test_replicas_and_slots_are_cut_at_max_items_in_the_statement_and_say_so(run_script, fake_psql):
    # the statement kept the first 2 of 5 replicas and of 4 slots and counted what it found
    raw = primary_raw(
        replicas=[replica(pid=i) for i in range(2)],
        slots=[slot(f"s{i}") for i in range(2)],
        replicas_total=5,
        slots_total=4,
    )
    answer(fake_psql, False, raw=raw)
    prim = run_script("replication_status", settings={"maxItems": 2}).envelope["outputs"][
        "replication"
    ]["primary"]
    assert (
        len(prim["replicas"]["items"]),
        prim["replicas"]["total"],
        prim["replicas"]["omitted"],
        prim["replicas"]["truncated"],
    ) == (2, 5, 3, True)
    assert prim["slots"]["total"] == 4 and prim["slots"]["omitted"] == 2
    assert prim["slots"]["truncated"] is True
    sql = fake_psql.calls[-1]["sql"]
    assert sql.count("LIMIT 2") == 2 and "{limit}" not in sql


def test_the_current_lsn_is_taken_once_and_every_distance_uses_it():
    sql = rp.primary_sql(200)
    assert sql.count("pg_current_wal_lsn()") == 1
    assert "cur AS MATERIALIZED" in sql
    assert "'currentLsn', (SELECT lsn::text FROM cur)" in sql
    assert sql.count("pg_wal_lsn_diff(cur.lsn,") == 5  # sent, write, flush, replay, retained
    # the standby's two positions are taken once as well
    standby = rp.standby_sql(160000)
    assert standby.count("pg_last_wal_receive_lsn()") == 1
    assert standby.count("pg_last_wal_replay_lsn()") == 1


def test_a_primary_without_pg_monitor_is_partial_scoped_to_pg_stat_replication(
    run_script, fake_psql
):
    hidden = replica(
        state=None, syncState=None, writeLagMs=None, sentLsnBytes=None, replayLsnBytes=None
    )
    answer(fake_psql, False, full=False, raw=primary_raw(replicas=[hidden]))
    env = run_script("replication_status").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [
        ("permission_denied", "pg_stat_replication")
    ]
    out = env["outputs"]["replication"]
    assert (
        out["visibility"] == "partial" and out["primary"]["replicas"]["items"][0]["state"] is None
    )
    assert_valid(SKILL, "replication-status", "replication", out)


def test_a_hidden_empty_replication_view_is_partial_never_healthy(run_script, fake_psql):
    answer(fake_psql, False, full=False, raw=primary_raw(replicas=[], slots=[]))
    env = run_script("replication_status").envelope
    assert env["status"] == "partial" and env["outputs"]["replication"]["visibility"] == "partial"


def test_a_standby_reports_the_receiver_and_the_replay_position(run_script, fake_psql):
    answer(fake_psql, True)
    env = run_script("replication_status").envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["replication"]
    assert out["role"] == "standby" and out["primary"] is None
    st = out["standby"]
    assert st["receiver"]["senderHost"] == "rw-pg-primary" and st["receiver"]["senderPort"] == 5432
    assert st["receiveEqualsReplay"] is True and st["receivedNotReplayedBytes"] == 0
    assert st["replayPaused"] is False and st["replayPauseState"] == "not paused"
    assert_valid(SKILL, "replication-status", "replication", out)


def test_an_idle_primary_shows_a_replay_delay_with_receive_equal_to_replay(run_script, fake_psql):
    answer(fake_psql, True)
    st = run_script("replication_status").envelope["outputs"]["replication"]["standby"]
    assert st["replayDelayMs"] == 111021 and st["receiveEqualsReplay"] is True


def test_a_paused_standby_is_reported(run_script, fake_psql):
    raw = {
        **STANDBY_RAW,
        "replayPaused": True,
        "replayPauseState": "paused",
        "receiveEqualsReplay": False,
        "receivedNotReplayedBytes": 4096,
    }
    answer(fake_psql, True, raw=raw)
    st = run_script("replication_status").envelope["outputs"]["replication"]["standby"]
    assert st["replayPaused"] is True and st["receivedNotReplayedBytes"] == 4096


def test_a_standby_reports_how_long_ago_its_receiver_last_heard_from_the_sender(
    run_script, fake_psql
):
    answer(fake_psql, True)
    out = run_script("replication_status").envelope["outputs"]["replication"]
    receiver = out["standby"]["receiver"]
    assert receiver["lastMsgReceiptAgeMs"] == 4200 and receiver["latestEndLsn"] == "0/3069898"
    sql = rp.standby_sql(160000)
    assert "clock_timestamp() - w.last_msg_receipt_time" in sql and "w.latest_end_lsn::text" in sql


def test_a_cut_off_standby_keeps_its_last_receive_lsn_and_a_stale_last_message(
    run_script, fake_psql
):
    # the receiver is stuck in a retry loop: not streaming, the last message is old, receiveLsn
    # still has its last value, receive equals replay because it replayed all it ever got
    raw = {
        **STANDBY_RAW,
        "receiver": {
            **STANDBY_RAW["receiver"],
            "status": "waiting",
            "lastMsgReceiptAgeMs": 7200000,
        },
    }
    answer(fake_psql, True, raw=raw)
    out = run_script("replication_status").envelope["outputs"]["replication"]
    st = out["standby"]
    assert (
        st["receiver"]["status"] == "waiting" and st["receiver"]["lastMsgReceiptAgeMs"] == 7200000
    )
    assert st["receiveLsn"] == "0/3069898" and st["receiveEqualsReplay"] is True
    assert_valid(SKILL, "replication-status", "replication", out)


def test_a_standby_without_a_receiver_has_a_null_receiver(run_script, fake_psql):
    raw = {
        **STANDBY_RAW,
        "receiver": None,
        "receiveLsn": None,
        "receivedNotReplayedBytes": None,
        "receiveEqualsReplay": None,
        "replayDelayMs": None,
    }
    answer(fake_psql, True, raw=raw)
    out = run_script("replication_status").envelope["outputs"]["replication"]
    assert out["standby"]["receiver"] is None
    assert_valid(SKILL, "replication-status", "replication", out)


def test_a_standby_without_pg_monitor_is_partial_scoped_to_the_wal_receiver(run_script, fake_psql):
    hidden = {
        **STANDBY_RAW,
        "receiver": {
            "status": None,
            "senderHost": None,
            "senderPort": None,
            "slotName": None,
            "lastMsgReceiptAgeMs": None,
            "latestEndLsn": None,
        },
    }
    answer(fake_psql, True, full=False, raw=hidden)
    env = run_script("replication_status").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [
        ("permission_denied", "pg_stat_wal_receiver")
    ]
    assert env["outputs"]["replication"]["visibility"] == "partial"


def test_every_query_is_read_only_and_times_are_computed_on_the_server(run_script, fake_psql):
    answer(fake_psql, True)
    run_script("replication_status")
    for call in fake_psql.calls:
        assert call["script"].startswith("BEGIN READ ONLY; SET LOCAL statement_timeout = ")
    assert "clock_timestamp()" in rp.STANDBY_SQL and "now()" not in rp.STANDBY_SQL
    assert "clock_timestamp()" not in rp.PRIMARY_SQL  # the lag intervals come as columns


@pytest.mark.parametrize(("stderr", "code"), FAILURES)
def test_a_failed_psql_fails_the_run_with_its_code(run_script, fake_psql, stderr, code):
    fake_psql.on("pg_is_in_recovery()", rc=2, stderr=stderr)
    env = run_script("replication_status").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == [code]


def test_a_failure_after_the_role_is_known_still_fails(run_script, fake_psql):
    fake_psql.on("pg_is_in_recovery()", stdout=json.dumps(role(False)))
    fake_psql.on(
        "pg_stat_replication",
        rc=1,
        stderr="ERROR:  permission denied for function pg_current_wal_lsn",
    )
    env = run_script("replication_status").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["permission_denied"]


def test_bad_settings_are_refused_before_any_query(run_script, fake_psql):
    for settings in ({"maxItems": -1}, {"statementTimeout": "1 minute"}):
        env = run_script("replication_status", settings=settings).envelope
        assert [e["code"] for e in env["errors"]] == ["invalid_input"], settings
    assert fake_psql.calls == []


def test_a_non_json_answer_is_an_internal_error(run_script, fake_psql):
    fake_psql.on("pg_is_in_recovery()", stdout="null")
    assert [e["code"] for e in run_script("replication_status").envelope["errors"]] == ["internal"]


# --- server versions ----------------------------------------------------------------------------


def test_the_server_version_is_read_once_with_the_role():
    assert "current_setting('server_version_num')::int" in rp.ROLE_SQL
    assert "server_version_num" not in rp.primary_sql(5) + rp.standby_sql(140000)


def test_a_standby_below_14_leaves_the_pause_state_out_of_its_statement(run_script, fake_psql):
    fake_psql.on("pg_is_in_recovery()", stdout=json.dumps(role(True, version=130012)))
    fake_psql.on(
        "pg_stat_wal_receiver", stdout=json.dumps({**STANDBY_RAW, "replayPauseState": None})
    )
    env = run_script("replication_status").envelope
    assert env["status"] == "ok"
    st = env["outputs"]["replication"]["standby"]
    assert st["replayPauseState"] is None and st["replayPaused"] is False
    assert_valid(SKILL, "replication-status", "replication", env["outputs"]["replication"])
    standby_sql = fake_psql.calls[-1]["sql"]
    assert "pg_get_wal_replay_pause_state" not in standby_sql
    assert "'replayPauseState', NULL" in standby_sql
    assert "pg_is_wal_replay_paused()" in standby_sql


def test_a_standby_on_14_or_later_reads_the_pause_state(run_script, fake_psql):
    answer(fake_psql, True)
    run_script("replication_status")
    assert "'replayPauseState', pg_get_wal_replay_pause_state()" in fake_psql.calls[-1]["sql"]


@pytest.mark.parametrize("in_recovery", [False, True])
def test_below_13_the_run_fails_with_dependency_before_the_second_statement(
    run_script, fake_psql, in_recovery
):
    fake_psql.on("pg_is_in_recovery()", stdout=json.dumps(role(in_recovery, version=120022)))
    env = run_script("replication_status").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["dependency"]
    assert "PostgreSQL 13 or later required" in env["errors"][0]["message"]
    assert len(fake_psql.calls) == 1


def test_a_missing_version_is_an_internal_error(run_script, fake_psql):
    fake_psql.on("pg_is_in_recovery()", stdout=json.dumps({"inRecovery": False}))
    assert [e["code"] for e in run_script("replication_status").envelope["errors"]] == ["internal"]


# --- the declared permissions ---------------------------------------------------------------
# which functions the task declares is checked against its SQL in test_pg_copies.py


def test_replication_status_declares_pg_read_all_stats_not_pg_monitor():
    permissions = tasks(SKILL)["replication-status"]["permissions"]
    assert {"role": "pg_read_all_stats"} in permissions
    assert {"role": "pg_monitor"} not in permissions
