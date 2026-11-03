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
  that condition type, with `readyReason`; `suspended`; `generationObserved` (false while the
  controller has not seen the latest spec); every condition with its message. `revisions`:
  `lastAppliedRevision`, `lastAttemptedRevision`, `revisionsDiffer`, `neverApplied`,
  `lastHandledReconcileAt`, and `helm` (chart, source, release `history`) or `kustomization`
  (source, path, `inventoryCount`). `dependencies`: each `dependsOn` entry with `exists`, `ready`
  and `readyReason`.
- `reconcile`: sets `reconcile.fluxcd.io/requestedAt`. `completed: true` means the controller
  handled that request; `completed: false` means `timeout` ran out first (the controller may
  still be working, or may be down); `null` means it did not wait. `after` is the object as last
  read. The source is not reconciled first: a new commit the source controller has not fetched
  yet is not seen. A suspended object is refused (`invalid_input`).
- `suspend` and `resume`: `suspension` has `suspendedBefore` and `suspendedAfter`; a dry run
  sets `wouldChange`. `target.managedBy` names a Flux object that applies this one from git and
  may put `spec.suspend` back. Resuming applies everything the source gained while paused.

## How to read the result

`ready: null` means the object has no `Ready` condition (a controller has not looked at it yet),
not that it is healthy. `ready: "False"` with `stalled: "True"` means Flux gave up retrying
(for a HelmRelease, retries exhausted); it will not try again until the spec changes or a
reconcile is requested. A Kustomization that is Stalled may have no `Reconciling` condition, so
`reconciling: null` there is normal. `ready: "False"` without `stalled` is a failing attempt that
is still being retried.

`revisionsDiffer: true` is a stuck upgrade: a revision was attempted and the applied one is the
older. `neverApplied: true` means nothing has been applied yet (a first install failing or still
pending). For a HelmRelease on the `v2` API there is no `lastAppliedRevision` field: the applied
revision is the chart version of the newest `history` entry Helm reports as `deployed`
(`appliedSource` says which was used).

`dependencies.items[].read: false` is a dependency in another namespace (not read, `note` says
so) or one that could not be read; `ready` is null there, not `False`. `exists: false` is a
`dependsOn` name with no object; Flux then reports the object as not ready for that reason.
At most 50 dependencies are listed (`count` is the real number); lists stop at `maxItems`
(default 200). A suspended object keeps its last `Ready` condition, so `suspended: true` with
`ready: "True"` is normal and says nothing about the live state.

## What to try next

- `ready: "False"` with a pod-level cause (a HelmRelease timed out waiting for the workload):
  `kubernetes:k8s-workload-health/rollout-status` and `pod-status` on the workload, and
  `kubernetes:k8s-workload-logs/recent-logs`.
- A `BuildFailed` or `ArtifactFailed` reason, or no `Reconciling` condition at all: the Flux
  controllers themselves; read their logs with `kubernetes:k8s-workload-logs/error-patterns` on the
  Deployments in the `flux-system` namespace.
- A dependency that is not Ready: run `ready-status` on it; one in another namespace needs its
  own target.
- After `suspend`, fix by hand, then `resume` and `reconcile`; the change belongs in git. The
  results of `kubernetes:k8s-workload-ops` tasks report `gitopsManaged` for workloads Flux applies.
