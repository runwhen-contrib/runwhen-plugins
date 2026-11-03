---
name: k8s-postgres-operator-ops
description: >-
  Remediation actions on a Postgres cluster run by the Crunchy operator
  (PostgresCluster) or the Zalando operator (postgresql), through Patroni inside
  the cluster's own pods: reinitialise a replica that cannot catch up, switch the
  leader over (or fail over to a named replica when there is no healthy leader), and
  rolling-restart every member. Every task changes the cluster and supports a dry run that does
  all the checks and runs nothing. Use after kubernetes:k8s-postgres-operator-health
  has shown what is wrong: no leader, a member stopped or far behind, or a restart
  needed to apply settings. Each result says whether Flux or Argo CD manages the
  object.
runwhen:
  appliesTo:
    types: [kubernetes/postgrescluster, kubernetes/postgresql]
  tags: [remediation, postgres, patroni, failover, restart, crunchy, zalando]
---

## When to use

The cause is known and the fix is one of these three actions. To find the cause first, use
`kubernetes:k8s-postgres-operator-health/patroni-status` (leader, members, lag) and
`cluster-status`.

Every task writes (`readOnly: false`) and needs approval. `reinit-member` and `switchover` are
`destructive`. Run each as a dry run first: it reads the cluster, makes every check the real run
makes, and reports what it would run (`applied: false`, `wouldRun: true`). Patroni has no
dry-run mode, so for `reinit-member` and `switchover` (and the Zalando restart) the dry run
changes nothing and runs no `patronictl` write; for a Crunchy restart it sends the same patch
as a server-side dry run.

The tasks exec `patronictl` in a member pod with the pod's own configuration: no database
credential, no port-forward. They need `get` on the cluster object, `list`/`get` on pods and
`create` on `pods/exec`; the Crunchy restart also needs `patch` on the object. `rolling-restart`
is one task for both operators (task names are fixed), so it declares both sets of rules:
`pods/exec` is used only for Zalando (the Crunchy restart is an annotation patch and never
execs), and the `postgresclusters` patch only for Crunchy; a role for one operator can drop the
other's rule.

## What each task tells you

Every task returns `target`: the cluster object, its `postgresVersion`, `patroniCluster` (the
Patroni scope read from `patronictl list`; Crunchy's is `<name>-ha`), and `gitopsManaged`
(true when Flux or Argo CD labels or annotations are on the object, with the keys in
`gitops.signals`). Nothing is refused for being GitOps managed: the approver decides.

- `reinit-member` (input `member`, `timeout`): needs a leader that is present and `running`,
  else `invalid_input` (switch over or fail over first). `reinit` has the member's `role`,
  `stateBefore`, `lagBeforeMb`, the `leader`, the `command` run and the pod it ran in,
  patronictl's `output`, then `stateAfter` and `completed`: after the Success line the member is
  read until it is running or streaming again (`completionReason: running`) or `timeout` runs
  out. The leader is refused (`invalid_input`), as is a name that is not a member (the members
  are listed).
- `switchover` (optional `candidate`, `timeout`): `mode` is `switchover` when the leader is
  `running`, `failover` when there is no leader or it is `stopped`, `crashed`, `start failed` or
  `restart failed`. A leader in any other state (`restarting`, `starting`, `stopping` ...) is
  refused as `invalid_input` "leader is <state>; retry later". A failover REQUIRES `candidate`
  (nothing is chosen for you) and refuses one whose lag is unknown. `leaderBefore`,
  `leaderAfter`, `leaderChanged`, `timelineBefore`/`After`, `candidate`, `membersAfter` and
  `completed`. A candidate must be another member that is running or streaming.
- `rolling-restart` (`timeout`): `restart.method` is `annotation` (Crunchy: the bare
  `restarted` annotation, the one the PGO v5 documentation uses, on `spec.metadata.annotations`,
  set to `requestedAt`, the run's reference time; the operator then rolls the instance pods and
  also the pgBouncer and pgBackRest repository host pods) or `patronictl` (Zalando: `patronictl
  restart <cluster> <member> --force` once per member, replicas first and the leader last, each
  waited for before the next). `completed` says whether every member was back within `timeout`;
  `membersExpected`, `membersFound`, `membersReady`, `membersRestarted` and `members` show where
  it stood, and for Zalando `memberResults` has each member's `status`, `output` and `detail`.

## How to read the result

A write counts as done only when patronictl says so. For `reinit-member` and each Zalando
restart that is its `Success: reinitialize for member X` / `Success: restart on member X` line:
a `Failed:` line, or no Success line, is an `internal` error with `completed: false` even when
patronictl exited 0, and `applied` stays false. `applied: true` therefore means patronictl
confirmed the write (for a Crunchy restart: the API server accepted the patch); it does not mean
the cluster is healthy. `completed: false` with `completionReason: timeout` means the action was
sent and the cluster had not finished within `timeout`; the task still succeeds, and
`patroni-status` shows how it ended. `completionReason: budget` is the same, cut short because
the task's own time (420s less a 30s margin) was running out: waits are capped so a result is
always returned. `completed: null` is not waited (`timeout: 0s`, a dry run) or not observed (an
error says why). A non-zero `patronictl` exit is a failed task with patronictl's own message (for
a Zalando member it is that member's `failed` result); the cluster may be partly changed, read
`patroni-status` before trying again.

A Zalando rolling restart stops at the first member that fails, does not come back running or
streaming with `Pending restart` cleared, or finds no time left: the members after it are
`not_attempted`, never restarted blind, so a leader is not restarted while a replica is down. The
`timeout` input is the wait for each member (at least 1s).

A Crunchy restart is done by the operator, so `membersRestarted` stays 0 until it starts: the
task does not call it complete merely because all pods are Running. If the operator is not
running or does not propagate the annotation, you get `completed: false` after the timeout with
`membersRestarted: 0`. `membersExpected` for Crunchy is the sum of `spec.instances[].replicas`
(1 for a set that does not say). A switchover changes the timeline and drops connections for a
few seconds; a failover can lose transactions the new leader had not received. A reinitialised
member shows `creating replica` or `starting` for as long as the clone takes. Lists stop at 50
entries.

## What to try next

- After any task: `kubernetes:k8s-postgres-operator-health/patroni-status` for leader, state and
  lag, and `cluster-status` for instances ready.
- A reinit refused for no healthy leader, or a leader in a terminal state: pick the member to
  promote from `patroni-status` (lowest known lag) and run `switchover` with it as `candidate`.
  A leader that is only restarting: wait and look again.
- A reinit that does not finish, or a member that keeps failing:
  `kubernetes:k8s-workload-logs/recent-logs` on the pod and `kubernetes:k8s-pvc-health/usage`
  on its data claim (a full disk stops replication).
- A restart that never completes on Crunchy: the operator itself, with
  `kubernetes:k8s-workload-health/pod-status` on its Deployment.
- A change that comes back: check `gitopsManaged`; the change belongs in git.
- The Crunchy restart annotation also appears on the pod templates; a later GitOps sync may
  remove it.

## Tests

Fixtures are recordings of the kind scenario `rw-pgop` (dry runs, refusals, one failed Zalando
restart, forbidden and not-found cases); no fixture runs a write that succeeds. kind runs no
operator and no Patroni: the CRs are synthetic and **`patronictl` is a stub script** that prints
member lists and Patroni-shaped output (a table, then `Success:` lines), changes its state in the
pod and appends writes to a log; in its `fail` mode it prints `Failed:` and exits 0. Real runs
against the stub (with `--allow-write`) are how the write paths were exercised; Patroni's own
wording and behaviour are not observed, only matched case-insensitively from its documented
`Success:`/`Failed:` lines.
