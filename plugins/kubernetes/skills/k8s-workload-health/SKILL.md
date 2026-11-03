---
name: k8s-workload-health
description: >-
  Health facts for a Kubernetes Deployment, StatefulSet or DaemonSet: rollout
  progress and replica counts, container restarts with their termination reasons
  (OOMKilled, exit codes), pod status (ImagePullBackOff, CrashLoopBackOff,
  unschedulable), Warning events, probe configuration, recent image, env and
  resource changes, and autoscaler state. Use when a workload is degraded, not
  ready, crash-looping or stuck mid-rollout, or an alert names one.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset, kubernetes/daemonset]
  tags: [health, availability, rollout, restarts]
---

## When to use

The target is a Deployment, StatefulSet or DaemonSet that is not serving as
expected: fewer ready pods than desired, pods restarting, a rollout that does
not finish, or an alert that names the workload. Run `rollout-status` first for
the shape of the problem, then the task that matches the symptom.

All tasks only read, inside the target's own namespace. Every task needs `get`
on the workload; the permissions on each task list the rest. No task reads
Secrets, ConfigMaps or logs, and none needs cluster-wide access.

## What each task tells you

- `rollout-status`: desired, ready, updated, available and unavailable counts;
  the workload's conditions (a Deployment with `progressDeadlineExceeded` has
  stopped progressing); whether the controller has seen the latest spec
  (`generationObserved`); update strategy; and the revision history with images.
- `pod-restarts`: containers that restarted, with the reason and exit code of
  the last termination (`OOMKilled`, `Error`, exit 137, 143), whether it ended
  within `since`, and `exceedsThreshold` against the `restartThreshold` setting.
  `inspected.pods` is how many pods were examined: an empty `restarts` with
  `inspected.pods` of 0 means there were no pods to look at, not that none restarted.
- `pod-status`: per pod, phase, ready, containers ready out of total, node, and
  why containers wait (`ImagePullBackOff`, `CrashLoopBackOff`,
  `PodInitializing`) or why a pod is unscheduled (`unschedulable.reason`). Init
  containers appear in `waiting`/`terminated` with `init: true` and are counted
  apart (`initContainersReady`/`initContainersTotal`); a completed init container
  shows as terminated `Completed`. `counts` holds the totals before the `maxItems` cap.
- `warning-events`: Warning events about the workload, its ReplicaSets or
  ControllerRevisions, its current pods and their PVCs, grouped by object and
  reason with `count`, `firstSeen`, `lastSeen` and the latest message. Events
  about pods that were already replaced, or about an earlier pod with the same
  name (matched by uid), are not included. `count` is the events' own lifetime
  counter, not a count inside the window.
- `probe-config`: the three probes of each container, their handler, port and
  timings. `portDeclared: false` means the probe port is not in the container's
  `ports` list; a probe can still work on an undeclared port, so treat it as a
  lead. A readiness probe on a port nothing listens on leaves a pod Running but
  never Ready. Native sidecars are listed with `init: true`. Exec probes show
  only the program and `argCount`, never the arguments.
- `config-changes`: revisions created within `since`, each against the one
  before: image changes, env var names added, removed or changed (values are
  never read), resource changes, containers added or removed, and
  `otherPodSpecChanged` (names of any other differing pod-spec field or template
  annotation, such as `annotations.kubectl.kubernetes.io/restartedAt` after a
  `rollout restart`). `current` marks the highest revision. A Deployment
  rollback re-uses an old ReplicaSet (`reused`, with its `earlierRevisions`); it
  is listed when the Deployment's Progressing condition changed within `since`
  (`windowBasis: progressing`). Scaling creates no revision. StatefulSet and
  DaemonSet revisions hold only `spec.template`: a `volumeClaimTemplates` change
  is invisible here, and a rollback to an existing ControllerRevision has no
  timestamp so it is not listed.
- `hpa-status`: autoscalers targeting this workload, with bounds, current and
  desired replicas, `atMax`/`atMin`, conditions and metrics.
  `resourceMetricWithoutRequests` means a Utilization metric points at a
  container with no request, so the autoscaler cannot compute it; a LimitRange
  that defaults requests can make this a false positive. With no metrics the
  autoscaler uses the controller default, reported as `defaultCpuTarget: 80`.
  An empty list means no autoscaler targets the workload.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy. Compare
them: `ready < desired` with `pod-status` showing waiting reasons tells you why;
`updated < desired` with `progressDeadlineExceeded` and a recent entry in
`config-changes` points at the revision that broke the rollout.

A failure to look (forbidden, target not found, API server unreachable) is an
error on the result, never an empty list. A result with errors is not a clean
bill of health for the outputs it did return (`rollout-status` keeps the
rollout facts and reports a failed revision list as an error scoped to
`revisions`). `restartThreshold` (default 1) sets `exceedsThreshold`.

Things that are normal, not failures:

- StatefulSet with a `partition`, or an `OnDelete` strategy: `updated < desired`
  is expected until pods are updated or deleted.
- DaemonSet `desired` is `desiredNumberScheduled`: 0 means no node matches its
  selector. `misscheduled` counts pods running where they should not.
- A workload scaled to 0 has `desired: 0` and no pods; that is not a failure.
- An empty `pods` list means no pod matched the selector and ownership, not that
  all are healthy. Check `desired` and `counts.total`.
- Events expire after the cluster's event TTL (default 1h): no Warning events
  does not mean none happened earlier.
- A list with exactly `maxItems` entries (default 200) was cut. Nested lists are
  cut at 50 and text at 1024 characters, flagged `truncated: true`.

## What to try next

- Pods restarting or crash-looping: `kubernetes:k8s-workload-logs/error-patterns`
  and `kubernetes:k8s-workload-logs/recent-logs` for what the container printed,
  including the previous run.
- A rollout that never finished or a bad revision:
  `kubernetes:k8s-workload-ops/rollback` or `kubernetes:k8s-workload-ops/rollout-restart`
  (both write).
- Pending or Unschedulable pods, or a PVC named in the events:
  `kubernetes:k8s-pvc-health/binding-status`, `kubernetes:k8s-node-health/node-conditions`.
- Many workloads in the namespace affected: `kubernetes:k8s-namespace-health/unhealthy-pods`.
