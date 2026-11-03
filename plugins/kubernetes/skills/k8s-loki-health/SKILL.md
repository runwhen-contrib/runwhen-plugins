---
name: k8s-loki-health
description: >-
  Health facts for Grafana Loki running as a StatefulSet or Deployment, read
  through the API server's pod proxy (no exec): whether each pod answers /ready
  and, per hash ring (the ingester ring on /ring, the distributor ring on
  /distributor/ring, the compactor ring on /compactor/ring), the members by
  state (ACTIVE, LEAVING, UNHEALTHY, JOINING), their token counts and how old
  their last heartbeat is, with rings that a workload does not serve recorded as
  facts. Use when Loki pushes or queries fail, an ingester is stuck leaving or
  unhealthy, a pod is running but not ready, or after a rollout of Loki.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/statefulset, kubernetes/deployment]
  tags: [health, loki, logging, ring, ready]
---

## When to use

The target is the StatefulSet or Deployment that runs Loki: the `loki` StatefulSet of a
single-binary install, or one component (`ingester`, `distributor`, `compactor`, ...) of a
microservices or simple-scalable install. Run it on a workload that is not Loki and every ring
comes back `served: false` and `ready` errors: the skill does not check that the workload is
Loki. Start with `ready`; if a pod is not ready or log pushes fail, `ring` shows whether the
member that should serve is ACTIVE with a recent heartbeat.

Both tasks reach each pod over HTTP through the API server's pod proxy
(`kubectl get --raw /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/ready`): no exec,
no port-forward, no secret read. That needs `get` on the workload, `list` on `pods` and `get`
on `pods/proxy` in its namespace. The pods are the workload's own selector. The port is the
`port` input, else the container port named `http-metrics` or `http`, else 3100; `scheme`
defaults to `http`.

## What each task tells you

- `ready`: per pod `ready` (`true` for a 200 that says ready, `false` for a 503), `httpStatus`,
  and the pod's own Ready condition `podReady`; `readyCount` and `notReadyCount`, and the
  workload's `replicas` and `readyReplicas`.
- `ring`: per path in the `ringPaths` setting (default `/ring`, `/distributor/ring`,
  `/compactor/ring`), `served` and, if served, `memberCount`, `byState`, `notActiveCount` and
  the `members` with `state`, `tokenCount`, `zone`, `lastHeartbeat` and `heartbeatAgeSeconds`.
  `viewedFrom` is the pod whose view this is; `unservedBy` lists pods asked before it that
  answered 404.

## How to read the result

Outputs are facts, not verdicts.

- **`ready: false` has no reason here.** Loki's 503 body ("Ingester not ready: waiting for 15s
  after being ready", "Some services are not Running") does not come back through the proxy.
  Use `ring` for the ring state and `kubernetes:k8s-workload-logs/recent-logs` for the pod's
  log. An ingester answers 503 for about `min_ready_duration` (15s by default) after it starts,
  so a pod that just started reads `ready: false` and is normal. An API server that is itself
  overloaded also answers 503, which reads as not ready here. A 403 from the backend (an auth
  proxy in front of Loki) surfaces as `permission_denied` on `pods/proxy`, like a missing RBAC
  grant.
- **A reply that is not the expected one is an error, never ready.** A 200 page that is not
  "ready", a ring page that is HTML, JSON without `shards`, a pod that cannot be reached: each is
  an error scoped `pod/<name>`, and the field stays `null`. When no pod answers at all the task
  fails. Ring JSON was verified on Loki 3.x; an older Loki whose ring page only answers HTML
  gives an `internal` error that says so. The ring page answers JSON only when the request asks for
  it; `kubectl get --raw` sends an Accept header that does, and the skill depends on that: a
  different client would get the HTML page.
- **`served: false` is a fact.** In microservices mode a pod serves the ring of its own component
  (a distributor serves `/distributor/ring`; an ingester `/ring`; a compactor
  `/compactor/ring`); the others answer 404. In single-binary mode (`target=all`) all three are
  served. A ring path in `ringPaths` that no asked pod serves is `served: false` with the
  pods that answered 404. `served: null` means every attempt failed (see `errors`). If every path is
  `served: false` on every asked pod the result is partial with a `dependency` error ("no ring
  served"): the workload may not be a Loki component that runs a ring, or `ringPaths` does not
  name its ring.
- **Ring states.** `ACTIVE` is normal. `JOINING` and `LEAVING` are normal briefly during a
  start or a shutdown; `UNHEALTHY` is reported when the member's last heartbeat is older than
  the ring's heartbeat timeout (check `heartbeatAgeSeconds` against `heartbeat_timeout` in the
  ring config, 1m by default). An `UNHEALTHY` member that is still in the ring after its pod is
  gone is a stale entry: the ring has not forgotten it. `heartbeatAgeSeconds` is measured against the ring page's own `now` (the
  server's clock, the one the ring judges heartbeats by) when the page has one, else against the
  run's reference time, so a small negative number means the clocks disagree. A timestamp that is
  present and does not parse is an `internal` error scoped `pod/<name>` and the field is `null`.
- **Empty lists.** `pods: []` with `podCount: 0` means the selector matches no pod
  (`replicas` says whether that is intended). A ring with `memberCount: 0` and `served: true`
  is an empty ring: nothing has registered. Pods that are not Running are listed with
  `queried: false` and not asked; at most 10 pods are asked. A task has 100 seconds: after that, and for a path whose last three
  pods all timed out, the pods or paths not asked get a `timeout` error scoped `pod/<name>`.
- **Cuts.** `pods` and `rings` stop at `maxItems` (default 200), members at 50; a body is cut
  at 1024 characters and flagged `bodyTruncated`.
- **Not read.** Ring token lists are only counted; the config endpoint (`/config`) and metrics are
  not read.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the same workload.
- Storage trouble behind unready ingesters (WAL replay, full volume):
  `kubernetes:k8s-pvc-health/usage` and `kubernetes:k8s-pvc-health/binding-status`.
- A rollout that stalled: `kubernetes:k8s-workload-health/rollout-status`.
- Components that run as separate workloads (distributor, ingester, compactor): run `ring` on
  each; the workload that serves a path is the one whose component owns that ring.
