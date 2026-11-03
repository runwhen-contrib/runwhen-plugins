---
name: k8s-pvc-health
description: >-
  Health facts for a Kubernetes PersistentVolumeClaim: binding phase, whether its
  StorageClass and bound PersistentVolume exist, requested versus bound capacity,
  events about the claim, whether it is stuck Terminating and which pods still
  mount it, filesystem usage of the mounted volume, and for ReadWriteOnce claims
  the nodes that mount it and its VolumeAttachments. Use when a claim is Pending,
  a pod waits on a volume, a claim will not delete, a volume is filling up or a
  volume will not attach or mount across nodes.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/persistentvolumeclaim]
  tags: [health, storage, pvc, volumes]
---

## When to use

The target is a PersistentVolumeClaim and a workload that uses it is Pending,
`ContainerCreating`, failing to mount, running out of space, or a claim will not
go away. Start with `binding-status` for the shape of the problem, then the task
that matches the symptom.

Every task needs `get` on the claim. Most of what they read is in the claim's own
namespace. Three reads are **cluster-scoped** and need a ClusterRole, not a
Role: `storageclasses` and `persistentvolumes` (`binding-status`), and
`volumeattachments` (`attachment`). A namespace-scoped identity still gets the
claim, its events and its pods; the cluster-scoped part comes back empty with a
`permission_denied` error (see below). `usage` runs `df` through `pods/exec`
(`create`) and is its own task so a forbidden exec fails only that task. No task
reads Secrets or logs.

## What each task tells you

- `binding-status`: `claim` has phase, StorageClass name, bound volume name,
  access modes, requested versus bound capacity (`boundBelowRequested` is true
  while a resize is pending) and the claim's conditions. `storageClass` says
  whether that class exists and its provisioner, `volumeBindingMode` and whether
  it can expand. `volume` is the bound PersistentVolume: phase, capacity,
  reclaim policy, `claimRefMatches`, and `terminating` (the PV has a
  deletionTimestamp; a PV has no Terminating phase, so this replaces the
  classic "PersistentVolumes in Terminating State" check). `events` are the
  claim's events within `since`, Normal and Warning, grouped by type and reason.
  A Pending claim whose only event is a Normal `WaitForFirstConsumer` is waiting
  for a pod, not broken; `ProvisioningFailed` is the provisioner saying why not.
- `terminating`: `termination.terminating`, the `deletionTimestamp`, how many
  seconds ago, and the finalizers. `pods` lists every pod in the namespace that
  mounts the claim; `active: false` (Succeeded or Failed) no longer holds it. A
  claim kept only by `kubernetes.io/pvc-protection` is waiting for its active
  pods to go; a custom finalizer has to be removed by whatever owns it.
- `usage`: size, used and available bytes, `percent` and `atOrAbove` against the
  `usageThresholdPercent` setting (default 85), as one container sees it. It
  runs `df -Pk` in one Running, ready pod that mounts the claim; `runningPods`
  says how many were candidates. With no Running pod the task is `skipped`
  (`no running pod mounts this claim`), not an empty result. A container without
  `df`, or an exec denied by RBAC, fails the task.
- `attachment`: `access` has the access modes, the nodes that active pods mount
  the claim from, and `multiNodeConflict`, true when a ReadWriteOnce claim is
  mounted from more than one node. `volumeAttachments` are the cluster's
  VolumeAttachment objects for the PV: node, `attached`, `attachError`,
  `detachError`. `volumeAttachments: []` means there are none, which is normal
  for volumes that are not attached through CSI (hostPath, local, kind's
  local-path) and for a claim that is not bound.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `events: []` means no event about the claim was last seen
  within `since` (events expire after about an hour by default, so an old
  problem can have none). `pods: []` means no pod in the namespace references
  the claim. `volumeAttachments: []` means none exist for the PV: normal for
  volumes not attached through CSI (hostPath, local, kind's local-path) and for
  a claim that is not bound. None of these says the volume works.
- **`null` is unknown.** A part that could not be read is `null` (`exists: null`
  for a StorageClass or volume, `events: null`, `volumeAttachments: null`) with
  a `permission_denied` error scoped `storageclasses`, `persistentvolumes`,
  `events` or `volumeattachments`. Only `exists: false` means the object is
  missing. A result with errors is not a clean bill of health for the outputs it
  did return.
- **Cuts.** List outputs stop at `maxItems` (default 200); lists inside an
  entry (mounts, conditions, finalizers) stop at 50. Messages are cut at 1024
  characters and flagged `truncated`. `since` takes `ms|s|m|h|d` combinations
  (default `1h`); anything else is an `invalid_input` error.
- **Normal states that look like failures.** A Pending claim whose StorageClass
  has `volumeBindingMode: WaitForFirstConsumer` stays Pending, with only a
  Normal `WaitForFirstConsumer` event, until a pod uses it. A claim held by
  `kubernetes.io/pvc-protection` is Terminating while a pod still mounts it. On
  kind and other node-local storage, `usage` shows the node's whole disk.
  `usage` runs `df` only; a container without `df` fails the task.

## What to try next

- Pending because a pod is unschedulable or waiting on the volume:
  `kubernetes:k8s-workload-health/pod-status` on the workload that mounts it and
  `kubernetes:k8s-node-health/node-conditions` for the node.
- Mounting pods crash-looping or restarting after a full disk:
  `kubernetes:k8s-workload-health/pod-restarts`, then
  `kubernetes:k8s-workload-logs/recent-logs`.
- Many claims or pods in the namespace affected:
  `kubernetes:k8s-namespace-health/unhealthy-pods`.
- Freeing a claim or moving a pod needs a write; use
  `kubernetes:k8s-workload-ops/delete-pods` or `rollout-restart` on the owner.
