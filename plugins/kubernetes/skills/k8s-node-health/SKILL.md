---
name: k8s-node-health
description: >-
  Health facts for Kubernetes nodes, for one node or every node of a cluster:
  Ready status with reason and transition time, memory, disk and PID pressure,
  cordoned and tainted nodes, kubelet version, nodes that went NotReady, rebooted
  or re-registered within a window, CPU and memory usage against allocatable
  from metrics.k8s.io, and how far the pods' summed requests and limits
  overcommit each node. Use when a node is NotReady, pods are Pending or
  evicted, a node was replaced or rebooted, or a cluster looks short of capacity.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/cluster, kubernetes/node]
  tags: [health, nodes, capacity, utilization]
---

## When to use

The target is a node, or a cluster whose nodes you want to look over: a node is
NotReady or cordoned, pods are unschedulable or evicted, a node came and went
(autoscaler, spot reclaim, reboot), or usage is high. A node target covers that
node (`ctx.resource.name`); a cluster target covers every node. Start with
`node-conditions`, then the task that matches the symptom.

Every read is **cluster-scoped**: `nodes`, `events` in the `default` namespace,
`pods` in all namespaces and the `metrics.k8s.io` NodeMetrics API. The
permissions on each task list what it needs (events are only read in `default`,
but RBAC rules here carry no namespace, so that rule is declared unscoped). A namespace-scoped connection is
refused (`permission_denied`) and the task fails, because there is nothing
useful left to report. The one exception is node events in `node-restarts`
(see below). No task reads Secrets, ConfigMaps or logs.

## What each task tells you

- `node-conditions`: per node, the `Ready` condition (status, reason,
  `lastTransition`), `MemoryPressure`, `DiskPressure`, `PIDPressure` and
  `NetworkUnavailable` only when their status is not `False`, any other
  condition type (node-problem-detector, cloud providers) whose status is not
  `False` as `otherConditions` (most are healthy when `False`, a few are not:
  read the type), `unschedulable` (cordoned), taints as key and effect,
  `capacity` (`on-demand`, `spot` or `preemptible` from the Karpenter, EKS, GKE
  and AKS labels, null when none is set), kubelet version and age. Order: not
  Ready, then with conditions, then cordoned, then name, so the `maxItems` cut
  keeps the nodes worth reading. `counts` is taken before the cut.
- `node-restarts`: within `since`, `transitions` lists nodes whose Ready
  condition changed state (its last transition time is inside the window), and
  `events.nodes` groups the node events `NodeNotReady`, `NodeReady`, `Rebooted`,
  `RegisteredNode`, `Starting` (from the kubelet only; kube-proxy emits
  `Starting` too) and the leaving reasons `RemovingNode`, `DeletingNode`,
  `NodeShutdown`, `TerminatingNode`, `Removed`, `Shutdown*`, `Preempt*` and
  `Termination*` per node with `count`, `firstSeen` and `lastSeen`, so a node
  that came and went shows. `events.eventsExamined` and `events.oldestEventSeen`
  say how much event history the API still held. `count`
  adds the events' own counters, it is not a count inside the window. Node events
  live in the `default` namespace and are matched by node name and kind.
- `node-utilization`: usage from the `metrics.k8s.io` NodeMetrics API against the
  node's allocatable, `percent` and `atOrAbove` for CPU and memory, with
  `atOrAbove` against the `utilizationPercent` setting (default 90). Usage is a
  point-in-time sample over `window`. A node with no metrics entry has null
  usage and null `atOrAbove`: unknown, not idle.
- `overcommit`: per node, the sum of requests and limits of its pods for CPU and
  memory against allocatable, with percent. Init containers follow the effective
  rule (the larger of the app containers plus native sidecars and the peak while
  init runs); pod-level `spec.resources` (1.34+) replaces the container sum when
  set; pod overhead is added to requests, and to limits only when there is a
  limit. Succeeded/Failed pods are not listed. `topPodsByLimits` and
  `topPodsByRequests` are the five largest pods per node (namespace, name, CPU
  and memory with percent of allocatable). `largestPodLimitPercent` is the
  biggest single pod's limit share, and `podAboveLimitPercent` is true when a
  pod's limits pass the `podLimitPercent` setting (default 90) of the node.
  `limitsAboveAllocatable` is true when the limits add up to more than the node
  can hold (CPU or memory); `requestsAboveAllocatable` is the same for requests.
  `containersWithoutLimits` counts long-running containers that set none, so the
  sums understate for them. One pod whose limits alone exceed the node
  also makes the sum exceed it.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `transitions: []` means no Ready condition changed inside
  `since`. `events: []` means no restart-related node event was last seen inside
  `since`. Node events expire after about an hour (event TTL, default 1h), so an
  older restart can leave nothing: a window longer than an hour is capped by what
  the API server still holds (compare `oldestEventSeen` with the window). `nodes: []` means the cluster has no nodes, not a
  failed read.
- **`null` is unknown.** `events: null` in `node-restarts` means the events could
  not be read (forbidden in `default`); a `permission_denied` error scoped `events`
  says so and the `transitions` are still good. A null `percent` or `atOrAbove`
  means usage or allocatable was not available.
- **Cuts.** Lists stop at `maxItems` (default 200); lists inside an entry
  (conditions, taints, reasons) stop at 50. Messages are cut at 1024 characters
  and flagged `truncated`. `since` takes `ms|s|m|h|d` combinations (default `1h`);
  anything else is an `invalid_input` error.
- **Metrics API.** `node-utilization` fails with `dependency`
  (`metrics.k8s.io not available`) when no metrics-server (or equivalent) is
  installed, as on a plain kind cluster. It never returns an empty list for that.
- **Fast reboots.** A node that reboots within the node-monitor grace period
  (about 40s to a few minutes) never flips Ready: there is no transition. Only
  its kubelet events (`Rebooted`, `Starting`) show it, and only while they last.
- **Normal states that look like failures.** Control-plane nodes carry a
  `node-role.kubernetes.io/control-plane:NoSchedule` taint (kind removes it on a
  single-node cluster). A freshly created node has a Ready `lastTransition` inside
  the window and a `RegisteredNode` event. A node-controller restart produces
  only `RegisteredNode` events for every node, no Ready transition. Limits above
  100% of allocatable are normal: limits are not reserved, only requests are, so
  `limitsAboveAllocatable` alone is not a fault (`requestsAboveAllocatable` is
  rare). A node
  with `NetworkUnavailable` absent is normal; most clusters never set it.

## What to try next

- The classic "pods causing high node utilization" task read files nothing wrote
  and is not ported: use `topPodsByRequests`/`topPodsByLimits` here, then
  `kubernetes:k8s-workload-health/pod-status` for those pods.
- A node is NotReady, cordoned or full: `kubernetes:k8s-workload-health/pod-status`
  on the affected workloads shows pods stuck Pending or evicted.
- Pods restarting around a node event: `kubernetes:k8s-workload-health/pod-restarts`.
- Many pods across namespaces affected:
  `kubernetes:k8s-namespace-health/unhealthy-pods`.
- Volumes that will not attach to a node: `kubernetes:k8s-pvc-health/attachment`.
- Draining or cordoning a node, or moving pods, needs a write; use
  `kubernetes:k8s-workload-ops/delete-pods` or `rollout-restart` on the owner.
