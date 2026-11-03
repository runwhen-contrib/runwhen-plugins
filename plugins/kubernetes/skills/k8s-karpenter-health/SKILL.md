---
name: k8s-karpenter-health
description: >-
  Health facts for a Karpenter NodePool: its Ready and other conditions with
  reasons, spec.limits against the resources it has used (cpu, memory, nodes) with
  percent and whether it is at its limit, disruption policy and budgets, and the
  NodeClass it refers to (EC2NodeClass, AKSNodeClass, GCENodeClass) with whether
  that exists and is Ready; the pool's NodeClaims with phase, age and which are
  stuck before Ready; and the Pending pods that cannot schedule and that this pool
  could serve, with the scheduler's and Karpenter's events on them. Use when
  workloads stay Pending, nodes are not being provisioned, a NodePool is not
  Ready, or a pool may have hit its limits.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/nodepool]
  tags: [health, karpenter, autoscaling, nodepool, capacity]
---

## When to use

The target is a Karpenter `NodePool` (`karpenter.sh`, cluster-scoped) and pods are
Pending, a new node never joins, or the pool reports Not Ready. Start with
`nodepool-status` (is the pool Ready, is it at a limit, is its NodeClass Ready), then
`nodeclaims` (did the nodes it asked for come up) and `pending-pods` (what is waiting
for capacity).

Every read is **cluster-scoped**: NodePools, NodeClaims and NodeClasses need a
ClusterRole, and `pending-pods` lists Pending pods and pod events in all namespaces.
`nodepool-status` still returns the pool facts when only the NodeClass read is
forbidden (`nodeClass.exists` and `ready` are `null`, with a `permission_denied`
error scoped `nodeclasses`); `pending-pods` keeps the pods when only events are
forbidden (`events: null`, scope `events`). The other reads fail the task instead.

`pending-pods` reaches beyond the pool's own objects (D47): a Pending pod is not owned
by the pool, but the pool should have made room for it, so the pods that select it
(and the unconstrained ones it could serve) are its direct concern.

**Controller health is not a task here.** The Karpenter controller (a Deployment in
the `karpenter` or `kube-system` namespace) and its logs are an ordinary workload: use
`kubernetes:k8s-workload-health/pod-status` and
`kubernetes:k8s-workload-logs/error-patterns` on that Deployment. The classic webhook
checks (Karpenter webhook configurations, caBundle, the webhook Service) are not ported
and no skill covers them (`kubernetes:k8s-ingress-health` has no such task): a gap. A
failing admission webhook shows only in the controller's pod status and logs.

## What each task tells you

- `nodepool-status`: `pool.ready` and `pool.conditions` are the status conditions
  (Ready, ValidationSucceeded, NodeClassReady, ... status, reason, message);
  `notTrueConditions` lists the types that are not True. `weight`,
  `consolidationPolicy`, `consolidateAfter` and `expireAfter` come from the spec;
  `budgets` are the disruption budgets as set (with none Karpenter applies its
  default of 10%). `allowsNone` is true for a `0` or `0%` budget with no schedule;
  with a schedule it is `allowsNoneDuringSchedule` instead (it blocks only inside
  the schedule window). Read `reasons` too: a zero budget with reasons blocks only
  those disruption reasons. `limits.items` has
  one entry per resource in `spec.limits` or `status.resources`: `limit` and
  `used` in base units (cores, bytes, a count), `percent`, `atOrAbove` against the
  `limitWarningPercent` setting (default 90) and `exhausted` (used at or above the
  limit). `nodeClass` names the referenced NodeClass (`ref`), whether it `exists`
  and is `ready`, with its Ready condition.
- `nodeclaims`: the NodeClaims labelled `karpenter.sh/nodepool=<pool>` (and owned by
  this pool when they name an owner). `phase` is Launching, Registering,
  Initializing, NotReady, Ready or Deleting, from the Launched, Registered,
  Initialized and Ready conditions. `notReadySeconds` is how long it has been not
  Ready: from creation for a claim that never got through those steps, from the Ready
  condition's lastTransitionTime for one that did and then went not Ready (so an old
  claim that failed a minute ago is not stuck); null when Ready or Deleting. `stuck`
  means `notReadySeconds` is over the `stuckMinutes` setting (default 15). Each claim also has
  `nodeName`, `capacityType`, `instanceType`, `zone`, `drifted`, `empty` and
  `consolidatable` (true only when that condition is True) and the conditions.
  Stuck claims come first. `counts` cover every claim, including those past the cap.
- `pending-pods`: Pending pods in any namespace whose PodScheduled condition is False
  with reason Unschedulable and whose `karpenter.sh/nodepool` constraint allows
  this pool. `constraint` says how: `nodeSelector` or `nodeAffinity` (an `In`
  requirement in every required term) select the pool; `nodeAffinityOther` (NotIn,
  Exists, DoesNotExist) does not, it only does not exclude it; `none` would take any
  pool. Pods that select this pool come first, then the others, oldest first. Each has
  the scheduler's `message`, `requests` (the scheduler's effective cpu and memory,
  sidecars and init containers included), the age and `owner`. DaemonSet-owned pods and
  pods pinned to a node by name (matchFields `metadata.name`) are never listed: Karpenter
  cannot make a node for them; they are counted as `daemonSetPods` and `pinnedToNode`.
  `counts` has the Pending total, how many are unschedulable, and `constrainedToPool`
  (select the pool), `otherConstraint`, `unconstrained` and `selectOtherPool`.
  `events` are the events on those pods inside `since` (default 1h) with reason
  FailedScheduling (the scheduler's and Karpenter's), Nominated or FailedProvisioning,
  grouped per pod and reason, with counts and last seen. Other events on the pods
  (image pulls, probes) are not read, and only the pods listed (up to `maxItems`) get
  events: a pod past the cap has none here.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `nodeClaims: []` means the pool owns no NodeClaim: it has not
  provisioned anything, or everything was consolidated away. `pods: []` means no
  Unschedulable Pending pod could use this pool; check `counts.pending` for Pending
  pods that are not Unschedulable (image pulls, volumes). `events: []` means no
  event was last seen inside `since`; events expire after about an hour (default
  event TTL), so a pod pending for longer shows none even though it is still stuck.
  `events: null` is different: not read, because no pod was listed or listing events
  was forbidden (a `permission_denied` error scoped `events` says which).
- **`null` is unknown.** `nodeClass.exists`/`ready` are `null` when the NodeClass was
  not looked up (`lookedUp: false`): either the reference is to a group or kind this
  skill does not read (for example KWOK), with no error, or the read was forbidden,
  with a `permission_denied` error. `exists: false` with `lookedUp: true` is a real
  finding: the object, or its whole type (`typeInstalled: false`), is not on the
  cluster. `limits.items[].used` is `null` before the controller has reported
  usage, `limit` is `null` for a resource Karpenter reports but the pool does not
  limit. `phase`/condition views are `null` for a condition the controller has not
  set. A result with errors is not a clean bill of health for the outputs it did
  return.
- **Stale Ready.** Compare `ready.observedGeneration` with `generation` (pool): a
  condition observed at an older generation describes the pool before its last edit.
- **Cuts.** Lists stop at `maxItems` (default 200); lists inside an entry stop at 50.
  Messages are cut at 1024 characters and flagged `truncated`. `counts` are taken
  before the cut. `since` takes `ms|s|m|h|d` combinations; anything else is an
  `invalid_input` error.
- **"Could serve" is the pool label only.** `pending-pods` does not evaluate taints,
  tolerations, zone, instance-type or resource constraints: a pod listed as
  unconstrained may still be impossible for this pool. Read the `message` and the
  pool's requirements.
- **Normal states that look like failures.** A pool near 100% of a limit with
  pods Pending is the usual way "pool is full" looks: look at `exhausted`. A NodeClaim
  that is Launching or Registering for a few minutes is just starting (it is only
  `stuck` after `stuckMinutes`). A `Drifted` or `Consolidatable` claim is waiting for
  a disruption, not failing. A pod that was just created is Pending for seconds
  before Karpenter nominates a node (a `Nominated` event). NodeClaims with a
  `deletionTimestamp` are Deleting, not stuck. Pods the scheduler has not yet looked
  at have no PodScheduled condition and are not listed.

## What to try next

- The pool is Ready but a pod stays Pending: the pod's `message` and
  `kubernetes:k8s-workload-health/pod-status` for its workload; compare `requests`
  with the pool's `limits` and requirements.
- The pool is at a limit (`exhausted`): raise `spec.limits` (a write, not in this
  skill), or find what holds the capacity with `kubernetes:k8s-node-health/overcommit`.
- The NodeClass is not Ready or missing: its Ready condition says why (subnets, AMI,
  role); fix it with the cloud provider tooling.
- A NodeClaim is stuck Registering or Initializing: the node booted or failed to join.
  `kubernetes:k8s-node-health/node-conditions` shows whether the node exists, and
  `kubernetes:k8s-workload-logs/recent-logs` on the Karpenter controller shows why
  the launch failed.
- Nothing provisions at all: check the controller itself with
  `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/error-patterns` on the `karpenter` Deployment.
