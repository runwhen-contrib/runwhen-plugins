---
name: k8s-postgres-operator-ops
description: >-
  Remediation actions on a Postgres cluster run by the Crunchy operator
  (PostgresCluster) or the Zalando operator (postgresql), through Patroni inside
  the cluster's own pods: reinitialise a replica that cannot catch up, switch the
  leader over (or fail over when there is no healthy leader), and rolling-restart
  every member. Every task changes the cluster and supports a dry run that does
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
`create` on `pods/exec`; the Crunchy restart also needs `patch` on the object.

## What each task tells you

Every task returns `target`: the cluster object, its `postgresVersion`, `patroniCluster` (the
Patroni scope read from `patronictl list`; Crunchy's is `<name>-ha`), and `gitopsManaged`
(true when Flux or Argo CD labels or annotations are on the object, with the keys in
`gitops.signals`). Nothing is refused for being GitOps managed: the approver decides.

- `reinit-member` (input `member`): `reinit` has the member's `role`, `stateBefore`,
  `lagBeforeMb`, the `leader`, the `command` run and the pod it ran in, patronictl's `output`,
  and `stateAfter` read once afterwards. The leader is refused (`invalid_input`), as is a name
  that is not a member (the members are listed). Reinit only starts the re-clone: expect
  `stateAfter` to be `creating replica`; follow it with `patroni-status`.
- `switchover` (optional `candidate`, `timeout`): `mode` is `switchover` when a healthy leader
  (Leader, running) exists, else `failover`. `leaderBefore`, `leaderAfter`, `leaderChanged`,
  `timelineBefore`/`After`, `candidate` and `candidateChosenBy` (`input`, or `auto` for a
  failover given no candidate: Patroni's failover needs one, so the running replica with the
  least known lag is chosen), `membersAfter`, and `completed`. A candidate must be another
  member that is running or streaming.
- `rolling-restart` (`timeout`): `restart.method` is `annotation` (Crunchy: the
  `postgres-operator.crunchydata.com/restarted` annotation on `spec.metadata.annotations`, set
  to `requestedAt`, the run's reference time) or `patronictl` (Zalando: `patronictl restart
  <cluster> --force`). `completed` says whether every member was back within `timeout`;
  `membersExpected`, `membersFound`, `membersReady`, `membersRestarted` and `members` show where
  it stood.

## How to read the result

`applied: true` means the write was accepted (patronictl exited 0, or the API server accepted
the patch); it does not mean the cluster is healthy. `completed: false` with
`completionReason: timeout` means the action was sent and the cluster had not finished within
`timeout`; the task still succeeds, and `patroni-status` shows how it ended. `completed: null` is
not waited (`timeout: 0s`, a dry run) or not observed (an error says why). A failed `patronictl`
is a failed task with patronictl's own message; the cluster may be partly changed, read
`patroni-status` before trying again.

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
- A reinit that does not finish, or a member that keeps failing:
  `kubernetes:k8s-workload-logs/recent-logs` on the pod and `kubernetes:k8s-pvc-health/usage`
  on its data claim (a full disk stops replication).
- A restart that never completes on Crunchy: the operator itself, with
  `kubernetes:k8s-workload-health/pod-status` on its Deployment.
- A change that comes back: check `gitopsManaged`; the change belongs in git.
- The Crunchy restart annotation also appears on the pod templates; a later GitOps sync may
  remove it.

## Tests

Fixtures are dry-run recordings of the kind scenario `rw-pgop` plus forbidden and not-found
cases; no fixture runs a real write. kind runs no operator and no Patroni: the CRs are synthetic
and **`patronictl` is a stub script** that prints recorded member lists, accepts the write
sub-commands and appends them to a log in the pod. Real runs against the stub (with
`--allow-write`) are how the write paths were exercised; Patroni's own behaviour is not.
