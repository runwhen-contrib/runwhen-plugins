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

Both tasks only read, in a read-only session, with the connection's own libpq environment.

## What each task tells you

- `discover` returns `instance` (host, port, version, `inRecovery`, `systemIdentifier`,
  the role the connection runs as), `databases` (name, owner, encoding, `sizeBytes`),
  `summary` (counts and per-step errors) and a `sync` artifact: the whole `rw.discovery.v1`
  sync the platform ingests. Templates and databases that refuse connections are left out.
  `summary.root.canonicalKey` is the instance's system identifier.
- `check-permissions` takes `rules`, each one of `{role}`, `{database, privilege}` (a name
  or `"*"` for every database that accepts connections) or `{function, privilege: EXECUTE}`
  (`schema.name`, all overloads), and returns `access`: one entry per rule, in input order,
  with `verdict` `granted`, `partial` (with `scope`), `denied` or `unverified`, and the
  evidence. A role, database or function that does not exist is `denied`.

## How to read the result

- `sizeBytes: null` with a `permission_denied` error scoped to that database: the role has
  neither CONNECT on it nor `pg_read_all_stats`. The run is `partial`; every other fact stands.
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
- The run fails with a code, not an empty result: `permission_denied` (authentication
  failed, no `pg_hba.conf` entry, missing role), `not_found` (the connection's database does
  not exist), `unreachable` (cannot connect, DNS, TLS), `timeout`, `dependency` (no psql).

## What to try next

- Permissions missing for a skill: grant the role what the rule names (`GRANT pg_monitor TO
  <role>`), then run `postgres:pg-base/check-permissions` again.
- The database a symptom points at is listed here; run the postgres health skills against it.
- The instance runs on Kubernetes: pod-level questions (restarts, volumes, the Service) belong
  to `kubernetes:k8s-workload-health` on the StatefulSet that hosts it.
