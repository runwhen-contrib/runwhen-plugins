---
name: pg-locks
description: >-
  Lock facts for a PostgreSQL instance. blocking lists every backend waiting for
  a lock that another session holds, with the blockers, lock type and mode
  wanted, wait time, the relation as schema.name, and chain depth, and marks the
  root blockers (idle in transaction is the classic one) with their state and
  transaction age. lock-summary counts pg_locks by mode and granted, gives the
  longest ungranted wait and estimates how full the lock table is against
  max_locks_per_transaction. Use when queries hang, a deploy or migration is
  stuck on a lock, or "out of shared memory" mentions max_locks_per_transaction.
runwhen:
  role: health
  appliesTo:
    types: [postgres/instance]
  tags: [health, locks, blocking]
---

## When to use

Queries are hanging, a migration (`ALTER TABLE`, `CREATE INDEX`) does not finish, or sessions
pile up waiting. Run `blocking` first: it names the session everything waits for. Run
`lock-summary` when you want the shape of the whole lock table (how many waiters, what modes,
how close to the limit) rather than the chains, or after an `out of shared memory ... You might
need to increase max_locks_per_transaction` error.

Both tasks only read, in a read-only session. Neither reads query text. They look at the
database the connection is in (`PGDATABASE`).

## What each task tells you

- `blocking`: `waiters` has one entry per backend waiting for a lock, longest wait first:
  `pid`, `datname`, `usename`, `applicationName`, `state`, `waitMs`, `lockType` and `lockMode`
  wanted, the `relation` (`schema.name`), `blockedBy` (`pg_blocking_pids`: sessions that hold a
  conflicting lock or are queued ahead of it), `rootBlockerPids` and `depth` (1 when the waiter
  is blocked directly by a root blocker, 2 when it waits behind a waiter that waits, and so on).
  `rootBlockers` lists the sessions that block others and are not waiting themselves, with
  `state`, `xactAgeMs` (how long their transaction has been open) and `blockedWaiters`.
  `maxDepth` is the deepest chain.
- `lock-summary`: `locks` has `total`, `granted`, `waiting`, `waitingBackends`, the
  `longestWaitMs` of an ungranted lock (null when nothing waits) and `byMode`, one entry per
  mode and `granted`. `lockTable` has `max_locks_per_transaction`, `capacity`
  (`max_locks_per_transaction * (max_connections + max_prepared_transactions)`, the documented
  size), `tableEntries` (distinct non-fast-path lock tags, what occupies the shared table) and
  `estimatedFillRatio`. This task's own backend is left out of every count.

## How to read the result

Outputs are facts, not verdicts.

- `waiters.total: 0` with `visibility: full` means no backend was waiting for a lock when the
  query ran. Lock waits are brief and common; one snapshot says what is blocked now, not what
  was blocked a minute ago. A wait of a few milliseconds is normal.
- `visibility: partial` (with a `permission_denied` error scoped to `pg_stat_activity`): the
  role is not a member of `pg_monitor` (or `pg_read_all_stats`), so `state` and `xactAgeMs` of
  other roles' sessions are null. The waits and blockers themselves are still listed
  (`pg_locks` and `pg_blocking_pids` are open to every role), but an idle-in-transaction root
  blocker will not show as such. Grant the role and run again.
- A root blocker with `state: idle in transaction` and a large `xactAgeMs` is a client that
  opened a transaction and stopped: it holds its locks until it commits, rolls back or is
  terminated. `state: active` means it is running a statement; `idle` cannot block.
- `relation` resolves only in the connected database. For a lock in another database it is null
  and `lockDatabase` and `relationOid` say where to look. For `lockType: transactionid` or
  `tuple` (row-level conflicts) the waiter waits on another transaction, so `relation` may be
  null: look at the root blocker's session, not at a table.
- A chain can include a waiter that blocks others: the second `ACCESS EXCLUSIVE` request queues
  behind the first, and everything after it queues behind that. Only `rootBlockers` are worth
  acting on first.
- `estimatedFillRatio` is an estimate: fast-path locks (up to 16 per backend) are held outside
  the table, and the real table is shared with other consumers. Near 1.0, expect `out of shared
  memory`; raising `max_locks_per_transaction` needs a restart.
- Lists stop at `maxItems` (default 200); `truncated` is true and `omitted` says how many were
  left out.
- Needs PostgreSQL 14 or later (`pg_locks.waitstart`). The run fails with a code, not an empty
  result: `permission_denied`, `not_found` (the connection's database), `unreachable`,
  `timeout`, `dependency` (no psql).

## What to try next

- A root blocker identified: find what it is doing with the postgres sessions skill (long
  queries, connections), then end the transaction in the application, or terminate the backend
  (`pg_terminate_backend`; a change this skill does not make).
- Many waiters on one relation: look for a migration holding `ACCESS EXCLUSIVE` and set
  `lock_timeout` in the migration.
- Fill ratio high with few waiters: a transaction touching many tables or partitions; raise
  `max_locks_per_transaction` or reduce partitions per statement.
- The instance runs on Kubernetes: pod-level questions belong to
  `kubernetes:k8s-workload-health` on the StatefulSet that hosts it.
