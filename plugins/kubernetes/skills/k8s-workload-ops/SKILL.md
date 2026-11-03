---
name: k8s-workload-ops
description: >-
  Remediation actions on a Kubernetes Deployment, StatefulSet or DaemonSet:
  rolling restart, rollback to a previous revision, scale replicas, delete
  pods, and set CPU and memory requests and limits. Every task changes the
  cluster and supports a dry run that shows what would change. Use after
  diagnosis, when the fix is a restart, a rollback, more or fewer replicas, a
  stuck pod removed, or a resource request or limit corrected. Each result
  says whether Flux or Argo CD manages the workload.
runwhen:
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset, kubernetes/daemonset]
  tags: [remediation, restart, rollback, scale, resources]
---

## When to use

The cause is known and the fix is one of these actions on the workload. To find
the cause first, use `kubernetes:k8s-workload-health/rollout-status`,
`kubernetes:k8s-workload-health/pod-restarts` and
`kubernetes:k8s-workload-logs/error-patterns`.

Every task writes (`readOnly: false`) and needs approval. Run it as a dry run
first: the server validates the change and the result says what would change.
Under a dry run `applied` is false and every "done" field is empty or false:
`changed` stays false and `wouldChange` says whether a real run would change
something; `deleted` is empty and `wouldDelete` lists the pods; `restartedAt`
is null. Permissions are namespace-scoped RBAC on the target's own namespace;
the task list shows exactly which verbs each one uses.

## What each task tells you

Every task returns `target`: the workload, its `generation`, and
`gitopsManaged` (true when Flux or Argo CD labels or annotations are on it,
with the keys in `gitops.signals`). Nothing is refused for being GitOps
managed: the approver decides, knowing the controller may revert the change.

- `rollout-restart`: one patch sets the pod template's restart annotation and
  the workload annotation `runwhen.com/restart-reason` to `reason`; with no
  `reason` an earlier reason annotation is removed (`reasonPresentBefore` says
  whether there was one, `reasonCleared` that it went). Then `revisionBefore`,
  `revisionAfter`, and whether the rollout finished within `timeout` (default
  120s, at most 240s). `completed: false` with `completionReason` `timeout`,
  `progress_deadline_exceeded` or `update_strategy_on_delete` means the restart
  was sent but the rollout was slow or stuck; the task still succeeds.
  `updateStrategy` matters: `Recreate` stops all pods at once, and `OnDelete`
  replaces nothing until pods are deleted (also true of rollback and
  set-resources).
- `rollback`: `from` and `to` (revision number and images, init containers
  flagged `init: true`), `available` revisions, and `revisionHistoryLimit`. No
  previous revision, or a `toRevision` that is not in the history, is refused.
  It does not wait for the rollout.
- `scale`: `from`, `to`, `readyBefore`, and the autoscalers targeting the
  workload. A DaemonSet is refused. Scaling to 0 is refused unless the
  `allowScaleToZero` setting is true. An autoscaler targeting the workload
  refuses the scale unless `overrideHpa` is true. Autoscaler bounds
  (min/max) cannot be changed by this skill.
- `delete-pods`: the pods deleted (`deleted`; `wouldDelete` on a dry run),
  `alreadyGone` for requested pods that no longer existed, `ownedCount`, and
  `deletesAll`. Only pods the workload owns (selector plus ownerReferences)
  are touched; a name in `podNames` that is not one of them fails the task and
  nothing is deleted. If a later batch fails, the pods already deleted are
  still reported, `remainingCount` says how many requested pods were not
  deleted, and the result is partial with an error naming the failure.
  Destructive: the controller makes replacements.
- `set-resources`: `fields` with `before` and `after` for `cpuRequest`,
  `cpuLimit`, `memoryRequest`, `memoryLimit` (`differs` per field). Give at
  least one explicit quantity; a request above its limit, a zero value, an
  absurd exponent, and a memory value under 1Mi with no unit (a bare number is
  bytes; did you mean `Mi`?) are refused. `hpaPresent: true` means an
  autoscaler targets the workload: changing a CPU or memory request shifts
  the utilization at which it scales, so review its target; `null` means the
  autoscalers could not be listed (an error on the result says why).

## How to read the result

A result with `applied: false` changed nothing: either a dry run, or the
workload already had the requested state (`changed: false`, `wouldChange:
false`). `applied: true` means the API server accepted the write. A failure (forbidden, target missing,
API unreachable) is an error on the result and never an empty success; a
refused guard is `invalid_input` and tells you which input to change.

A restart, rollback or resource change replaces pods, so ready replicas dip
while it runs. Lists (`deleted`, `pods`) stop at `maxItems` (default 200); the
counts next to them do not. The kind-specific traps: a StatefulSet replaces
pods in reverse ordinal order, one at a time, so a restart is slow by design;
a DaemonSet has no replicas; a Deployment history is bounded by
`revisionHistoryLimit`, so an old revision may no longer exist to roll back to.

## What to try next

- After a restart, rollback or resource change:
  `kubernetes:k8s-workload-health/rollout-status` and `pod-status` for how the
  new pods came up; `kubernetes:k8s-workload-logs/recent-logs` if they crash.
- A rollback that does not stay: check `gitopsManaged`; the change belongs in
  git, not on the cluster.
- A scale that is undone: `kubernetes:k8s-workload-health/hpa-status`.
- Pods that stay Pending after a resource increase:
  `kubernetes:k8s-node-health/node-conditions`.
