"""long-running: active queries and idle-in-transaction sessions older than minDuration.

One statement reads pg_stat_activity; every duration is computed on the server (now() minus
query_start, state_change or xact_start), so the argv a recording replays does not depend on the
clock. The task's own backend (pg_backend_pid()) and walsenders (a replication connection is
"active" for as long as it lives) are left out. Query text is returned only when the
includeQueryText setting is on. The text is normalized IN the statement (`sqlnorm`: literals and
comments replaced by `?`, then cut to 1024 characters), so raw query text never leaves the
database; `sqltext.safe_query_text` is a second pass over what comes back.

A session can be blocked by a prepared transaction (`PREPARE TRANSACTION`), which has no backend:
pg_blocking_pids lists it as pid 0. The statement finds it through the granted pg_locks rows that
have no pid (they carry the virtual transaction of the session that prepared it, and an
ExclusiveLock on its transaction id) and lists it in the item's `preparedTransactions`."""

from .errors import RwError
from .pg import parse_duration, psql_json
from .sqlnorm import QUERY_CHARS, RAW_QUERY_CHARS, normalized_query_expr
from .sqltext import MAX_QUERY_BYTES, safe_query_text

SCOPE = "pg_stat_activity"

# `full_view` and `hidden` as in pg-connections: a role without pg_read_all_stats sees no state
# for other roles' sessions, so they cannot match the filters below.
#
# pg_blocking_pids runs once per returned row (not per candidate). `preparedTransactions` names the
# prepared transactions holding a lock on the object a session waits for, only for a session whose
# blockers include pid 0; the conflict of the two lock modes is not checked, so a prepared
# transaction that holds a compatible lock on the same object is listed as well.
LONG_RUNNING_SQL = """WITH cfg AS (
  SELECT (current_setting('is_superuser') = 'on'
           OR pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')) AS full_view
), hidden AS (
  SELECT count(*) AS n
  FROM pg_stat_activity a CROSS JOIN cfg
  WHERE a.pid <> pg_backend_pid()
    AND (a.backend_type = 'client backend'
         OR (a.backend_type IS NULL AND a.datname IS NOT NULL AND a.usename IS NOT NULL))
    AND NOT (cfg.full_view OR coalesce(pg_has_role(current_user, a.usesysid, 'USAGE'), false))
), cand AS (
  SELECT a.pid, a.datname, a.usename, a.application_name, host(a.client_addr) AS client_addr,
         a.state, a.backend_type, a.wait_event_type, a.wait_event,
         to_jsonb(a) ->> 'query_id' AS query_id,
         CASE WHEN a.state = 'active' THEN 'active' ELSE 'idleInTransaction' END AS kind,
         CASE WHEN a.state = 'active' THEN now() - a.query_start
              ELSE now() - a.state_change END AS dur,
         now() - a.xact_start AS xact_age{query_column}
  FROM pg_stat_activity a
  WHERE a.pid <> pg_backend_pid()
    AND a.backend_type <> 'walsender'
    AND a.state IN ('active', 'idle in transaction', 'idle in transaction (aborted)')
), hits AS (
  SELECT * FROM cand WHERE dur > interval '1 millisecond' * {min_ms}
), lk AS MATERIALIZED (
  SELECT pid, locktype, granted, database, relation, page, tuple, virtualxid, transactionid,
         classid, objid, objsubid, virtualtransaction
  FROM pg_locks
), prep AS (
  SELECT p.gid, p.prepared, p.owner::text AS owner, p.database::text AS database,
         t.virtualtransaction AS vxid
  FROM pg_prepared_xacts p
  JOIN lk t ON t.pid IS NULL AND t.granted AND t.locktype = 'transactionid'
           AND t.transactionid = p.transaction
)
SELECT json_build_object(
  'fullView', (SELECT full_view FROM cfg),
  'hiddenSessions', (SELECT n FROM hidden),
  'total', (SELECT count(*) FROM hits),
  'items', (SELECT coalesce(json_agg(json_build_object(
      'pid', pid,
      'database', datname,
      'user', usename,
      'applicationName', application_name,
      'clientAddr', client_addr,
      'state', state,
      'kind', kind,
      'durationMs', (extract(epoch FROM dur) * 1000)::bigint,
      'xactAgeMs', (extract(epoch FROM xact_age) * 1000)::bigint,
      'waitEventType', wait_event_type,
      'waitEvent', wait_event,
      'backendType', backend_type,
      'queryId', query_id,
      'blockedBy', to_json(bp.pids[1:50]),
      'blockedByTruncated', cardinality(bp.pids) > 50,
      'preparedTransactions', CASE WHEN 0 = ANY(bp.pids) THEN coalesce((
          SELECT json_agg(json_build_object(
              'gid', x.gid,
              'prepared', floor(extract(epoch FROM clock_timestamp() - x.prepared) * 1000)::bigint,
              'owner', x.owner,
              'database', x.database
            ) ORDER BY x.prepared, x.gid)
          FROM (SELECT DISTINCT pr.gid, pr.prepared, pr.owner, pr.database
                FROM lk w
                JOIN lk hl ON hl.pid IS NULL AND hl.granted AND hl.locktype = w.locktype
                         AND hl.database IS NOT DISTINCT FROM w.database
                         AND hl.relation IS NOT DISTINCT FROM w.relation
                         AND hl.page IS NOT DISTINCT FROM w.page
                         AND hl.tuple IS NOT DISTINCT FROM w.tuple
                         AND hl.virtualxid IS NOT DISTINCT FROM w.virtualxid
                         AND hl.transactionid IS NOT DISTINCT FROM w.transactionid
                         AND hl.classid IS NOT DISTINCT FROM w.classid
                         AND hl.objid IS NOT DISTINCT FROM w.objid
                         AND hl.objsubid IS NOT DISTINCT FROM w.objsubid
                JOIN prep pr ON pr.vxid = hl.virtualtransaction
                WHERE w.pid = h.pid AND NOT w.granted
                LIMIT 50) x), '[]'::json) ELSE '[]'::json END{query_field}
    ) ORDER BY dur DESC, pid), '[]'::json)
    FROM {items_from} CROSS JOIN LATERAL (SELECT pg_blocking_pids(h.pid) AS pids) bp)
)"""

PLAIN_ITEMS_FROM = "(SELECT * FROM hits ORDER BY dur DESC, pid LIMIT {limit}) h"
# the expression runs on the capped rows only, once per row
NORMALIZED_ITEMS_FROM = (
    "(SELECT t.*, " + normalized_query_expr("t.query") + " AS query_norm\n"
    "      FROM (SELECT * FROM hits ORDER BY dur DESC, pid LIMIT {limit}) t) h"
)
# query_norm is cut here, after the expression. queryTruncated also covers the raw cut at
# RAW_QUERY_CHARS and the server's own cut: pg_stat_activity keeps at most
# track_activity_query_size - 1 bytes (in whole characters, so up to 3 bytes fewer). That setting
# is read from pg_settings, in bytes: current_setting() returns it with a unit ("1kB").
QUERY_FIELD = (
    ",\n      'query', left(query_norm, {chars}),"
    "\n      'queryTruncated', (char_length(query_norm) > {chars} OR char_length(query) >= {raw}"
    "\n                         OR query_cut_by_server)"
).format(chars=QUERY_CHARS, raw=RAW_QUERY_CHARS)
QUERY_COLUMN = (
    ",\n         left(a.query, {raw}) AS query,"
    "\n         (octet_length(a.query) >= (SELECT setting::int FROM pg_settings"
    "\n                                    WHERE name = 'track_activity_query_size') - 4)"
    "\n           AS query_cut_by_server"
).format(raw=RAW_QUERY_CHARS)


def long_running_sql(min_ms, limit, include_query_text):
    """The statement for a threshold in ms and a cap; the query text column only when asked for."""
    return (
        LONG_RUNNING_SQL.replace(
            "{query_column}",
            QUERY_COLUMN if include_query_text else "",
        )
        .replace("{query_field}", QUERY_FIELD if include_query_text else "")
        .replace("{items_from}", NORMALIZED_ITEMS_FROM if include_query_text else PLAIN_ITEMS_FROM)
        .replace("{min_ms}", str(int(min_ms)))
        .replace("{limit}", str(int(limit)))
    )


def min_duration_ms(value):
    """The minDuration input in whole milliseconds (at least 1); a bad value raises invalid_input."""
    return max(1, round(parse_duration(value, "minDuration") * 1000))


def shape_item(item, include_query_text):
    entry = {
        "pid": item["pid"],
        "database": item["database"],
        "user": item["user"],
        "applicationName": item["applicationName"],
        "clientAddr": item["clientAddr"],
        "state": item["state"],
        "kind": item["kind"],
        "durationMs": item["durationMs"],
        "xactAgeMs": item["xactAgeMs"],
        "waitEventType": item["waitEventType"],
        "waitEvent": item["waitEvent"],
        "backendType": item["backendType"],
        "queryId": item["queryId"],
        "blockedBy": item["blockedBy"],
        "blockedByTruncated": item["blockedByTruncated"],
        "preparedTransactions": item["preparedTransactions"],
    }
    if include_query_text:
        # a second pass over text the statement already normalized and cut
        entry["query"], clipped = safe_query_text(item.get("query"))
        entry["queryTruncated"] = bool(item.get("queryTruncated")) or clipped
    return entry


def shape(row, min_ms, max_items, include_query_text):
    """The `longRunning` output from the statement's row."""
    if not isinstance(row, dict):
        raise RwError("internal", "the server returned no session facts")
    full = bool(row["fullView"])
    items = [shape_item(i, include_query_text) for i in row["items"]][:max_items]
    total = row["total"]
    return {
        "visibility": "full" if full else "partial",
        "hiddenSessions": 0 if full else row["hiddenSessions"],
        "minDurationMs": min_ms,
        "queryTextIncluded": include_query_text,
        "items": items,
        "total": total,
        "omitted": max(total - len(items), 0),
        "truncated": total > len(items),
    }


def partial_message(facts):
    return (
        f"the role cannot read other roles' sessions: {facts['hiddenSessions']} session(s) have no "
        "state or query, so items lists only the sessions the role can see and total is a lower "
        "bound; grant pg_read_all_stats (or pg_monitor) to see them"
    )


def collect(ctx, min_duration, max_items, include_query_text):
    """Run the statement; returns the `longRunning` output. A role that cannot see other
    sessions is a permission_denied error scoped to pg_stat_activity and the run is partial."""
    min_ms = min_duration_ms(min_duration)
    sql = long_running_sql(min_ms, max_items, include_query_text)
    facts = shape(psql_json(ctx, sql), min_ms, max_items, include_query_text)
    if facts["visibility"] == "partial":
        ctx.error("permission_denied", partial_message(facts), scope=SCOPE)
    return facts


__all__ = ["MAX_QUERY_BYTES", "collect", "long_running_sql", "shape"]
