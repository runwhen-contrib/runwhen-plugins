"""replication-status: the server's role, its replicas and slots (primary) or its receiver and
replay position (standby).

Times and byte distances are computed on the server (clock_timestamp(), pg_wal_lsn_diff), so the
argv of a run does not change with the clock. pg_stat_replication and pg_stat_wal_receiver show
a role without pg_read_all_stats (pg_monitor) their rows with every column but the pid and the
role NULL; that is reported as `visibility: partial`, never as a healthy empty view."""

from .errors import RwError

NESTED_LIMIT = 50

ROLE_SQL = """SELECT json_build_object(
  'inRecovery', pg_is_in_recovery(),
  'fullView', pg_has_role(current_user, 'pg_read_all_stats', 'USAGE'),
  'database', current_database()
)"""

# Only run on a primary: pg_current_wal_lsn() raises during recovery. A lag interval is NULL
# when the standby has been idle and caught up for a while (it is not zero then).
PRIMARY_SQL = """SELECT json_build_object(
  'currentLsn', pg_current_wal_lsn()::text,
  'replicas', coalesce((SELECT json_agg(json_build_object(
      'pid', r.pid,
      'usename', r.usename::text,
      'applicationName', r.application_name,
      'clientAddr', host(r.client_addr),
      'state', r.state,
      'syncState', r.sync_state,
      'writeLagMs', floor(extract(epoch FROM r.write_lag) * 1000)::bigint,
      'flushLagMs', floor(extract(epoch FROM r.flush_lag) * 1000)::bigint,
      'replayLagMs', floor(extract(epoch FROM r.replay_lag) * 1000)::bigint,
      'sentLsnBytes', pg_wal_lsn_diff(pg_current_wal_lsn(), r.sent_lsn)::bigint,
      'writeLsnBytes', pg_wal_lsn_diff(pg_current_wal_lsn(), r.write_lsn)::bigint,
      'flushLsnBytes', pg_wal_lsn_diff(pg_current_wal_lsn(), r.flush_lsn)::bigint,
      'replayLsnBytes', pg_wal_lsn_diff(pg_current_wal_lsn(), r.replay_lsn)::bigint
    ) ORDER BY r.application_name, r.pid) FROM pg_stat_replication r), '[]'::json),
  'slots', coalesce((SELECT json_agg(json_build_object(
      'slotName', s.slot_name::text,
      'slotType', s.slot_type,
      'database', s.database::text,
      'active', s.active,
      'activePid', s.active_pid,
      'walStatus', s.wal_status,
      'retainedWalBytes', pg_wal_lsn_diff(pg_current_wal_lsn(), s.restart_lsn)::bigint,
      'safeWalSize', s.safe_wal_size
    ) ORDER BY s.slot_name) FROM pg_replication_slots s), '[]'::json)
)"""

# Only run on a standby. receiveLsn is NULL when nothing is being streamed (archive recovery),
# and replayDelayMs is NULL until a transaction has been replayed.
STANDBY_SQL = """SELECT json_build_object(
  'receiver', (SELECT json_build_object(
      'status', w.status,
      'senderHost', w.sender_host,
      'senderPort', w.sender_port,
      'slotName', w.slot_name::text
    ) FROM pg_stat_wal_receiver w LIMIT 1),
  'receiveLsn', pg_last_wal_receive_lsn()::text,
  'replayLsn', pg_last_wal_replay_lsn()::text,
  'receivedNotReplayedBytes', pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint,
  'receiveEqualsReplay', pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn(),
  'replayDelayMs', floor(extract(epoch FROM clock_timestamp() - pg_last_xact_replay_timestamp()) * 1000)::bigint,
  'replayPaused', pg_is_wal_replay_paused(),
  'replayPauseState', pg_get_wal_replay_pause_state()
)"""

PRIMARY_PARTIAL = (
    "pg_stat_replication shows a role without pg_read_all_stats (grant pg_monitor) its rows with "
    "state, sync_state, the lag intervals and the LSNs hidden: those fields are null"
)
STANDBY_PARTIAL = (
    "pg_stat_wal_receiver shows a role without pg_read_all_stats (grant pg_monitor) a row with "
    "status and sender hidden: those fields are null"
)


def listing(items, limit):
    """`{items, total, omitted, truncated}`: the first `limit` items and what was left out."""
    return {
        "items": items[:limit],
        "total": len(items),
        "omitted": max(len(items) - limit, 0),
        "truncated": len(items) > limit,
    }


def read_role(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("inRecovery"), bool):
        raise RwError("internal", "the server returned no recovery state")
    return raw


def build_primary(role, raw, max_items):
    """The `replication` output for a primary."""
    if not isinstance(raw, dict) or not isinstance(raw.get("replicas"), list):
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
