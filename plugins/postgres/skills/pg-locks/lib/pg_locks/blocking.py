"""blocking: who is waiting for a lock, who holds it, and the root blockers of each chain.

The SQL returns the waiting backends (from pg_locks, with pg_blocking_pids) and the blockers that
are not themselves waiting; `build_blocking` turns that into chain depths and bounded lists. No
query text is read or returned.

A blocker that is a prepared transaction (`PREPARE TRANSACTION`) has no backend: pg_blocking_pids
lists it as pid 0. The SQL finds the prepared transaction through the granted pg_locks rows that
have no pid (they carry the virtual transaction of the session that prepared it, and an
ExclusiveLock on its transaction id) and reports it as a root blocker with `preparedTransaction`."""

from .errors import RwError

DEPTH_LIMIT = 100  # a longer chain than this is a loop (a deadlock the detector has not broken yet)
NESTED_LIMIT = 50
MIN_VERSION = (
    130000  # pg_stat_activity.backend_type, pg_replication_slots.wal_status... below it: dependency
)
WAITSTART_VERSION = 140000  # pg_locks.waitstart

# One waiting backend is one ungranted pg_locks row (a backend waits for one lock at a time).
# pg_locks, pg_blocking_pids and pg_prepared_xacts are open to every role; pg_stat_activity hides
# state and xact_start of other roles' sessions from a role without pg_read_all_stats (pg_monitor),
# which shows as NULL. `relation` is resolved only in the connected database (pg_class is per
# database): a lock in another database carries relationOid and lockDatabase instead.
#
# The whole lock table is read once (`lk`). pg_locks.waitstart exists from PostgreSQL 14: it is
# read through to_jsonb, so the statement runs on 13 and gives NULL there. `edges` is every waiting
# backend with every blocker as numbers only: depth and the roots a chain reaches are computed from
# all of them. What carries text (names, relations) is cut to {limit} rows, the longest waits first
# for waiters; when there are more root blockers than that, the ones with most waiters directly
# behind them are kept (the ranking of what is returned is by every waiter behind a root).
#
# A prepared transaction is matched to a waiter through a granted pg_locks row with no pid on the
# same lock tag as the lock the waiter wants; the conflict of the two modes is not checked, so a
# prepared transaction that holds a compatible lock on the same object is listed as well.
BLOCKING_SQL = """WITH cfg AS (
  SELECT current_setting('server_version_num')::int AS version,
         pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS full_view
), lk AS MATERIALIZED (
  SELECT l.pid, l.locktype, l.mode, l.granted, l.database, l.relation, l.page, l.tuple,
         l.virtualxid, l.transactionid, l.classid, l.objid, l.objsubid, l.virtualtransaction,
         (to_jsonb(l) ->> 'waitstart')::timestamptz AS waitstart
  FROM pg_locks l
), waits AS (
  SELECT DISTINCT ON (l.pid) l.pid, l.locktype, l.mode, l.database, l.relation, l.page, l.tuple,
         l.virtualxid, l.transactionid, l.classid, l.objid, l.objsubid,
         pg_blocking_pids(l.pid) AS blockers,
         floor(extract(epoch FROM clock_timestamp() - l.waitstart) * 1000)::bigint AS wait_ms
  FROM lk l
  WHERE NOT l.granted AND l.pid IS NOT NULL AND l.pid <> pg_backend_pid()
  ORDER BY l.pid, l.waitstart
), edges AS (
  SELECT * FROM waits WHERE cardinality(blockers) > 0
), prep AS (
  SELECT p.gid, p.prepared, p.owner::text AS owner, p.database::text AS database,
         t.virtualtransaction AS vxid
  FROM pg_prepared_xacts p
  JOIN lk t ON t.pid IS NULL AND t.granted AND t.locktype = 'transactionid'
           AND t.transactionid = p.transaction
), prepared_waits AS (
  SELECT DISTINCT e.pid, pr.gid
  FROM edges e
  JOIN lk h ON h.pid IS NULL AND h.granted AND h.locktype = e.locktype
           AND h.database IS NOT DISTINCT FROM e.database
           AND h.relation IS NOT DISTINCT FROM e.relation
           AND h.page IS NOT DISTINCT FROM e.page
           AND h.tuple IS NOT DISTINCT FROM e.tuple
           AND h.virtualxid IS NOT DISTINCT FROM e.virtualxid
           AND h.transactionid IS NOT DISTINCT FROM e.transactionid
           AND h.classid IS NOT DISTINCT FROM e.classid
           AND h.objid IS NOT DISTINCT FROM e.objid
           AND h.objsubid IS NOT DISTINCT FROM e.objsubid
  JOIN prep pr ON pr.vxid = h.virtualtransaction
  WHERE 0 = ANY(e.blockers)
), roots AS (
  SELECT b.pid, count(*) AS direct
  FROM edges e CROSS JOIN LATERAL unnest(e.blockers) AS b(pid)
  WHERE b.pid <> 0 AND b.pid NOT IN (SELECT pid FROM edges)
  GROUP BY b.pid
)
SELECT json_build_object(
  'fullView', (SELECT full_view FROM cfg),
  'serverVersionNum', (SELECT version FROM cfg),
  'database', current_database(),
  'edges', coalesce((SELECT json_agg(json_build_object(
      'pid', e.pid,
      'blockedBy', to_json(e.blockers),
      'prepared', coalesce((SELECT json_agg(w.gid ORDER BY w.gid) FROM prepared_waits w WHERE w.pid = e.pid),
                           '[]'::json)
    ) ORDER BY e.pid) FROM edges e), '[]'::json),
  'waiters', coalesce((SELECT json_agg(json_build_object(
      'pid', e.pid,
      'datname', a.datname::text,
      'usename', a.usename::text,
      'applicationName', a.application_name,
      'state', a.state,
      'waitMs', e.wait_ms,
      'lockType', e.locktype,
      'lockMode', e.mode,
      'lockDatabase', (SELECT d.datname FROM pg_database d WHERE d.oid = e.database)::text,
      'relationOid', e.relation::bigint,
      'relation', CASE WHEN e.database = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database())
                       THEN (SELECT n.nspname || '.' || c.relname FROM pg_class c
                             JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = e.relation) END
    ) ORDER BY e.wait_ms DESC NULLS LAST, e.pid)
    FROM (SELECT * FROM edges ORDER BY wait_ms DESC NULLS LAST, pid LIMIT {limit}) e
    LEFT JOIN pg_stat_activity a ON a.pid = e.pid), '[]'::json),
  'blockers', coalesce((SELECT json_agg(json_build_object(
      'pid', r.pid,
      'datname', r.datname::text,
      'usename', r.usename::text,
      'applicationName', r.application_name,
      'state', r.state,
      'xactAgeMs', floor(extract(epoch FROM clock_timestamp() - r.xact_start) * 1000)::bigint
    ) ORDER BY r.direct DESC, r.xact_start NULLS LAST, r.pid)
    FROM (SELECT x.pid, x.direct, a.datname, a.usename, a.application_name, a.state, a.xact_start
          FROM roots x LEFT JOIN pg_stat_activity a ON a.pid = x.pid
          ORDER BY x.direct DESC, a.xact_start NULLS LAST, x.pid LIMIT {limit}) r), '[]'::json),
  'preparedTransactions', coalesce((SELECT json_agg(json_build_object(
      'gid', pr.gid,
      'prepared', floor(extract(epoch FROM clock_timestamp() - pr.prepared) * 1000)::bigint,
      'owner', pr.owner,
      'database', pr.database
    ) ORDER BY pr.prepared, pr.gid)
    FROM (SELECT * FROM prep WHERE gid IN (SELECT gid FROM prepared_waits)
          ORDER BY prepared, gid LIMIT {limit}) pr), '[]'::json)
)"""

VISIBILITY_MESSAGE = (
    "pg_stat_activity hides the state and transaction start of other roles' sessions from a role "
    "without pg_read_all_stats (grant pg_monitor): state and xactAgeMs are null where hidden"
)


def blocking_sql(limit):
    """The statement, with the cap on the rows that carry text."""
    return BLOCKING_SQL.replace("{limit}", str(int(limit)))


def listing(items, limit, total=None):
    """`{items, total, omitted, truncated}`: the first `limit` items and what was left out."""
    total = len(items) if total is None else total
    kept = items[:limit]
    return {
        "items": kept,
        "total": total,
        "omitted": max(total - len(kept), 0),
        "truncated": total > len(kept),
    }


def check_version(raw):
    """The server's version number; below 13 the task cannot run, which is a dependency error."""
    version = raw.get("serverVersionNum")
    if not isinstance(version, int):
        raise RwError("internal", "the server returned no version number")
    if version < MIN_VERSION:
        raise RwError("dependency", "PostgreSQL 13 or later required")
    return version


def depths(edges):
    """{pid: chain depth} for the waiting pids in `edges` ({pid: [blockers]}): 1 when every
    blocker is a root blocker (not waiting), else 1 + the deepest waiting blocker. A loop is cut
    at the pid that closes it."""
    memo = {}

    def depth(pid, path):
        if pid not in edges:
            return 0
        if pid in memo:
            return memo[pid]
        if pid in path or len(path) > DEPTH_LIMIT:
            return 0
        result = 1 + max(depth(b, path | {pid}) for b in edges[pid])
        memo[pid] = result
        return result

    return {pid: depth(pid, frozenset()) for pid in edges}


def roots_of(edges):
    """{pid: set of root blockers its chain reaches}."""
    memo = {}

    def roots(pid, path):
        if pid not in edges:
            return {pid}
        if pid in memo:
            return memo[pid]
        if pid in path:
            return set()
        found = set()
        for blocker in edges[pid]:
            found |= roots(blocker, path | {pid})
        memo[pid] = found
        return found

    return {pid: roots(pid, frozenset()) for pid in edges}


def prepared_node(gid):
    """A prepared transaction as a node of the wait graph: a tuple, so it never equals a pid."""
    return ("prepared", gid)


def prepared_entry(row):
    return {
        "gid": row["gid"],
        "prepared": row["prepared"],
        "owner": row["owner"],
        "database": row["database"],
    }


def build_blocking(raw, max_items):
    """The `blocking` output from the query's JSON."""
    if not isinstance(raw, dict) or not isinstance(raw.get("edges"), list):
        raise RwError("internal", "the server returned no lock-wait facts")
    version = check_version(raw)
    wait_times = version >= WAITSTART_VERSION
    prepared = {p["gid"]: p for p in raw["preparedTransactions"]}
    # the wait graph over every waiting backend; pid 0 stands for the prepared transactions the
    # SQL attributed to that waiter (one node each), or for an unidentified one
    edges, own = {}, {}
    for e in raw["edges"]:
        own[e["pid"]] = e
        nodes = []
        for blocker in e["blockedBy"]:
            if blocker == 0:
                nodes += [prepared_node(g) for g in e["prepared"]] or [prepared_node(None)]
            else:
                nodes.append(blocker)
        edges[e["pid"]] = nodes
    depth = depths(edges)
    reach = roots_of(edges)

    def pid_of(node):
        return 0 if isinstance(node, tuple) else node

    waiters = []
    for w in raw["waiters"]:
        pid = w["pid"]
        blocked_by = own[pid]["blockedBy"]
        root_pids = sorted({pid_of(n) for n in reach[pid]})
        waiters.append(
            {
                "pid": pid,
                "datname": w["datname"],
                "usename": w["usename"],
                "applicationName": w["applicationName"],
                "state": w["state"],
                "waitMs": w["waitMs"] if wait_times else None,
                "lockType": w["lockType"],
                "lockMode": w["lockMode"],
                "lockDatabase": w["lockDatabase"],
                "relationOid": w["relationOid"],
                "relation": w["relation"],
                "depth": depth[pid],
                "blockedBy": blocked_by[:NESTED_LIMIT],
                "blockedByTruncated": len(blocked_by) > NESTED_LIMIT,
                "preparedTransactions": [
                    prepared_entry(prepared[g]) for g in own[pid]["prepared"] if g in prepared
                ][:NESTED_LIMIT],
                "rootBlockerPids": root_pids[:NESTED_LIMIT],
                "rootBlockerPidsTruncated": len(root_pids) > NESTED_LIMIT,
            }
        )
    blocked_by_root = {}
    for found in reach.values():
        for root in found:
            blocked_by_root[root] = blocked_by_root.get(root, 0) + 1
    sessions = {b["pid"]: b for b in raw["blockers"]}
    roots = []
    for node, count in blocked_by_root.items():
        if isinstance(node, tuple):
            gid = node[1]
            row = prepared.get(gid) if gid is not None else None
            roots.append(
                {
                    "pid": 0,
                    "datname": row["database"] if row else None,
                    "usename": row["owner"] if row else None,
                    "applicationName": None,
                    "state": None,
                    "xactAgeMs": None,
                    "blockedWaiters": count,
                    "preparedTransaction": prepared_entry(row)
                    if row
                    else {"gid": gid, "prepared": None, "owner": None, "database": None},
                }
            )
        elif node in sessions:
            b = sessions[node]
            roots.append(
                {
                    "pid": b["pid"],
                    "datname": b["datname"],
                    "usename": b["usename"],
                    "applicationName": b["applicationName"],
                    "state": b["state"],
                    "xactAgeMs": b["xactAgeMs"],
                    "blockedWaiters": count,
                    "preparedTransaction": None,
                }
            )
    roots.sort(key=lambda r: (-r["blockedWaiters"], -(r["xactAgeMs"] or 0), r["pid"]))
    return {
        "visibility": "full" if raw["fullView"] else "partial",
        "database": raw["database"],
        "waitTimesUnavailable": not wait_times,
        "maxDepth": max(depth.values(), default=None),
        "waiters": listing(waiters, max_items, total=len(edges)),
        "rootBlockers": listing(roots, max_items, total=len(blocked_by_root)),
    }
