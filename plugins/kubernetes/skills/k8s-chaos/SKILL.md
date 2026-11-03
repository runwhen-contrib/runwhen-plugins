---
name: k8s-chaos
description: >-
  Chaos actions on a Kubernetes namespace, Deployment or StatefulSet, to test
  that it survives failure: delete random pods and see whether the ready
  replicas come back and how fast, OOM-kill a container by allocating past its
  memory limit, break a Service's selector or port for a while and restore it,
  and fill a pod's /tmp. Every task is destructive and supports a dry run that
  shows what would be done. A workload target acts on that workload's pods
  only; a namespace target picks from the running pods of the namespace. Each
  result reports what was done and what was observed afterwards.
runwhen:
  appliesTo:
    types: [kubernetes/namespace, kubernetes/deployment, kubernetes/statefulset]
  tags: [chaos, resilience, high-availability, destructive]
---

## When to use

You want to prove that a workload recovers: that a replica set refills after a pod is
killed, that a memory limit is enforced and the container restarts, that traffic
survives a broken Service, that a full `/tmp` does not take the pod down unnoticed. These
are experiments, not fixes: for fixes use `kubernetes:k8s-workload-ops/delete-pods`,
`rollout-restart` and `set-resources`. To suspend GitOps before an experiment (it would
heal the damage) use `kubernetes:k8s-flux-health/suspend`.

Every task writes (`readOnly: false`, `destructive: true`) and needs approval. Run it as a
dry run first: the result names the pods or Services that would be hit and changes nothing.
A workload target acts on that workload's pods only (found by its selector and confirmed
through ownerReferences). A namespace target picks from the running pods a controller owns,
never mirror pods, DaemonSet pods or the pod the task runs in; `includeUncontrolled: true` adds
pods nothing recreates. The namespaces kube-system, kube-public and kube-node-lease are always
refused (`invalid_input`, no override), and so is the Service `kubernetes` in `default`. Reading the Namespace object (`namespaces get`) is
cluster-scoped; if it is forbidden the result is partial with a `permission_denied` error
scoped `namespaces` and the task goes on with namespace-scoped reads.

## What each task tells you

Every task returns `target`: scope, kind, name, namespace, the workload's `desiredReplicas`
and `readyReplicas` now, and `gitopsTool` when Flux or Argo CD manages it.

Pods are picked at random but recorded: `seed` (the `seed` input, else the target's path plus
the UTC date) and a hash of seed and pod name decide, and every result reports the seed used.
A dry run and the real run approved from it on the same day therefore pick the same pods; pass
the dry run's `seed` explicitly when the real run may happen on another day, or when it matters.
`oomkill-pod` and `fill-tmp` dry runs also check with `kubectl auth can-i` that exec is allowed
(`permission_denied` when not), since a dry run runs no exec.

- `delete-pods`: `count` (1 to 10) pods deleted. `deleted` names them (`wouldDelete` on a dry
  run), `candidateCount` is how many were running, `deletesAllReady` says the ready pods were all
  hit. For a workload target `recovery`: `recovered` (ready replicas back at `desiredReplicas`,
  none of them a deleted pod), `recoveryMs` (milliseconds from the delete; null when it did not
  recover within `timeout`), `readyReplicasBefore` and `readyReplicasAfter`. `recovered: false` is
  a finding about the workload, not a task failure.
- `oomkill-pod`: one dd process allocates the memory limit plus a quarter in the picked
  container. Reports `exitCode` of that process (137 = killed), `restartDelta`, `lastState`
  (reason, exit code, finishedAt) and `oomKilled` (restart count rose AND lastState is
  OOMKilled from a newer termination). `oomKilled: false` after a run means the limit did not
  hold or the kill was not seen within `timeout`. `execKilled: true` is dd exiting 137 with no
  container OOM: the limit held, but the kernel killed only the process (cgroup v1, or no
  `memory.oom.group`), so nothing restarted and the container is not proven to die on OOM. No
  memory limit anywhere is `invalid_input`, and so is a limit above `maxMemoryLimitMi` (1536);
  no sh or dd in the image is `dependency`; neither reports a pass.
- `mangle-service`: per Service, `change` (the field, old value, new value), ready endpoints
  `before`, `during` and `afterRestore`, `endpointsDropped`, `heldMs`, and `restore`
  (`verified: true` means a read afterwards found the original selector and ports). Mode `selector`
  makes the Service select no pod, so endpoints fall to 0. Mode `port` changes the Service port
  and keeps the pods: endpoints stay, clients on the old port fail (`endpointsDropped: false` is
  normal). A workload target mangles every Service that selects its pods (at most 5; the `service`
  input narrows it); a namespace target needs `service`. A Service that also selects pods the
  workload does not own is refused (`invalid_input`, `details.sharedWith`) unless `allowShared`;
  then `sharedWith` names the other pods it cut off. `recover: true` mangles nothing: it restores
  every target Service still carrying the annotation of a run that died, from that annotation,
  and verifies and removes it (`serviceCount: 0` means nothing was stuck); in a dry run it sends
  the restore patch server-side.
- `fill-tmp`: `bytesWritten` (size of the file afterwards), `writeExitCode`/`writeError` from dd,
  `evicted`, `restarted` and `podPhase` after `observe` seconds, and `cleanup`
  (`succeeded` only when rm ran and the file was confirmed gone). An evicted pod's `/tmp` went with
  it: `cleanup.reason` is `pod_evicted`. A restarting container is retried for 30s; if rm never
  got in the reason is `container_restarting` with a `cleanup` error and a hint to remove the file
  by hand. `pod_gone` means reading the pod said not found; `pod_terminating` a pod being deleted. A low memory limit can make the write itself OOM-kill the
  container (page cache is charged to it): `restarted: true`, `writeExitCode` 137.

## How to read the result

A dry run has `applied: false` and empty "done" fields. A failure (forbidden, target missing,
no sh in the container) is an error on the result and never an empty success; a target with no
running pod is `not_found`. A restore is tried again after 1, 3 and 6s and touches only fields that still hold the mangled
value. One that failed is `internal`: the message names the Service and the annotation
`runwhen.com/chaos-original-spec` (kept while mangled), `details.services[].originalSpec` holds
the spec: rerun with `recover: true` or put it back by hand. `concurrentChange: true` means someone
else changed a mangled field meanwhile: it was left alone and the annotation kept. A mangle patch
that lost a race with another writer aborts as `internal` ("concurrent change") with nothing changed. A cleanup that failed adds an error scoped `cleanup` naming the file. Lists stop at
`maxItems`; counts do not.

Normal states that look like failure: a StatefulSet pod comes back with the same name (compare
`recovery` times, not names); a Deployment may show more ready pods than desired while it surges;
a `recovered: false` on a one-replica workload usually means a slow start, not a broken one;
`oomKilled` can lag the kill by the kubelet's sync.

If the task process is killed (executor timeout, node loss) a mangled Service stays mangled: look
for the annotation above. A new run refuses it and says to rerun with `recover: true`.

## What to try next

- Recovery too slow or never: `kubernetes:k8s-workload-health/rollout-status`, `pod-status` and
  `probe-config` (readiness gates the ready count), `kubernetes:k8s-workload-health/hpa-status`.
- Container did not restart after the OOM: `kubernetes:k8s-namespace-resources/resource-settings` for
  missing limits, then `kubernetes:k8s-workload-ops/set-resources`.
- Traffic did not recover after a mangle: `kubernetes:k8s-ingress-health/backends`.
- Pod evicted or node under disk pressure: `kubernetes:k8s-node-health/node-conditions`.
- The damage keeps coming back: a GitOps controller is healing it; see `target.gitopsTool` and
  `kubernetes:k8s-flux-health/ready-status`.
