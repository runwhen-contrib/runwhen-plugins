---
name: pg-connections
description: >-
  Connection capacity of a PostgreSQL instance: max_connections, the reserved slots and
  the connections in use as a percentage of what an ordinary role may use, sessions
  counted by state (active, idle, idle in transaction), backend type, database and
  user with application_name, the age of the oldest idle-in-transaction session and
  open transaction, and backends waiting on a lock. Use when clients get "too many
  clients already" or "remaining connection slots are reserved", when connections
  climb, when a pool looks leaked, or before looking for long queries.
runwhen:
  role: health
  appliesTo:
    types: [postgres/instance]
  tags: [health, connections, sessions, capacity]
---

## When to use

An application cannot connect (`FATAL: sorry, too many clients already`, `remaining
connection slots are reserved for roles with the SUPERUSER attribute`), connection counts
are climbing, a pool may be leaking, or latency came with many waiting sessions. Run
`connections` first: it says how close the instance is to its limit and which database,
role and application holds the slots. For the queries behind it go to
`postgres:pg-long-running-queries/long-running`.

The task only reads `pg_stat_activity` and the connection settings, in a read-only session,
with the connection's own libpq environment. It connects to the connection's database
(PGDATABASE); `pg_stat_activity` is the same whichever database that is.

## What each task tells you

`connections` returns one `connections` output:

- `maxConnections`, `superuserReservedConnections`, `reservedConnections` (`null` before
  PostgreSQL 16) and `usableConnections`, the three subtracted: the slots an ordinary role
  can use. `inUse` is the number of client connections and `usedPercent` is `inUse` as a
  percentage of `usableConnections`. The task's own connection is not counted, so the
  figure is one slot under what the server holds while the task runs. Walsenders,
  autovacuum workers and other background processes do not use these slots and are not in
  `inUse`.
- `byState`: client sessions that are `active`, `idle`, `idleInTransaction`,
  `idleInTransactionAborted`, `fastpath`, `disabled` or `nullState`.
- `byBackendType` (every backend), `byDatabase` and `byUserApplication` (role x
  application_name), largest first, each `{items, total, omitted, truncated}` cut to `maxItems`
  in the query; `truncated` is true when groups were left out.
- `oldestIdleInTransactionMs`: how long the longest idle-in-transaction session (aborted
  included) has been idle; `oldestTransactionMs`: the age of the oldest open transaction.
  Both are `null` when there is none. `waitingOnLock`: sessions waiting on a lock.

## How to read the result

- `visibility: partial` with a `permission_denied` error scoped to `pg_stat_activity` and
  a `partial` run: the role is not a superuser and not a member of `pg_read_all_stats` (or
  `pg_monitor`), so other roles' sessions have no state, wait event, times or backend type.
  Only a row whose `backend_type` is visible as `client backend` counts as a connection, so
  `inUse`, `usedPercent`, `byState`, `byDatabase` and `byUserApplication` describe the sessions the
  role can see and are **lower bounds**: the real figures are higher. `hiddenSessions` counts the
  rows of other roles that name a role and a database (an estimate: it can include a logical
  replication worker); they are not in `inUse`, so `inUse + hiddenSessions` is the most that may
  be in use. `oldestIdleInTransactionMs`, `oldestTransactionMs` and `waitingOnLock` are `null`:
  they would only describe the sessions the role can see. `byBackendType` counts every row, so
  the rows whose type the role cannot read (other roles' sessions and the background processes)
  show as one group with `backendType: null`; that group is not a backend type, it is the
  hidden rows. Do not read `byState` as the real mix then.
- A pooler (pgbouncer) shows as few sessions of its own role; the application behind it is
  not visible here.
- Normal states that look alarming: a few `idle` sessions from a pool; `idleInTransaction`
  for a moment while a client works between statements. Look at `oldestIdleInTransactionMs`
  (seconds is normal, minutes or hours holds locks and blocks vacuum).
- `usedPercent` near 100 means new ordinary connections are refused; superusers still
  connect through `superuserReservedConnections`. A server with `usedPercent` low and a
  failing client is usually pointing at a role or database limit (`ALTER ROLE ... CONNECTION
  LIMIT`), which this task does not read.
- Failure is an error, not an empty result: `permission_denied` (authentication,
  `pg_hba.conf`, missing role), `not_found` (the connection's database does not exist),
  `unreachable`, `timeout`, `dependency` (no psql).
- The task reads `backend_type`, so it needs PostgreSQL 10 or later; `reservedConnections` is
  null before 16.

## What to try next

- Sessions idle in transaction or waiting on a lock: `postgres:pg-long-running-queries/long-running`
  lists them with the pids that block them.
- Many sessions from one application: that application's pool size, or
  `kubernetes:k8s-workload-health/rollout-status` for a Deployment that just scaled out.
- `permission_denied` scoped to `pg_stat_activity`: `GRANT pg_monitor TO <role>`, then
  `postgres:pg-base/check-permissions` to confirm.
