---
name: k8s-postgres-operator-health
description: >-
  Health facts for a Postgres cluster run by the Crunchy operator (PostgresCluster)
  or the Zalando operator (postgresql): instances desired against ready, version,
  conditions or status.PostgresClusterStatus, pgBackRest backup status (last full
  and incremental completion, repos), the member pods with ready and restarts,
  Patroni members (leader present, role, state, timeline, lag in MB), connections
  by state against max_connections with the longest query and idle-in-transaction
  age and waiting locks, and the non-default Postgres settings. The database
  facts come from kubectl exec into the cluster's own pods (local socket, no
  credential). Use when an application cannot reach its database, a failover or
  restart went wrong, replicas lag, or backups are suspected stale.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/postgrescluster, kubernetes/postgresql]
  tags: [health, postgres, patroni, crunchy, zalando, database]
---

## When to use

The target is a `PostgresCluster` (Crunchy, `postgres-operator.crunchydata.com`) or a
`postgresql` (Zalando, `acid.zalan.do`) and the database is unavailable, slow, refusing
connections, failing over, or not backed up. Start with `cluster-status` (Kubernetes API only),
then `patroni-status` for leadership and lag, then `connections` and `settings` for what Postgres
itself reports.

`cluster-status` needs `get` on the object and `list` on pods. The other three exec into a pod
(`pods/exec`, `create`), so a role that can only read objects gets `permission_denied` from them
and still gets `cluster-status`. They use the pod's own `patronictl` and `psql` over the local
socket: no database credential and no port-forward. A separate postgres plugin, with its own
connection, covers SQL that needs a login.

## What each task tells you

- `cluster-status`: `cluster` (kind, operator, `postgresVersion`, `instances` with `desired`,
  `ready` and per-set counts, `conditions`, `generationObserved`, and for Zalando
  `clusterStatus`), `backups` and `members`.
  - `backups` (Crunchy): `repos` with `bound` and `stanzaCreated`; `lastFull`, `lastIncr`,
    `lastDiff` and `last`, each the newest scheduled backup whose Job completed, with
    `ageSeconds`; `neverCompleted` counts scheduled entries with no completion; `manual` and
    `restore` when one exists. Zalando objects carry no backup status (`supported: false`).
  - `members`: the pods the operator's labels select, each with `ready`, `restarts`, the role
    label (`master`/`primary`/`replica`) and every container (init containers have `init: true`).
- `patroni-status`: `patroni` with `leaderPresent`, `leader`, `timelines`, `timelineMismatch`,
  `maxLagMb`, `notRunning`, `aboveLagThreshold` (setting `lagThresholdMb`, default 100) and each
  member's `role`, `state`, `running`, `timeline`, `lagMb`, `lagUnknown`, `pendingRestart`.
  `queried` names the pod asked.
- `connections`: `connections` with `byState`, `total` against `usableConnections`
  (`max_connections` less `superuser_reserved_connections`), `utilizationPercent`,
  `aboveThreshold` (setting `connectionUtilizationPercent`, default 80),
  `longestQuerySeconds`, `longestIdleInTransactionSeconds` and `waitingLocks`. Counts leave out
  the asking session. No query text.
- `settings`: `settings.items`, the non-default `pg_settings` rows (`name`, `setting`, `unit`,
  `source`). Names matching `password|secret|key|ssl_.*file` are left out (`excludedCount`);
  `redacted: true` marks a setting that can embed a connection string or command, with no value.
  Rows with source `client` or `session` belong to the asking connection and are not listed.

## How to read the result

`leaderPresent: false` is a cluster with no writer: the apps cannot write, which is the finding,
not a failed task. `leaderPresent: null` and `queried: null` mean no member pod could be asked
(an error says so), which is not the same as no leader. A replica's `state` is normally
`streaming` (older Patroni says `running`); `creating replica`, `starting`, `stopped` and
`start failed` are not up. `lagMb: null` with `lagUnknown: true` is a replica Patroni could not
measure, not zero. A leader has no lag. `timelineMismatch: true` after a failover is normal for
a moment: a member still on the old timeline is being rewound or has to be reinitialised.

`instances.ready: null` on a Crunchy object means the operator has written no status yet.
`instances.desired` counts `replicas: 1` for an instance set that does not say. The Zalando
object has no readiness: `readySource: pods` says it was counted from the pods, and is null if
the pod list was forbidden (the error names scope `members`). A Zalando `clusterStatus` of
`Running` is the operator's view of its own work, not proof the database accepts connections.

`backups.lastFull: null` with `neverCompleted > 0` is a schedule that has never finished; with
`scheduledCount: 0` there is no scheduled backup in the status (a cluster that is only backed up
by hand shows it under `manual`). It is never reported as a recent backup. Lists stop at
`maxItems` (default 200); nested lists at 50.

`connections` counts client backends; `total` near `usableConnections` is saturation, a large
`longestIdleInTransactionSeconds` is a session holding locks and blocking vacuum, and
`waitingLocks > 0` with a long `longestQuerySeconds` points at a blocked query. `inRecovery: true`
means the pod labelled primary is really a replica (a stale label during a failover);
`primaryCandidates > 1` means more than one pod carries the primary label. These are facts about
this moment: take a second reading before acting.

`connections` and `settings` fail with `not_found` when no Running pod carries the primary role
label, and `permission_denied` when `pods/exec` is not granted. A pod without `psql` or
`patronictl` is `dependency`. No events are collected here.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-logs/recent-logs` on the pod's
  StatefulSet or the namespace, and `kubernetes:k8s-namespace-health/warning-events` for the
  events (this skill does not collect pod logs or events).
- No leader, a stopped member or a member far behind: `kubernetes:k8s-postgres-operator-ops/switchover`
  (or its failover path) for no leader, `reinit-member` for a replica that cannot catch up;
  `kubernetes:k8s-postgres-operator-ops/rolling-restart` after a settings change that needs a
  restart (`pendingRestart: true`).
- Saturated connections: find the clients in the apps that use the database; terminating
  sessions needs a login and belongs to the postgres plugin.
- Volume full or a claim Pending: `kubernetes:k8s-pvc-health/binding-status` and `usage` on the
  data claim.
- After an action, run `patroni-status` again to read the new state.

## Tests

Fixtures replay recorded `kubectl` output from the kind scenario `rw-pgop`. kind runs no operator
and no Patroni: the CRs are synthetic with the status an operator would write, the primary pods
run a real `postgres:16-alpine` for psql, and **`patronictl` is a stub script** that prints
recorded member lists. Patroni's real output is therefore reproduced from its documented
`list -f json` shape, not observed.
