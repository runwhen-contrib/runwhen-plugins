---
name: pg-long-running-queries
description: >-
  Long-running work on a PostgreSQL instance: active queries running longer than
  minDuration and sessions idle in transaction longer than minDuration, longest first,
  with pid, database, role, application_name, client address, how long, the transaction
  age, wait event, the pids that block each one, and the query text with literals
  replaced when includeQueryText is on. Use when queries hang or pile up, a lock wait
  or a stuck migration is suspected, vacuum cannot clean up, or connections are held
  by sessions that do nothing.
runwhen:
  role: health
  appliesTo:
    types: [postgres/instance]
  tags: [health, queries, sessions, locks]
---

## When to use

A query has run for minutes, requests queue behind a lock, `pg_stat_activity` shows many
`idle in transaction` sessions, or `postgres:pg-connections/connections` reports a high
`oldestIdleInTransactionMs`. `long-running` names the sessions and, through `blockedBy`,
which pid holds up which.

The task only reads `pg_stat_activity` and `pg_blocking_pids`, in a read-only session, with the
connection's own libpq environment. It never cancels or terminates a session.

## What each task tells you

`long-running` takes `minDuration` (default `5m`) and returns one `longRunning` output:

- `items`, longest first, cut to `maxItems`; `total` is the count before the cut, `omitted` the
  number left out and `truncated` is true when any were. Each item has `pid`, `database`, `user`,
  `applicationName`, `clientAddr`, `state`, `waitEventType`/`waitEvent`, `backendType`, `queryId`
  and `blockedBy` (the pids it waits for).
- `kind: active` is a query running now: `durationMs` is the time since it started.
  `kind: idleInTransaction` is a session with an open transaction that is waiting for its client
  (`idle in transaction (aborted)` included): `durationMs` is the time since it went idle.
  `xactAgeMs` is the age of the transaction in both cases and is what holds back vacuum.
- `queryId` is set only when `compute_query_id` is on (or `auto` with a module such as
  pg_stat_statements) on PostgreSQL 14 or later; it matches the id in pg_stat_statements.
- `query` and `queryTruncated` appear only when the `includeQueryText` setting is on. The text is
  normalized: string and numeric literals become `?`, comments are dropped, whitespace is
  collapsed, and it is cut to 1024 bytes. It is never the raw statement. A literal that the
  server's own cut (`track_activity_query_size`) left open is replaced to the end of the text.

## How to read the result

- `items: []` with `total: 0` and `visibility: full` means nothing has run or idled longer than
  `minDuration`; it says nothing about shorter work.
- `visibility: partial`, with a `permission_denied` error scoped to `pg_stat_activity` and a
  `partial` run: the role is not a superuser and not a member of `pg_read_all_stats` (or
  `pg_monitor`), so other roles' sessions have no state or query. `items` then lists only the
  sessions the role owns, `total` is a lower bound and `hiddenSessions` counts what it cannot
  see. An empty list is not "none" in that case.
- Normal things that look like problems: autovacuum workers and long `VACUUM`, `CREATE INDEX`
  or `pg_dump` sessions (see `backendType` and `applicationName`); a session waiting on `Client`
  (`ClientRead`) is idle in transaction, not slow. A walsender is left out because a
  replication connection stays active for its whole life.
- `blockedBy` lists the immediate blockers (including sessions queued ahead of it for the same
  lock). Follow the pids to the session whose own `blockedBy` is empty: that is the one holding
  the lock, usually an `idleInTransaction` entry.
- `waitEventType: Lock` is a wait for a lock; `IO` and `LWLock` are waits on the server's own
  resources, not on another session.
- `queryId` is null on PostgreSQL 13 and older and when `compute_query_id` is off.
- Failure is an error, not an empty result: `permission_denied` (authentication,
  `pg_hba.conf`, missing role), `not_found` (the connection's database does not exist),
  `unreachable`, `timeout`, `dependency` (no psql).

## What to try next

- A pid in `blockedBy` that is idle in transaction: the application holding it needs fixing
  (a missing commit, a client waiting on an external call); `pg_terminate_backend(pid)` ends it,
  which this task does not do.
- Many sessions, or a pool near its limit: `postgres:pg-connections/connections`.
- A query that is only slow: its `queryId` in pg_stat_statements and `EXPLAIN` are the next step;
  turn on `includeQueryText` to see its shape.
- `permission_denied` scoped to `pg_stat_activity`: `GRANT pg_monitor TO <role>`, then
  `postgres:pg-base/check-permissions` to confirm.
