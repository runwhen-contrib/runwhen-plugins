"""connections: how many connections the server allows, how many are in use, and who holds them.

One statement reads pg_stat_activity and the settings; every age is computed on the server
(now() - state_change), so the argv a recording replays does not depend on the clock. The
task's own backend (pg_backend_pid()) is left out of every count."""

from .errors import RwError
from .pg import psql_json

SCOPE = "pg_stat_activity"
IDLE_IN_TRANSACTION = ("idle in transaction", "idle in transaction (aborted)")

# A role without pg_read_all_stats (and not a superuser) sees every row of pg_stat_activity but the
# rows of other roles have NULL state, query, wait_event and times, and NULL backend_type, so what
# they are cannot be known. Only a row whose backend_type is visible as 'client backend' is counted
# as a client connection (inUse, byState, byDatabase, byUserApplication); a hidden row that names
# a role and a database is counted apart, as hiddenSessions, by the same rule the server applies to
# hide it. hiddenSessions can include a logical-replication worker or walsender: it is the only
# estimate in a partial result, and it is never added to inUse. byBackendType counts every row, so
# the hidden rows are its NULL backendType.
CONNECTIONS_SQL = """WITH cfg AS (
  SELECT current_setting('max_connections')::int AS max_connections,
         current_setting('superuser_reserved_connections')::int AS superuser_reserved,
         nullif(current_setting('reserved_connections', true), '')::int AS reserved,
         (current_setting('is_superuser') = 'on'
           OR pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')) AS full_view
), s AS (
  SELECT a.datname, a.usename, a.application_name, a.state, a.backend_type, a.wait_event_type,
         a.xact_start, a.state_change,
         (a.backend_type = 'client backend') AS is_client,
         (a.backend_type IS NULL AND a.datname IS NOT NULL AND a.usename IS NOT NULL
           AND NOT (cfg.full_view OR coalesce(pg_has_role(current_user, a.usesysid, 'USAGE'), false))) AS is_hidden
  FROM pg_stat_activity a CROSS JOIN cfg
  WHERE a.pid <> pg_backend_pid()
), by_type AS (
  SELECT backend_type, count(*) AS n FROM s GROUP BY backend_type
), by_db AS (
  SELECT datname, count(*) AS n FROM s WHERE is_client GROUP BY datname
), by_user AS (
  SELECT usename, application_name, count(*) AS n FROM s WHERE is_client GROUP BY usename, application_name
)
SELECT json_build_object(
  'fullView', (SELECT full_view FROM cfg),
  'maxConnections', (SELECT max_connections FROM cfg),
  'superuserReservedConnections', (SELECT superuser_reserved FROM cfg),
  'reservedConnections', (SELECT reserved FROM cfg),
  'inUse', (SELECT count(*) FROM s WHERE is_client),
  'hiddenSessions', (SELECT count(*) FROM s WHERE is_hidden),
  'byState', (SELECT json_build_object(
      'active', count(*) FILTER (WHERE state = 'active'),
      'idle', count(*) FILTER (WHERE state = 'idle'),
      'idleInTransaction', count(*) FILTER (WHERE state = 'idle in transaction'),
      'idleInTransactionAborted', count(*) FILTER (WHERE state = 'idle in transaction (aborted)'),
      'fastpath', count(*) FILTER (WHERE state = 'fastpath function call'),
      'disabled', count(*) FILTER (WHERE state = 'disabled'),
      'nullState', count(*) FILTER (WHERE state IS NULL))
    FROM s WHERE is_client),
  'byBackendType', json_build_object(
    'total', (SELECT count(*) FROM by_type),
    'items', (SELECT coalesce(json_agg(json_build_object('backendType', backend_type, 'count', n)
                                       ORDER BY n DESC, backend_type), '[]'::json)
              FROM (SELECT * FROM by_type ORDER BY n DESC, backend_type LIMIT {limit}) t)),
  'byDatabase', json_build_object(
    'total', (SELECT count(*) FROM by_db),
    'items', (SELECT coalesce(json_agg(json_build_object('database', datname, 'count', n)
                                       ORDER BY n DESC, datname), '[]'::json)
              FROM (SELECT * FROM by_db ORDER BY n DESC, datname LIMIT {limit}) t)),
  'byUserApplication', json_build_object(
    'total', (SELECT count(*) FROM by_user),
    'items', (SELECT coalesce(json_agg(json_build_object('user', usename, 'applicationName', application_name, 'count', n)
                                       ORDER BY n DESC, usename, application_name), '[]'::json)
              FROM (SELECT * FROM by_user ORDER BY n DESC, usename, application_name LIMIT {limit}) t)),
  'oldestIdleInTransactionMs', (SELECT (extract(epoch FROM max(now() - state_change)) * 1000)::bigint
      FROM s WHERE is_client AND state IN ('idle in transaction', 'idle in transaction (aborted)')),
  'oldestTransactionMs', (SELECT (extract(epoch FROM max(now() - xact_start)) * 1000)::bigint
      FROM s WHERE is_client AND xact_start IS NOT NULL),
  'waitingOnLock', (SELECT count(*) FROM s WHERE is_client AND wait_event_type = 'Lock')
)"""


def connections_sql(limit):
    return CONNECTIONS_SQL.replace("{limit}", str(int(limit)))


def listing(block):
    """A grouped count as {items, total, omitted, truncated}: the SQL cut it to the cap."""
    items, total = block["items"], block["total"]
    omitted = max(total - len(items), 0)
    return {"items": items, "total": total, "omitted": omitted, "truncated": omitted > 0}


def used_percent(in_use, usable):
    return round(100.0 * in_use / usable, 1) if usable > 0 else None


def shape(row):
    """The `connections` output from the statement's row. Under partial visibility inUse and
    usedPercent count the client sessions the role can see only (a lower bound; hiddenSessions
    says how many more there may be), and the figures that read other sessions' state (the oldest
    transaction ages, the lock waiters) are null: the visible sessions alone would understate
    them."""
    if not isinstance(row, dict):
        raise RwError("internal", "the server returned no connection facts")
    full = bool(row["fullView"])
    reserved = row["reservedConnections"]
    usable = row["maxConnections"] - row["superuserReservedConnections"] - (reserved or 0)
    return {
        "visibility": "full" if full else "partial",
        "hiddenSessions": 0 if full else row["hiddenSessions"],
        "maxConnections": row["maxConnections"],
        "superuserReservedConnections": row["superuserReservedConnections"],
        "reservedConnections": reserved,
        "usableConnections": usable,
        "inUse": row["inUse"],
        "usedPercent": used_percent(row["inUse"], usable),
        "byState": row["byState"],
        "byBackendType": listing(row["byBackendType"]),
        "byDatabase": listing(row["byDatabase"]),
        "byUserApplication": listing(row["byUserApplication"]),
        "oldestIdleInTransactionMs": row["oldestIdleInTransactionMs"] if full else None,
        "oldestTransactionMs": row["oldestTransactionMs"] if full else None,
        "waitingOnLock": row["waitingOnLock"] if full else None,
    }


def partial_message(facts):
    return (
        f"the role cannot read other roles' sessions: {facts['hiddenSessions']} session(s) have no "
        "state, query or backend type and are not counted in inUse, so inUse and usedPercent are "
        "lower bounds and oldestIdleInTransactionMs, oldestTransactionMs and waitingOnLock are "
        "null; grant pg_read_all_stats (or pg_monitor) to see them"
    )


def collect(ctx, max_items):
    """Run the statement; returns the `connections` output. A role that cannot see other
    sessions is a permission_denied error scoped to pg_stat_activity and the run is partial."""
    facts = shape(psql_json(ctx, connections_sql(max_items)))
    if facts["visibility"] == "partial":
        ctx.error("permission_denied", partial_message(facts), scope=SCOPE)
    return facts
