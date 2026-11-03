---
name: k8s-flux-health
description: >-
  Health of a Flux HelmRelease or Kustomization: whether it is Ready,
  Reconciling or Stalled with the reason and message, whether it is suspended,
  whether the controller has seen the latest spec, the last applied against the
  last attempted revision (chart version, or git revision), and whether each
  object it dependsOn exists and is Ready. Also three write tasks: request a
  reconcile now, suspend reconciliation, resume it. Use when a GitOps deploy
  did not roll out, a release is stuck on an old version, or Flux has to be
  paused while someone fixes something by hand. It does not look for newer
  chart versions (that needs the chart repository).
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/helmrelease, kubernetes/kustomization]
  tags: [health, gitops, flux, helm, kustomize]
---

## When to use

The target is a Flux `HelmRelease` or `Kustomization` and a change in git did not reach the
cluster, a release is failing to install or upgrade, or a workload keeps being reverted. Start
with `ready-status`. Use `reconcile` to ask Flux to try again now, and `suspend` before hand
fixes on a workload that Flux manages (it would put them back), `resume` afterwards.

`ready-status` needs `get` on the object. It reads dependencies from the object's own
namespace only, so no cluster-wide permission is needed. The three write tasks need `get` and
`patch`, are `readOnly: false`, and support a dry run.

## What each task tells you

- `ready-status`: three outputs. `state`: `ready`, `reconciling` and `stalled` are the status of
  that condition type, with `readyReason`, `readyLastTransitionTime`; `healthy` (a Kustomization's
  health checks on what it applied); `released` and `testSuccess` (a HelmRelease's install or
  upgrade, and its tests); `suspended`; `generationObserved` (false while the controller has not
  seen the latest spec); every condition with its message. `revisions`: `lastAppliedRevision`,
  `lastAttemptedRevision`, `revisionsDiffer`, `neverApplied`, `lastHandledReconcileAt`, and `helm`
  (chart, source, release `history`) or `kustomization` (source, path, `inventoryCount`).
  `dependencies`: each `dependsOn` entry with `exists`, `ready` and `readyReason`.
- `reconcile`: sets `reconcile.fluxcd.io/requestedAt`. It waits until the controller reports the
  request handled **and** the `Reconciling` condition is no longer True (the controller records a
  request when it starts on it, so Ready read at that moment may still be the old one).
  `completed: true`: handled and finished; read `after.ready`, since handled is not succeeded.
  `completed: false`: `timeout` ran out, with `completionReason` `timeout` (not handled; the
  controller may be busy or down) or `reconciling` (handled, still working). `completed: null`:
  the task did not wait (`timeout: 0s`, a dry run) or could not observe it (`not_observed`, an
  error says why). The source is not reconciled first: a commit the source controller has not
  fetched yet is not seen. A suspended object is refused (`invalid_input`).
  - `reset: true` (HelmRelease only) also sets `resetAt`. Use it when the release is `Stalled`
    with `RetriesExceeded`: a plain request does not reset an exhausted retry budget. The result
    waits for `after.lastHandledResetAt`.
  - `force: true` (HelmRelease only) also sets `forceAt`: a one-off forced upgrade, for a release
    that must be re-applied although nothing changed (it can restart workloads). Neither is
    valid on a Kustomization (`invalid_input`).
- `suspend` and `resume`: `suspension` has `suspendedBefore` and `suspendedAfter` (read back from
  the cluster after a real change; null with a `verify` error if that read failed); a dry run
  sets `wouldChange`. `target.managedBy` names a Flux object that applies this one from git and
  may put `spec.suspend` back. Resuming applies everything the source gained while paused; a
  Kustomization then also corrects drift, a HelmRelease only with `driftDetection` or a chart or
  values change.

## How to read the result

`ready: null` means the object has no `Ready` condition (a controller has not looked at it yet),
not that it is healthy. `ready: "Unknown"` with `reconciling: "True"` is a reconcile in progress,
not a failure. `ready: "False"` with `stalled: "True"` means Flux gave up retrying (for a
HelmRelease, retries exhausted); it will not try again until the spec changes or a reconcile with
`reset` is requested. A Kustomization that is Stalled may have no `Reconciling` condition, so
`reconciling: null` there is normal. `ready: "False"` without `stalled` is a failing attempt that
is still being retried. `healthy: "Unknown"` means the health checks are still waiting.

While bootstrapping (a new cluster, many objects created together), `DependencyNotReady` and a
Kustomization waiting on its health checks are normal for a few minutes; judge by whether
`readyLastTransitionTime` keeps moving and the dependency eventually turns Ready.

`revisionsDiffer: true` is **not** by itself a stuck upgrade. It is also what an upgrade in flight
looks like (`helm.history[0].status` `pending-upgrade`, `reconciling: "True"`). Call it stuck only
when `reconciling` is not True and `readyLastTransitionTime` is old, or `ready` is `False`. Build
metadata (`+abc`) on a chart version is ignored in the comparison. `neverApplied: true` means
nothing has been applied yet (a first install failing or still pending). For a HelmRelease on the
`v2` API there is no `lastAppliedRevision` field: the applied revision is the chart version of the
newest `history` entry Helm reports as `deployed` (`appliedSource` says which was used).

`dependencies.items[].read: false` is a dependency in another namespace (not read, `note` says
so) or one that could not be read; `ready` is null there, not `False`. `exists: false` is a
`dependsOn` name with no object; Flux then reports the object as not ready for that reason.
At most 50 dependencies are listed (`count` is the real number, `truncated` says some were left
out); lists stop at `maxItems` (default 200). A suspended object keeps its last `Ready`
condition, so `suspended: true` with `ready: "True"` is normal and says nothing about the live
state.

## What to try next

- `ready: "False"` with a pod-level cause (a HelmRelease timed out waiting for the workload):
  `kubernetes:k8s-workload-health/rollout-status` and `pod-status` on the workload, and
  `kubernetes:k8s-workload-logs/recent-logs`.
- A `BuildFailed` or `ArtifactFailed` reason, no `Reconciling` condition for long, or
  `completionReason: timeout` on every reconcile: the Flux controllers themselves. Check the
  controller Deployments in the `flux-system` namespace with
  `kubernetes:k8s-workload-health/pod-status` and `rollout-status`, and their logs with
  `kubernetes:k8s-workload-logs/error-patterns`.
- A HelmRelease `Stalled` with `RetriesExceeded` after the cause is fixed: `reconcile` with `reset`.
- A dependency that is not Ready: run `ready-status` on it; one in another namespace needs its
  own target.
- After `suspend`, fix by hand, then `resume` and `reconcile`; the change belongs in git. The
  results of `kubernetes:k8s-workload-ops` tasks report `gitopsManaged` for workloads Flux applies.
