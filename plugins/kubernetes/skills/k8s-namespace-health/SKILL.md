---
name: k8s-namespace-health
description: >-
  Health facts for a whole Kubernetes namespace: Warning events grouped by
  object and reason with the owning workload, pods that are Pending (with the
  scheduler's reason), Failed, not Ready, waiting (ImagePullBackOff,
  CrashLoopBackOff) or restarting, failed and long-running Jobs, ResourceQuota
  usage against its limits, and multi-replica Deployments and StatefulSets with
  no PodDisruptionBudget or a budget that allows no disruptions. Use when a
  namespace is degraded, several workloads in it fail at once, an alert names
  the namespace, or you need a first sweep before looking at one workload.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/namespace]
  tags: [health, namespace, pods, events, quota, jobs]
---

## When to use

The target is a Namespace and the question is "what is wrong in here?": more
than one workload affected, an alert on the namespace, a quota or scheduling
problem, a failed batch Job. Run `unhealthy-pods` and `warning-events` first;
they say which workloads are involved. For one named workload go straight to
`kubernetes:k8s-workload-health/rollout-status`.

All tasks only read, inside the namespace. Every task also reads the Namespace
object itself (`get` on `namespaces`, a cluster-scoped permission) so that a
deleted namespace fails with `not_found` instead of listing as empty. An identity
without that permission still gets results, with a `permission_denied` error
scoped to `namespaces` (a partial result): in that case a namespace that no longer
exists would show as empty lists. The permissions on each task list the rest. No
task reads Secrets, ConfigMaps or logs.

## What each task tells you

- `warning-events`: Warning events last seen within `since`, grouped by
  involved object (`kind`, `name`, `uid`) and `reason`, with `count`,
  `firstSeen`, `lastSeen` and the latest message. For a Pod, ReplicaSet or Job,
  `owner` is the workload that owns it (Deployment, StatefulSet, DaemonSet, Job
  or CronJob) and `objectExists` says whether that object is still there; an
  event about a deleted pod is kept with `owner: null`. `resolved: false` on an
  owner means the chain stopped at a ReplicaSet or Job that is gone.
- `unhealthy-pods`: current state, whenever the pod got there (no look-back).
  `states` holds any of `pending` (with `pending.reason` such as
  `Unschedulable` when unscheduled), `failed` (reason, e.g. `Evicted`, and
  exit codes), `notReady` (Running, Ready condition not True), `waiting`
  (container waiting reasons; init containers have `init: true`) and `restarts`
  (a container restarted at least `restartThreshold` times, with how the last
  run ended). `counts` has the totals before the `maxItems` cap.
- `failed-jobs`: Jobs with a Failed condition (`failureReason` such as
  `BackoffLimitExceeded` or `DeadlineExceeded`) and active Jobs running longer
  than `jobActiveMaxMinutes` (`longRunning`). `cronJob` names the owning CronJob.
  A failed Job is listed until it is deleted, so an old failure of a CronJob that
  has since succeeded still shows: compare `failedAt` with the CronJob's runs.
- `resource-quotas`: each resource of each ResourceQuota with `used`, `hard`,
  `ratio` and `atOrAbove` (ratio at or above `quotaRatio`). A `ratio` of null
  with a hard limit of 0 means nothing can be created; null `used` means the
  quota controller has not computed usage yet.
- `pdb-coverage`: `uncovered` lists Deployments and StatefulSets with more than
  one replica that no PodDisruptionBudget selects through their pod template
  labels; `pdbs` lists budgets whose `disruptionsAllowed` is 0 with
  `currentHealthy` against `desiredHealthy` and the workloads they select.
  `inspected` says how many objects were looked at.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- An empty list is only meaningful next to its totals: `unhealthy-pods` with
  `counts.total` of 0, or `pdb-coverage` with `inspected.deployments` of 0, means
  there was nothing to look at. An empty `resource-quotas` means the namespace has
  no ResourceQuota. An empty `warning-events` means no Warning event in the
  window, which is not proof of calm because of the event TTL below.
- Kubernetes keeps events for about an hour by default, so a `since` longer than
  that cannot reach further back than the cluster retained them; an old incident
  may have no events at all.
- Lists stop at `maxItems` (default 200), nested lists at 50 and message text at
  1024 characters; `truncated` is true on any entry that was cut, and
  `counts` (before the cap) tells you how many were left out.
- A task that could not look (forbidden, namespace not found, API unreachable)
  returns an error, not an empty list. A partial result, with an error scoped to
  `pods`, `replicasets.apps`, `jobs.batch`, `deployments.apps` or
  `statefulsets.apps`, keeps its main facts but lacks the enrichment from that
  list (owners become null; `inspected` reports null for a kind not read).
- States that look like failures but are usually normal: a Pending pod with
  `scheduled: true` and `ContainerCreating` for a few seconds; `Terminating` pods
  (`terminating: true`); Failed pods of a Job still inside its backoff limit
  (the Job is not in `failed-jobs` until it gives up); a StatefulSet pod waiting
  for its volume; an active Job that is simply a long batch run.
- `restartThreshold` defaults to 3 (the classic bundle's default), unlike
  `kubernetes:k8s-workload-health/pod-restarts`, which defaults to 1.

## What to try next

- A workload named in `owner`: `kubernetes:k8s-workload-health/rollout-status`,
  `kubernetes:k8s-workload-health/pod-restarts` and
  `kubernetes:k8s-workload-logs/recent-logs` for what the container printed.
- Pending with `Unschedulable`, or events naming nodes:
  `kubernetes:k8s-node-health/node-conditions`.
- A pod waiting on a volume or a Pending claim:
  `kubernetes:k8s-pvc-health/binding-status`.
- A failed Job owned by a CronJob: `kubernetes:k8s-cronjob-health` (schedule,
  suspension, last runs).
- A quota at its limit: look at which workloads hold the usage with
  `unhealthy-pods` and the workload skills; raising a quota is a change this skill
  does not make.
