---
name: pg-base
description: >-
  The PostgreSQL integration's own skill. discover reads an instance and lists its
  databases (owner, encoding, size) and reports the instance's system identifier;
  check-permissions tests role memberships (pg_monitor, pg_read_all_stats...),
  database privileges (CONNECT, CREATE, TEMPORARY) and function EXECUTE grants
  against the connecting role. Use to see what is on an instance, or to find out
  why another postgres skill reports permission_denied.
runwhen:
  role: [discovery, permission-check]
  appliesTo:
    types: [postgres/instance]
  tags: [discovery, permission-check]
---

## When to use

Reach for `discover` to see what an instance holds: the version, whether it is a primary or a
standby, and every database that accepts connections. Reach for `check-permissions` when a
postgres skill came back `permission_denied` or `partial`, to see which role membership or
privilege the connection is missing. The platform also runs both on its own: discovery every
15 minutes, the permission check when a connection is confirmed and daily.

Both tasks only read, with the connection's own libpq environment. Each query runs as one
script, `BEGIN READ ONLY; SET LOCAL statement_timeout = ...; <query>; COMMIT;`: the queries are
the task's own fixed text and cannot write. Nothing is set on the session itself, so a pooler
that shares server connections (pgbouncer in transaction mode) carries no setting over to
another client. The `connectTimeout`
(default 10s) and `statementTimeout` (default 30s) settings bound it; both are clamped to what
the server accepts (at least 1 ms, at most 2147483647 ms). `discover` has a 180s task timeout.

## What each task tells you

- `discover` returns `instance` (host, port, version, `inRecovery`, `systemIdentifier`,
  the role the connection runs as), `databases` (name, owner, encoding, `sizeBytes`),
  `summary` (counts and per-step errors) and a `sync` artifact: the whole `rw.discovery.v1`
  sync the platform ingests. Templates and databases that refuse connections are left out.
  `summary.root.canonicalKey` is the instance's system identifier. `databases` is
  `{items, total, omitted}`: `total` is every database found, `items` the first `maxItems`
  (default 200) of them and `omitted` how many were cut. Only that list is cut: the `sync`
  artifact always holds every database. When the run has the per-run `runwhen/sync`
  credential (`RW_SYNC_CREDENTIAL`) `discover` also pushes the sync to the platform: open,
  batches, commit. A push that fails fails the task; a commit the platform holds, or items it
  rejects, make the run `partial` with an `internal` error scoped `sync`. Without the
  credential (a local `rwtask run`) the artifact is all it produces.
- `check-permissions` takes `rules`, each one of `{role}`, `{database, privilege}` (a name
  or `"*"` for every database that accepts connections) or `{function, privilege: EXECUTE}`
  (`schema.name`, all overloads), and returns `access`: one entry per rule, in input order,
  with `verdict` `granted`, `partial` (with `scope`), `denied` or `unverified`, and the
  evidence. A role, database or function that does not exist is `denied`.

## How to read the result

- A database without `sizeBytes` and a `permission_denied` error scoped to that database: the
  role has neither CONNECT on it nor `pg_read_all_stats`. The run is `partial`; every other fact
  stands. Function EXECUTE alone is not enough for statistics and sizes: the role must also be
  a member of `pg_read_all_stats` (or `pg_monitor`), so a skill that needs them declares
  `{role: pg_read_all_stats}`.
- `databases.omitted` above zero: the list was cut at `maxItems`; raise the setting, or read
  the `sync` artifact for the whole list.
- `systemIdentifier: null` with an error scoped to `systemIdentifier`: the role cannot call
  `pg_control_system()`, so the platform cannot key the instance by identity.
- A physical standby reports the same system identifier as its primary; `inRecovery: true`
  tells them apart. Connecting a standby and its primary as two connections makes them one
  root.
- `partial` on `{database: "*"}` lists the databases where the privilege is granted in
  `scope`; on a function it lists the overloads that are executable.
- `role` verdicts follow inheritance: a member of `pg_monitor` through a role created
  `NOINHERIT` is `denied` because its privileges are not in effect without `SET ROLE`.
- `unverified` means the rule is not one of the three shapes; the evidence says why.
- A role name in a rule is matched exactly, as `pg_roles.rolname`: case counts and a name with
  `.` or `"` is just a name. `PG_MONITOR` is not `pg_monitor`.
- A rule the task cannot hash or evaluate is `unverified` with the reason; the rest of the
  report stands.
- The run fails with a code, not an empty result, classified from libpq's own words:
  - `permission_denied`: authentication failed, `no pg_hba.conf entry`, `pg_hba.conf rejects
    connection`, `is not permitted to log in`, missing role, `permission denied`.
  - `rate_limited`: `too many clients already`, `remaining connection slots are reserved`,
    `too many connections for role`.
  - `not_found`: the connection's database does not exist (`database "x" does not exist`, or
    pgbouncer's `no such database`).
  - `unreachable`: cannot connect, DNS, TLS, and a connection that was cut: `conflict with
    recovery`, `terminating connection due to administrator command`, `connection to server was
    lost`, `SSL connection has been closed unexpectedly`, `pgbouncer cannot connect to server`.
  - `timeout`: the connect or statement timeout. `dependency`: no psql, or a pooler's
    `unsupported startup parameter`. Anything else is `internal`.
  - A system identifier the role may not read (`permission_denied`) is reported and the run goes
    on. Any other failure reading it fails the run.

## What to try next

- Permissions missing for a skill: grant the role what the rule names (`GRANT pg_monitor TO
  <role>`), then run `postgres:pg-base/check-permissions` again.
- The database a symptom points at is listed here; run the postgres health skills against it.
- The instance runs on Kubernetes: pod-level questions (restarts, volumes, the Service) belong
  to `kubernetes:k8s-workload-health` on the StatefulSet that hosts it.
