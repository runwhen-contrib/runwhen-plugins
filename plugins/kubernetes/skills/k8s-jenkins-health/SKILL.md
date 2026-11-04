---
name: k8s-jenkins-health
description: >-
  Health facts for a Jenkins controller running as a StatefulSet or Deployment, read anonymously
  through the API server's pod proxy (no exec, no credential): whether each pod serves /login (a
  page with the "Jenkins <version>" footer) and which version it is, and from the JSON API the
  build queue items that Jenkins flags stuck or that have waited longer than a threshold (with
  Jenkins's own why text) and the offline executors with their cause. `queue` needs anonymous
  read, which the chart's default JCasC turns off; without it the queue endpoints are reported as
  needing credentials and the task is skipped, never an empty queue. Use when builds do not start,
  the Jenkins UI is slow or down, the queue keeps growing, or after a rollout of Jenkins.
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
`pods` and `get` on `pods/proxy` in its namespace. The port, `scheme` and `urlPrefix` (Jenkins's
`--prefix`, the chart's `jenkinsUriPrefix`, for example `/jenkins`) are the inputs when given; when
not, they come from the Jenkins container's `readinessProbe.httpGet` (its port and scheme, and its
path minus a trailing `/login`; the chart's probe asks `<prefix>/login`), else the container port
named `http`, else 8080, `http` and no prefix. Each pod row says which it asked (`port`, `scheme`,
`urlPrefix`). **Jenkins behind an SSO realm redirects anonymous requests to the identity provider,
so this skill's facts are limited there.**

## What each task tells you

- `http-status`: per pod `httpStatus` and `ok` for `/login` (a 200 with the Jenkins footer is
  `true`; 503 and 404 are `false`), `ssoRedirect` and a `hint`, the `version` and its
  `versionSource`, and `okCount`/`notOkCount`, plus the workload's `replicas` and `readyReplicas`.
- `queue`: per pod, `queue` with `itemCount`, `discoverableCount`, the listed items
  (`stuckAfterItems`: task name and `taskUrl`, `ageSeconds`, `stuck`, `blocked`, `why`), Jenkins's
  own `flaggedStuckCount` and `flaggedBlockedCount`; and `computers` with `offlineCount` and the
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
- **`version` comes from the login page's footer** (`Jenkins 2.452.3`, `versionSource:
  login-footer`). That footer is also what makes a `/login` 200 count as Jenkins: the bare word
  "Jenkins" does not. When the page gives no version (a 503, a 404, a hidden footer) the version is
  the container image's tag (`versionSource: image-tag`), which is what was deployed, not
  necessarily what runs; a tag like `lts` or `latest` gives `null`.
- **A reply that is not the expected one is an error, never ok.** A 200 `/login` that is neither a
  Jenkins page nor a login page, queue JSON without `items`, a computer list without `computer`, an
  `inQueueSince` that is not a number, a pod that cannot be reached: each is an error scoped
  `pod/<name>` (or `pod/<name>/queue`, `.../computers`) and the section stays `null`. A 503 on the
  JSON API (Jenkins starting) is a `dependency` error, as there is nothing to report. When no pod
  answers at all the task fails. A 404 on every pod usually means a wrong prefix: check
  `urlPrefix` against the row.
- **`ssoRedirect: likely`.** `/login` answered 200 with a login page that has no `Jenkins
  <version>` footer, as an SSO identity provider's page does. `ok` is `null` and `hint` says why;
  the queue endpoints of such a Jenkins are not readable anonymously either.
- **"Stuck" is Jenkins's flag, or age.** The queue lists every item Jenkins flags `stuck` (no
  executor can ever take it), whatever its age, and the blocked or buildable items that have waited
  longer than `stuckAfter` (default 30m) by `inQueueSince` against the run's time; flagged items
  first, then oldest first. `flaggedStuckCount` and `flaggedBlockedCount` count the flags over all
  items. The `why` text says what the item waits for; it is cut at 1024 characters, as are names,
  ids and the task `taskUrl`. `ageUnknownCount` counts items with no `inQueueSince` (a flagged one
  is listed with `ageSeconds: null`); `inQueueSince` is Jenkins's clock, so a skewed controller
  clock skews every age.
- **`discoverableCount`** is the number of queue items the anonymous caller can see exist but not
  read; the queue is that much longer than `itemCount`.
- **Offline executors.** `offline` lists computers Jenkins reports offline; a node taken offline on
  purpose has `temporarilyOffline: true` and the admin's cause text. The built-in node is a
  computer too. Cloud agents (Kubernetes pod templates) appear only while they exist, and one that
  is being provisioned shows offline normally.
- **Empty lists.** `pods: []` means the selector matches no pod. `stuckAfterItems: []` with
  `itemCount > 0` means no flagged item and no blocked or buildable one old enough; `itemCount: 0`
  is an empty queue. Pods that are not Running are listed with `queried: false`; when the workload
  wants replicas and no pod is Running the result is partial, with an `unreachable` error scoped
  `pods`. At most 10 pods are asked and the task has 100 seconds, after which what was not asked
  gets a `timeout` error.
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
(limit 64Mi) that serve `/login` (with a `Jenkins 2.452.3` footer) and the JSON of
`/queue/api/json` and `/computer/api/json` in the shape the Jenkins remote API documents, with the
labels and ports of the jenkinsci Helm chart: healthy, a variant with a queue item waiting for days
and an offline node, one that is still starting (503 on every path), one with anonymous read off
(403 on the JSON API) and one served under the prefix `/jenkins` whose readiness probe asks
`/jenkins/login`. The stubs run a python image, so where the image tag gives the version it is
python's. Replay needs no cluster.
