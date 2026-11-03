"""replication-status: the server's role, its replicas and slots (primary) or its receiver and
replay position (standby).

Times and byte distances are computed on the server (clock_timestamp(), pg_wal_lsn_diff), so the
argv of a run does not change with the clock. pg_stat_replication and pg_stat_wal_receiver show
a role without pg_read_all_stats (pg_monitor) their rows with every column but the pid and the
role NULL; that is reported as `visibility: partial`, never as a healthy empty view."""

from .errors import RwError

NESTED_LIMIT = 50

MIN_VERSION = 130000  # pg_replication_slots.wal_status and safe_wal_size; below it: dependency
PAUSE_STATE_VERSION = 140000  # pg_get_wal_replay_pause_state()

# The server's version is read once, here, and decides which statement runs next.
ROLE_SQL = """SELECT json_build_object(
  'inRecovery', pg_is_in_recovery(),
  'fullView', pg_has_role(current_user, 'pg_read_all_stats', 'USAGE'),
  'database', current_database(),
  'serverVersionNum', current_setting('server_version_num')::int
)"""

# Only run on a primary: pg_current_wal_lsn() raises during recovery. It is taken ONCE (the
# MATERIALIZED CTE) and every distance and `currentLsn` use that value, so they agree. A lag
# interval is NULL when the standby has been idle and caught up for a while (it is not zero then).
# The views are read once each; the rows that carry text are cut to {limit} in SQL and `total` is
# the count before the cut. `retainedWalBytes` of a lost slot is a distance from restart_lsn, not
# WAL that is still on disk; `safeWalSize` is NULL for a lost slot and without
# max_slot_wal_keep_size.
PRIMARY_SQL = """WITH cur AS MATERIALIZED (
  SELECT pg_current_wal_lsn() AS lsn
), rep AS MATERIALIZED (
  SELECT * FROM pg_stat_replication
), slots AS MATERIALIZED (
  SELECT * FROM pg_replication_slots
)
SELECT json_build_object(
  'currentLsn', (SELECT lsn::text FROM cur),
  'replicas', json_build_object(
    'total', (SELECT count(*) FROM rep),
    'items', coalesce((SELECT json_agg(json_build_object(
      'pid', r.pid,
      'usename', r.usename::text,
      'applicationName', r.application_name,
      'clientAddr', host(r.client_addr),
      'state', r.state,
      'syncState', r.sync_state,
      'writeLagMs', floor(extract(epoch FROM r.write_lag) * 1000)::bigint,
      'flushLagMs', floor(extract(epoch FROM r.flush_lag) * 1000)::bigint,
      'replayLagMs', floor(extract(epoch FROM r.replay_lag) * 1000)::bigint,
      'sentLsnBytes', pg_wal_lsn_diff(cur.lsn, r.sent_lsn)::bigint,
      'writeLsnBytes', pg_wal_lsn_diff(cur.lsn, r.write_lsn)::bigint,
      'flushLsnBytes', pg_wal_lsn_diff(cur.lsn, r.flush_lsn)::bigint,
      'replayLsnBytes', pg_wal_lsn_diff(cur.lsn, r.replay_lsn)::bigint
    ) ORDER BY r.application_name, r.pid)
    FROM (SELECT * FROM rep ORDER BY application_name, pid LIMIT {limit}) r CROSS JOIN cur), '[]'::json)),
  'slots', json_build_object(
    'total', (SELECT count(*) FROM slots),
    'items', coalesce((SELECT json_agg(json_build_object(
      'slotName', s.slot_name::text,
      'slotType', s.slot_type,
      'database', s.database::text,
      'active', s.active,
      'activePid', s.active_pid,
      'walStatus', s.wal_status,
      'retainedWalBytes', pg_wal_lsn_diff(cur.lsn, s.restart_lsn)::bigint,
      'safeWalSize', s.safe_wal_size
    ) ORDER BY s.slot_name)
    FROM (SELECT * FROM slots ORDER BY slot_name LIMIT {limit}) s CROSS JOIN cur), '[]'::json))
)"""

# Only run on a standby. receiveLsn is NULL when no WAL was ever received (archive recovery) but,
# once a receiver has run, it KEEPS its last value after the receiver exits: it says how far the
# standby got, not that it is still connected (see receiver and lastMsgReceiptAgeMs). The LSNs
# are read once. replayDelayMs is NULL until a transaction has been replayed. The pause state
# function exists from PostgreSQL 14; below it replayPauseState is NULL.
STANDBY_SQL = """WITH lsn AS MATERIALIZED (
  SELECT pg_last_wal_receive_lsn() AS recv, pg_last_wal_replay_lsn() AS replay
)
SELECT json_build_object(
  'receiver', (SELECT json_build_object(
      'status', w.status,
      'senderHost', w.sender_host,
      'senderPort', w.sender_port,
      'slotName', w.slot_name::text,
      'lastMsgReceiptAgeMs', floor(extract(epoch FROM clock_timestamp() - w.last_msg_receipt_time) * 1000)::bigint,
      'latestEndLsn', w.latest_end_lsn::text
    ) FROM pg_stat_wal_receiver w LIMIT 1),
  'receiveLsn', (SELECT recv::text FROM lsn),
  'replayLsn', (SELECT replay::text FROM lsn),
  'receivedNotReplayedBytes', (SELECT pg_wal_lsn_diff(recv, replay)::bigint FROM lsn),
  'receiveEqualsReplay', (SELECT recv = replay FROM lsn),
  'replayDelayMs', floor(extract(epoch FROM clock_timestamp() - pg_last_xact_replay_timestamp()) * 1000)::bigint,
  'replayPaused', pg_is_wal_replay_paused(),
  'replayPauseState', {pause_state}
)"""


def primary_sql(limit):
    """The primary's statement, with the cap on replicas and on slots."""
    return PRIMARY_SQL.replace("{limit}", str(int(limit)))


def standby_sql(version):
    """The standby's statement; below PostgreSQL 14 there is no pause state to read."""
    pause = "pg_get_wal_replay_pause_state()" if version >= PAUSE_STATE_VERSION else "NULL"
    return STANDBY_SQL.replace("{pause_state}", pause)


PRIMARY_PARTIAL = (
    "pg_stat_replication shows a role without pg_read_all_stats (grant pg_monitor) its rows with "
    "state, sync_state, the lag intervals and the LSNs hidden: those fields are null"
)
STANDBY_PARTIAL = (
    "pg_stat_wal_receiver shows a role without pg_read_all_stats (grant pg_monitor) a row with "
    "status and sender hidden: those fields are null"
)


def listing(block, limit):
    """`{items, total, omitted, truncated}` from the statement's `{total, items}`: the SQL cut the
    rows to the cap and counted what it found."""
    items, total = block["items"][:limit], block["total"]
    return {
        "items": items,
        "total": total,
        "omitted": max(total - len(items), 0),
        "truncated": total > len(items),
    }


def read_role(raw):
    """The role row; below PostgreSQL 13 the task cannot run, which is a dependency error."""
    if not isinstance(raw, dict) or not isinstance(raw.get("inRecovery"), bool):
        raise RwError("internal", "the server returned no recovery state")
    version = raw.get("serverVersionNum")
    if not isinstance(version, int):
        raise RwError("internal", "the server returned no version number")
    if version < MIN_VERSION:
        raise RwError("dependency", "PostgreSQL 13 or later required")
    return raw


def build_primary(role, raw, max_items):
    """The `replication` output for a primary."""
    if not isinstance(raw, dict) or not isinstance(raw.get("replicas"), dict):
        raise RwError("internal", "the server returned no replication facts")
    return {
        "role": "primary",
        "visibility": "full" if role["fullView"] else "partial",
        "database": role["database"],
        "primary": {
            "currentLsn": raw["currentLsn"],
            "replicas": listing(raw["replicas"], max_items),
            "slots": listing(raw["slots"], max_items),
        },
        "standby": None,
    }


def build_standby(role, raw):
    """The `replication` output for a standby."""
    if not isinstance(raw, dict) or "replayLsn" not in raw:
        raise RwError("internal", "the server returned no recovery facts")
    return {
        "role": "standby",
        "visibility": "full" if role["fullView"] else "partial",
        "database": role["database"],
        "primary": None,
        "standby": {
            k: raw[k]
            for k in (
                "receiver",
                "receiveLsn",
                "replayLsn",
                "receivedNotReplayedBytes",
                "receiveEqualsReplay",
                "replayDelayMs",
                "replayPaused",
                "replayPauseState",
            )
        },
    }
