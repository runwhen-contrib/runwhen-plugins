"""lock-summary: pg_locks counted by mode and granted, the longest wait, and the lock table's fill."""

from .blocking import WAITSTART_VERSION, check_version, listing
from .errors import RwError

# The task's own backend is left out. The lock table (the shared hash that max_locks_per_transaction
# sizes) holds one entry per locked object that is not a fast-path lock, so the used count is the
# number of distinct lock tags among the non-fast-path rows; its documented size is
# max_locks_per_transaction * (max_connections + max_prepared_transactions), an estimate.
# pg_locks.waitstart exists from PostgreSQL 14: it is read through to_jsonb, so the statement runs
# on 13 and the longest wait is NULL there.
SUMMARY_SQL = """WITH l AS MATERIALIZED (
  SELECT pid, locktype, database, relation, page, tuple, virtualxid, transactionid, classid,
         objid, objsubid, mode, granted, fastpath,
         (to_jsonb(pg_locks) ->> 'waitstart')::timestamptz AS waitstart
  FROM pg_locks
  WHERE pid IS DISTINCT FROM pg_backend_pid()
), s AS (
  SELECT current_setting('server_version_num')::int AS version,
         current_setting('max_locks_per_transaction')::int AS per_xact,
         current_setting('max_connections')::int AS conns,
         current_setting('max_prepared_transactions')::int AS prepared
)
SELECT json_build_object(
  'serverVersionNum', (SELECT version FROM s),
  'total', (SELECT count(*) FROM l),
  'granted', (SELECT count(*) FROM l WHERE granted),
  'waiting', (SELECT count(*) FROM l WHERE NOT granted),
  'waitingBackends', (SELECT count(DISTINCT pid) FROM l WHERE NOT granted),
  'longestWaitMs', (SELECT floor(extract(epoch FROM clock_timestamp() - min(waitstart)) * 1000)::bigint
                    FROM l WHERE NOT granted),
  'byMode', coalesce((SELECT json_agg(json_build_object('mode', t.mode, 'granted', t.granted, 'count', t.n)
                                      ORDER BY t.mode, t.granted DESC)
                      FROM (SELECT mode, granted, count(*) AS n FROM l GROUP BY mode, granted) t), '[]'::json),
  'maxLocksPerTransaction', (SELECT per_xact FROM s),
  'maxConnections', (SELECT conns FROM s),
  'maxPreparedTransactions', (SELECT prepared FROM s),
  'capacity', (SELECT per_xact::bigint * (conns + prepared) FROM s),
  'tableEntries', (SELECT count(*) FROM (SELECT DISTINCT locktype, database, relation, page, tuple,
                     virtualxid, transactionid, classid, objid, objsubid FROM l WHERE NOT fastpath) d),
  'fastPathLocks', (SELECT count(*) FROM l WHERE fastpath)
)"""


def build_summary(raw, max_items):
    """The `locks` and `lockTable` outputs from the query's JSON."""
    if not isinstance(raw, dict) or not isinstance(raw.get("byMode"), list):
        raise RwError("internal", "the server returned no lock counts")
    wait_times = check_version(raw) >= WAITSTART_VERSION
    modes = [
        {"mode": m["mode"], "granted": m["granted"], "count": m["count"]} for m in raw["byMode"]
    ]
    locks = {
        "total": raw["total"],
        "granted": raw["granted"],
        "waiting": raw["waiting"],
        "waitingBackends": raw["waitingBackends"],
        "longestWaitMs": raw["longestWaitMs"] if wait_times else None,
        "waitTimesUnavailable": not wait_times,
        "byMode": listing(modes, max_items),
    }
    capacity = raw["capacity"]
    fill = round(raw["tableEntries"] / capacity, 4) if capacity else None
    table = {
        "maxLocksPerTransaction": raw["maxLocksPerTransaction"],
        "maxConnections": raw["maxConnections"],
        "maxPreparedTransactions": raw["maxPreparedTransactions"],
        "capacity": raw["capacity"],
        "tableEntries": raw["tableEntries"],
        "fastPathLocks": raw["fastPathLocks"],
        "estimatedFillRatio": fill,
    }
    return {"locks": locks, "lockTable": table}
