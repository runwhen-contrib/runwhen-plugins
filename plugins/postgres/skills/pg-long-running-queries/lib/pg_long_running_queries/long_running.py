"""long-running: active queries and idle-in-transaction sessions older than minDuration.

One statement reads pg_stat_activity; every duration is computed on the server (now() minus
query_start, state_change or xact_start), so the argv a recording replays does not depend on the
clock. The task's own backend (pg_backend_pid()) and walsenders (a replication connection is
"active" for as long as it lives) are left out. Query text is returned only when the
includeQueryText setting is on, and only after `sqltext.safe_query_text`."""

from .errors import RwError
from .pg import parse_duration, psql_json
from .sqltext import MAX_QUERY_BYTES, safe_query_text

SCOPE = "pg_stat_activity"
RAW_QUERY_CHARS = 8192  # characters of query text fetched; the rest is never read

# `full_view` and `hidden` as in pg-connections: a role without pg_read_all_stats sees no state
# for other roles' sessions, so they cannot match the filters below.
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
      'blockedBy', pg_blocking_pids(pid){query_field}
    ) ORDER BY dur DESC, pid), '[]'::json)
    FROM (SELECT * FROM hits ORDER BY dur DESC, pid LIMIT {limit}) h)
)"""


def long_running_sql(min_ms, limit, include_query_text):
    """The statement for a threshold in ms and a cap; the query text column only when asked for."""
    return (
        LONG_RUNNING_SQL.replace(
            "{query_column}",
            f",\n         left(a.query, {RAW_QUERY_CHARS}) AS query" if include_query_text else "",
        )
        .replace("{query_field}", ",\n      'query', query" if include_query_text else "")
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
    }
    if include_query_text:
        entry["query"], entry["queryTruncated"] = safe_query_text(item.get("query"))
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
