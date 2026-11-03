---
name: k8s-namespace-resources
description: >-
  Resource and image facts for a Kubernetes namespace: containers of running pods
  and workload templates without CPU or memory requests or limits, current usage
  against limits from metrics.k8s.io, VerticalPodAutoscaler recommendations
  against current requests, every container's image with tag, digest and pull
  policy plus pods stuck in ImagePullBackOff, and what a ServiceAccount may do
  in the namespace (LocalSubjectAccessReview). Use when pods are OOMKilled or
  throttled, a namespace is hard to size or schedule, an image will not pull,
  or a workload's ServiceAccount seems to lack access.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/namespace]
  tags: [health, resources, requests, limits, images, rbac]
---

## When to use

The target is a namespace and the question is about how its containers are sized,
what they run, or what their ServiceAccount may do. For pods that are unhealthy
right now use `kubernetes:k8s-namespace-health/unhealthy-pods` first; come here
when the cause looks like sizing, an image or access.

Every task reads the Namespace (`get namespaces`) so a deleted namespace is
`not_found` and not an empty list. If that single read is forbidden the task goes
on and reports a `permission_denied` error scoped `namespaces`. The other reads
are lists inside the namespace. `resource-usage` needs the `metrics.k8s.io` API, and
`serviceaccount-permissions` needs `create` on `localsubjectaccessreviews.authorization.k8s.io`
in the namespace, which a namespace admin has and a view role does not. That create
persists nothing; no task writes, reads Secrets or logs, or creates a pod.

## What each task tells you

- `resource-settings`: containers of Running pods (`pods`) and of Deployment,
  StatefulSet and DaemonSet pod templates (`workloads`) with at least one of
  `cpuRequest`, `cpuLimit`, `memoryRequest`, `memoryLimit` unset, the four values
  as the API holds them, and `missing` naming the unset ones. `workloads` is the
  pod template: a template with a limit and no request counts the limit as the
  request (the API server does the same when it creates the pod). LimitRange
  defaults are applied to pods at admission and not to templates, so a pod can
  have a value its template lacks. Init containers and
  native sidecars are listed with `init: true`. A pod's `owner` is the workload
  above it (Deployment through its ReplicaSet, CronJob through its Job); `resolved:
  false` means the chain stopped at a ReplicaSet or Job that is gone or could not
  be listed. `inspected` counts what was looked at.
- `resource-usage`: per container and resource, `usage`, `request`, `limit`
  (CPU in millicores, memory in bytes), `percentOfLimit`, `percentOfRequest`, the
  sample's `timestamp` and `window`, and `atOrAbove`, true when usage is at or above the `utilizationPercent` setting
  (default 90) of the limit. Highest use of the limit first. `atOrAbove` is null
  when there is no limit; use above a request alone is normal bursting.
- `vpa-recommendations`: each VerticalPodAutoscaler with its target workload,
  `updateMode` (null means the VPA default, Auto), its status `conditions`
  (`RecommendationProvided`, `LowConfidence`, `NoPodsMatched`, ...), and per
  container the recommended `target` with `lowerBound` and `upperBound` beside
  `current`, the requests in the workload's pod template, and `currentLive`, the
  requests of its live pods (pod count and distinct values; null when the pods
  could not be read). Under `Auto`, `Recreate` or `Initial` the VPA changes pods
  and not the template, so live pods can differ from `current`; `Off` changes
  nothing. `belowLowerBound` and `aboveUpperBound` say whether `current` is
  outside the recommender's bounds for CPU or memory. `cpuDifferencePercent` and
  `memoryDifferencePercent`
  (positive means the recommendation is higher), and `differs`: true when either is
  at least `vpaDifferencePercent` (default 20) away, false when all are within it,
  null when nothing is comparable (no request set, or no recommendation yet).
  `inspected.vpas` is the count before the `maxItems` cut.
- `images`: every container's `image` as written, `repository`, `tag`, `digest`,
  `pullPolicy` and `imageID` (null until pulled), pods of every phase. `tag` and
  `digest` null together means an untagged image, which the runtime pulls as
  `latest`. `pullFailures` lists containers waiting in `ImagePullBackOff`,
  `ErrImagePull`, `ErrImageNeverPull` or `InvalidImageName` with the kubelet's
  message (the registry's answer: not found, unauthorized, no such host).
- `serviceaccount-permissions`: for the `serviceAccount` input (default
  `default`) and each of the `checks` input (`{verb, resource, group?}`; default
  get and list pods, get configmaps, list services), whether it is `allowed`,
  explicitly `denied`, and the authorizer's `reason` (null when no authorizer
  had an opinion, which is the usual answer for a plain "no"). `resource` may name a
  subresource (`pods/log`) but has no dot: write `resource: deployments, group:
  apps`, not `deployments.apps`. `serviceAccount.exists` says whether the account is
  there; reviews are answered either way.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `resource-settings` returns only what is unset: empty `pods`
  with `inspected.runningPods: 0` means nothing was running, not that everything is
  sized. `vpas: []` means the CRD exists and no VPA does. `pullFailures: []` means
  no container is waiting on a pull right now; it says nothing about past failures.
  There is no image "rollover time" here: when a container started is `startedAt` in
  the pod's status (`kubernetes:k8s-workload-health/pod-status` shows why pods wait;
  `pod-restarts` has the last termination's `startedAt`).
- **Missing dependencies fail the task.** No `metrics.k8s.io` (metrics-server
  absent or not serving) fails `resource-usage` with `dependency`; no
  VerticalPodAutoscaler CRD fails `vpa-recommendations` with `dependency`. Neither
  ever returns an empty list. `podsWithoutMetrics` counts Running pods with no
  sample yet (new pods); when Running pods exist and none has a sample, the result is
  partial with a `dependency` error (metrics-server has not scraped them yet).
- **`null` is unknown.** `pods` or `workloads` null in `resource-settings`, `request`
  and `limit` null in `resource-usage` for every container, `targetFound: null` in
  `vpa-recommendations`, `exists: null` for a ServiceAccount: that read was forbidden
  and a `permission_denied` error with the scope says which. A result with errors is
  not a clean bill of health for the outputs it did return. A failed review
  creation fails `serviceaccount-permissions`; it never reads as "denied".
- **Cuts.** List outputs stop at `maxItems` (default 200); `inspected` has the
  counts before the cut. Lists inside an entry stop at 50 and messages at 1024
  characters (`truncated`). `checks` takes at most 50 entries.
- **Normal states that look like failures.** A container without a CPU limit is
  common and deliberate (limits cause throttling); a memory limit is the one to
  expect. Setting only a limit makes the API copy it into the request. A completed
  init container still counts for scheduling but runs no more. `usage` is a
  short-window sample (see `window`), not a peak: OOMKills show in restarts, not here. A VPA in
  `updateMode: Off` only recommends. A recommendation that is new, or whose
  `conditions` include `LowConfidence`, is unreliable: the recommender needs days of
  samples (and `NoPodsMatched` means it saw none), so do not size from it yet. A Pending pod with a bad image is listed in
  `images` and not in `resource-settings`, which looks at Running pods.

## What to try next

- Containers restarting or OOMKilled: `kubernetes:k8s-workload-health/pod-restarts`
  on the owner, then `kubernetes:k8s-workload-logs/recent-logs`.
- Unhealthy pods in the namespace, or a quota that blocks sizing:
  `kubernetes:k8s-namespace-health/unhealthy-pods` and
  `kubernetes:k8s-namespace-health/resource-quotas`.
- A node out of CPU or memory: `kubernetes:k8s-node-health/node-conditions`.
- A bad image on one workload: `kubernetes:k8s-workload-health/pod-status`.
- Changing requests and limits is a write:
  `kubernetes:k8s-workload-ops/set-resources`.
