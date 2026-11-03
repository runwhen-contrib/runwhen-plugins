---
name: k8s-jenkins-health
description: >-
  Health facts for a Jenkins controller running as a StatefulSet or Deployment, read anonymously
  through the API server's pod proxy (no exec, no credential): whether each pod serves /login and
  its X-Jenkins version header, and from the JSON API the build queue items that have waited
  longer than a threshold (with Jenkins's own why text) and the offline executors with their
  cause. If anonymous read is off the queue endpoints are reported as needing credentials, not as
  healthy. Use when builds do not start, the Jenkins UI is slow or down, the queue keeps
  growing, or after a rollout of Jenkins.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/statefulset, kubernetes/deployment]
  tags: [health, jenkins, ci, queue, executors]
---

## When to use

The target is the StatefulSet or Deployment that runs the Jenkins controller (the `jenkins`
StatefulSet of the jenkinsci Helm chart). Run it on a workload that is not Jenkins and `/login`
comes back `ok: false` or an error: the skill does not check that the workload is Jenkins. Start
with `http-status` to see whether the controller answers; `queue` shows whether builds are waiting
and whether the executors they need are online.

Both tasks reach each running pod over HTTP through the API server's pod proxy
(`kubectl get --raw /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/login`): no exec, no
port-forward, no Secret read, no credential sent. That needs `get` on the workload, `list` on
`pods` and `get` on `pods/proxy` in its namespace. The port is the `port` input, else the pod's
container port named `http` (the chart's), else 8080; `scheme` defaults to `http`; `urlPrefix`
carries Jenkins's `--prefix` (the chart's `jenkinsUriPrefix`, for example `/jenkins`).

## What each task tells you

- `http-status`: per pod `httpStatus` and `ok` for `/login` (200 from a Jenkins is `true`; 503 and
  404 are `false`), the `X-Jenkins` `version`, `headersExposed`, and `okCount`/`notOkCount`, plus
  the workload's `replicas` and `readyReplicas`.
- `queue`: per pod, `queue` with `itemCount`, the items waiting longer than `stuckAfter`
  (`stuckAfterItems`: task, `ageSeconds`, `stuck`, `blocked`, `why`), Jenkins's own
  `flaggedStuckCount` and `flaggedBlockedCount`; and `computers` with `offlineCount` and the
  `offline` executors (name, `temporarilyOffline`, `cause`). Top level `stuckAfterCount` and
  `offlineCount` add the pods up.

## How to read the result

Outputs are facts, not verdicts.

- **Anonymous read is off.** Jenkins answers 403 to `/queue/api/json` and `/computer/api/json` when
  anonymous users lack Overall/Read. That is `authRequired`-style: a `permission_denied` error with
  the hint "anonymous read is off; a Jenkins API token needs a generic connection (misc plugin,
  M4)", and the section is `null`, never an empty queue. When every endpoint asked answered 401 or
  403 the task is `skipped` with that reason. The skill sends no credential, so there is no way
  to read the queue of such a Jenkins from this plugin. `/login` is public and still works.
  A refusal of the proxy itself (no RBAC on `pods/proxy`) is also `permission_denied` but fails the
  task.
- **`version` is read from response headers** that `kubectl get -v=8` logs. `headersExposed: true`
  with `version: null` means the response had no `X-Jenkins` header (a reverse proxy in front may
  strip it); `headersExposed: false` means the proxy showed no headers at all and the version is
  unknown, not absent.
- **A reply that is not the expected one is an error, never ok.** A 200 `/login` that does not look
  like Jenkins (no `X-Jenkins` header, no "Jenkins" in the page), queue JSON without `items`, a
  computer list without `computer`, an `inQueueSince` that is not a number, a pod that cannot be
  reached: each is an error scoped `pod/<name>` (or `pod/<name>/queue`, `.../computers`) and the
  section stays `null`. A 503 on the JSON API (Jenkins starting) is a `dependency` error, as there
  is nothing to report. When no pod answers at all the task fails.
- **"Stuck" here is age.** Items are listed when they have waited longer than `stuckAfter` (default
  30m) by `inQueueSince` against the run's time. Jenkins's own `stuck` flag (no executor can ever
  take the item) and `blocked` flag are separate counts: an item can be flagged and younger than
  `stuckAfter`, or old and unflagged. The `why` text says what the item waits for; it is cut at
  1024 characters. `ageUnknownCount` counts items with no `inQueueSince`.
- **Offline executors.** `offline` lists computers Jenkins reports offline; a node taken offline on
  purpose has `temporarilyOffline: true` and the admin's cause text. The built-in node is a
  computer too. Cloud agents (Kubernetes pod templates) appear only while they exist.
- **Empty lists.** `pods: []` means the selector matches no pod. `stuckAfterItems: []` with
  `itemCount > 0` means no item is old enough; `itemCount: 0` is an empty queue. Pods that are not
  Running are listed with `queried: false`; at most 10 pods are asked and the task has 100 seconds,
  after which what was not asked gets a `timeout` error.
- **Cuts.** `pods` and `stuckAfterItems` stop at `maxItems` (default 200), offline executors at 50,
  text at 1024 characters.
- **Not read.** Queue items' build parameters (they can hold secrets) are not requested: the
  calls ask Jenkins for only the fields above with `?tree=`. Job lists, build logs, plugin lists
  and credentials are not read, and no pod env or Secret is fetched.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the same workload.
- A rollout that stalled: `kubernetes:k8s-workload-health/rollout-status`.
- Home directory trouble (full volume, pending claim): `kubernetes:k8s-pvc-health/usage` and
  `kubernetes:k8s-pvc-health/binding-status`.
- Items waiting for a label with no node, or Kubernetes agents that cannot start: check the agent
  pods' namespace with `kubernetes:k8s-namespace-health` and the Warning events there.

## Tests

Fixtures are recorded on kind against a stub: a real Jenkins is too heavy for the 4 GiB test
cluster, so `tests/kind/scenarios/30-k8s-jenkins-health.yaml` runs small python:3.12-alpine pods
(limit 64Mi) that serve `/login` and the JSON of `/queue/api/json` and `/computer/api/json` in the
shape the Jenkins remote API documents, with the labels and ports of the jenkinsci Helm chart:
healthy, a variant with a queue item waiting for days and an offline node, one that is still
starting (503 on every path) and one with anonymous read off (403 on the JSON API). Replay needs no
cluster.
