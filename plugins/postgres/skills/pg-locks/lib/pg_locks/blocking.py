"""blocking: who is waiting for a lock, who holds it, and the root blockers of each chain.

The SQL returns the waiting backends (from pg_locks, with pg_blocking_pids) and the blockers that
are not themselves waiting; `build_blocking` turns that into chain depths and bounded lists. No
query text is read or returned."""

from .errors import RwError

DEPTH_LIMIT = 100  # a longer chain than this is a loop (a deadlock the detector has not broken yet)
NESTED_LIMIT = 50

# One waiting backend is one ungranted pg_locks row (a backend waits for one lock at a time).
# pg_locks and pg_blocking_pids are open to every role; pg_stat_activity hides state and
# xact_start of other roles' sessions from a role without pg_read_all_stats (pg_monitor), which
# shows as NULL. `relation` is resolved only in the connected database (pg_class is per
# database): a lock in another database carries relationOid and lockDatabase instead.
BLOCKING_SQL = """WITH waits AS (
  SELECT DISTINCT ON (l.pid) l.pid, l.locktype, l.mode, l.database, l.relation,
         pg_blocking_pids(l.pid) AS blockers,
         floor(extract(epoch FROM clock_timestamp() - l.waitstart) * 1000)::bigint AS wait_ms
  FROM pg_locks l
  WHERE NOT l.granted AND l.pid IS NOT NULL AND l.pid <> pg_backend_pid()
  ORDER BY l.pid, l.waitstart
), edges AS (
  SELECT * FROM waits WHERE cardinality(blockers) > 0
), involved AS (
  SELECT pid FROM edges UNION SELECT unnest(blockers) FROM edges
)
SELECT json_build_object(
  'fullView', pg_has_role(current_user, 'pg_read_all_stats', 'USAGE'),
  'database', current_database(),
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
                             JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = e.relation) END,
      'blockedBy', to_json(e.blockers)
    ) ORDER BY e.wait_ms DESC NULLS LAST, e.pid)
    FROM edges e LEFT JOIN pg_stat_activity a ON a.pid = e.pid), '[]'::json),
  'blockers', coalesce((SELECT json_agg(json_build_object(
      'pid', i.pid,
      'datname', a.datname::text,
      'usename', a.usename::text,
      'applicationName', a.application_name,
      'state', a.state,
      'xactAgeMs', floor(extract(epoch FROM clock_timestamp() - a.xact_start) * 1000)::bigint
    ) ORDER BY i.pid)
    FROM involved i LEFT JOIN pg_stat_activity a ON a.pid = i.pid
    WHERE i.pid NOT IN (SELECT pid FROM edges)), '[]'::json)
)"""

VISIBILITY_MESSAGE = (
    "pg_stat_activity hides the state and transaction start of other roles' sessions from a role "
    "without pg_read_all_stats (grant pg_monitor): state and xactAgeMs are null where hidden"
)


def listing(items, limit):
    """`{items, total, omitted, truncated}`: the first `limit` items and what was left out."""
    return {
        "items": items[:limit],
        "total": len(items),
        "omitted": max(len(items) - limit, 0),
        "truncated": len(items) > limit,
    }


def depths(edges):
    """{pid: chain depth} for the waiting pids in `edges` ({pid: [blocker pids]}): 1 when every
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
    """{pid: set of root blocker pids its chain reaches}."""
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


def build_blocking(raw, max_items):
    """The `blocking` output from the query's JSON."""
    if not isinstance(raw, dict) or not isinstance(raw.get("waiters"), list):
        raise RwError("internal", "the server returned no lock-wait facts")
    edges = {w["pid"]: list(w["blockedBy"]) for w in raw["waiters"]}
    depth = depths(edges)
    reach = roots_of(edges)
    waiters = []
    for w in raw["waiters"]:
        pid = w["pid"]
        waiters.append(
            {
                "pid": pid,
                "datname": w["datname"],
                "usename": w["usename"],
                "applicationName": w["applicationName"],
                "state": w["state"],
                "waitMs": w["waitMs"],
                "lockType": w["lockType"],
                "lockMode": w["lockMode"],
                "lockDatabase": w["lockDatabase"],
                "relationOid": w["relationOid"],
                "relation": w["relation"],
                "depth": depth[pid],
                "blockedBy": edges[pid][:NESTED_LIMIT],
                "rootBlockerPids": sorted(reach[pid])[:NESTED_LIMIT],
            }
        )
    blocked_by_root = {}
    for pid, found in reach.items():
        for root in found:
            blocked_by_root[root] = blocked_by_root.get(root, 0) + 1
    roots = [
        {
            "pid": b["pid"],
            "datname": b["datname"],
            "usename": b["usename"],
            "applicationName": b["applicationName"],
            "state": b["state"],
            "xactAgeMs": b["xactAgeMs"],
            "blockedWaiters": blocked_by_root.get(b["pid"], 0),
        }
        for b in raw["blockers"]
    ]
    roots.sort(key=lambda r: (-r["blockedWaiters"], -(r["xactAgeMs"] or 0), r["pid"]))
    return {
        "visibility": "full" if raw["fullView"] else "partial",
        "database": raw["database"],
        "maxDepth": max(depth.values(), default=None),
        "waiters": listing(waiters, max_items),
        "rootBlockers": listing(roots, max_items),
    }
