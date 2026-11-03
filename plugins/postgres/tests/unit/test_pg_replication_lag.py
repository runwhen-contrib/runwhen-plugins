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


def role(in_recovery, full=True):
    return {"inRecovery": in_recovery, "fullView": full, "database": "orders"}


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


def primary_raw(replicas=None, slots=None):
    return {
        "currentLsn": "0/3069898",
        "replicas": [replica()] if replicas is None else replicas,
        "slots": [slot()] if slots is None else slots,
    }


STANDBY_RAW = {
    "receiver": {
        "status": "streaming",
        "senderHost": "rw-pg-primary",
        "senderPort": 5432,
        "slotName": "rw_standby",
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


def test_replicas_and_slots_are_cut_at_max_items(run_script, fake_psql):
    raw = primary_raw(
        replicas=[replica(pid=i) for i in range(5)], slots=[slot(f"s{i}") for i in range(4)]
    )
    answer(fake_psql, False, raw=raw)
    prim = run_script("replication_status", settings={"maxItems": 2}).envelope["outputs"][
        "replication"
    ]["primary"]
    assert (
        len(prim["replicas"]["items"]),
        prim["replicas"]["omitted"],
        prim["replicas"]["truncated"],
    ) == (
        2,
        3,
        True,
    )
    assert prim["slots"]["total"] == 4 and prim["slots"]["truncated"] is True


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
        "receiver": {"status": None, "senderHost": None, "senderPort": None, "slotName": None},
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
        assert "default_transaction_read_only = on" in " ".join(call["argv"])
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


# --- the declared permissions ---------------------------------------------------------------

TRACKED = (
    "pg_current_wal_lsn",
    "pg_wal_lsn_diff",
    "pg_last_wal_receive_lsn",
    "pg_last_wal_replay_lsn",
    "pg_last_xact_replay_timestamp",
    "pg_is_wal_replay_paused",
    "pg_get_wal_replay_pause_state",
)


def test_replication_status_declares_the_functions_its_sql_calls_and_pg_monitor():
    declared = {
        p["function"].split(".")[1]
        for p in tasks(SKILL)["replication-status"]["permissions"]
        if "function" in p
    }
    sql = rp.PRIMARY_SQL + rp.STANDBY_SQL
    assert declared == {f for f in TRACKED if f + "(" in sql}
    assert {"role": "pg_monitor"} in tasks(SKILL)["replication-status"]["permissions"]
