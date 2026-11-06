---
name: k8s-artifactory-health
description: >-
  Health facts for JFrog Artifactory running as a StatefulSet or Deployment, read through the API
  server's pod and Service proxy (no exec, no credential): whether each pod, and each Service
  in front of it, answers Artifactory's liveness and readiness endpoints (200 OK or 503), the
  router's health document (the state of the router and of each Artifactory service) and the
  router's own liveness and readiness (the chart's probes). A 503, a 404 for an endpoint that is
  not served and an endpoint that demands credentials are recorded as facts. Use when Artifactory
  is slow or unreachable, a pod is running but not ready, after a rollout or a database outage, or
  to see which endpoint (pod or Service) is failing.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/statefulset, kubernetes/deployment]
  tags: [health, artifactory, jfrog, readiness, liveness, router]
---

## When to use

The target is the StatefulSet or Deployment that runs Artifactory (the `artifactory` StatefulSet of
the JFrog Helm chart, or an `artifactory-oss` one). Run it on a workload that is not Artifactory
and every endpoint comes back `served: false`: the skill does not check that the workload is
Artifactory.

One task, `health`, reaches each running pod, and each Service whose selector matches the
pod template's labels, over HTTP through the API server's proxy
(`ctx.k8s_proxy_get` on the pod and on the Service, which never follows a redirect): no exec, no port-forward, no Secret read, no credential sent. That needs `get`
on the workload, `list` on `pods` and `services`, and `get` on `pods/proxy` and `services/proxy` in
its namespace. Liveness and readiness are asked on the `port` input, else the pod's container port
named `http-internal` (the chart's Artifactory port, 8081), else 8081; the router's health, liveness
and readiness on `routerPort`, else the container port named `http` (the chart's router port), else
8082. The
classic bundle curled `localhost:8091` inside the pod; 8091 is not a port the chart declares, so
pass `port: 8091` only if your image serves it to the pod IP. `scheme` is `http` by default.

## What each task tells you

- `health`: per pod and per Service, five endpoint rows: `liveness` and `readiness` (Artifactory's),
  `router` (the router health document) and `routerLiveness` and `routerReadiness` (the router's
  own, which the chart's probes use), each with `httpStatus`, `ok`, `served`, `authRequired` and the
  `port` asked. The `router` row carries `router.state` and the `services` it lists with their
  `state` and `message`. Also `podCount`,
  `serviceCount`, `notOkCount` (endpoints with `ok: false`) and the workload's `replicas` and
  `readyReplicas`.

## How to read the result

Outputs are facts, not verdicts.

- **`ok: false` has no reason here.** Artifactory answers 503 on readiness while it starts or when a
  dependency (the database) is down; the skill reports the status, not the 503 body. Use
  `kubernetes:k8s-workload-logs/recent-logs` for the pod's log. A pod that just started reads
  `ok: false` on readiness and is normal. An API server that cannot reach the pod or is overloaded
  is an error (`unreachable`), not `ok: false`. A redirect to another host (an SSO login in front
  of Artifactory) is never followed and is a `dependency` error naming the host, scoped
  `pod/<name>/<endpoint>`.
- **A Service row can say 503 for another reason.** `noEndpoints: true` means the Service has no
  ready pod (typically because readiness fails), not that Artifactory answered. A Service is asked
  on the Service port whose `targetPort` is the pod's Artifactory (or router) port; if none is, that
  endpoint is `null`. The API server picks one endpoint per request, so a Service row is one
  backend's view.
- **A reply that is not the expected one is an error, never ok.** A liveness or readiness 200 is
  ok only when its text is `OK` or it is a JSON document whose `code` is `"OK"`; any other 200 text
  or code, a router page that is not the health document, an unreachable pod: each is an error
  scoped `pod/<name>/<endpoint>` and the row says nothing answered (`httpStatus: null`). When nothing
  answers at all the task fails. The `OK` forms and the router document shape were verified
  against a documented-response stub, not a real Artifactory.
- **`authRequired: true`.** Artifactory itself answered 401 or 403, so that endpoint needs
  credentials, which the skill never sends. It is a `permission_denied` error scoped to the
  endpoint (the result is partial); when every answered endpoint wants credentials the task is
  `skipped` with that reason. An Artifactory access token needs a generic connection (misc plugin,
  M4). A refusal of the proxy itself (no RBAC on `pods/proxy`) is also `permission_denied` but fails
  the task, and a refused Service list or Service proxy keeps the pod facts and records the error.
- **A 404 is `served: false`.** Nothing serves the path on that port. On a pod's `liveness`,
  `readiness`, `routerLiveness` or `routerReadiness` it is also `ok: false` with
  `reason: "not served (booting, wrong port, or not Artifactory)"`, and counts in `notOkCount`:
  a Running pod that does not answer its own probe paths is not healthy. If every endpoint of every
  queried pod answers 404 the task fails with `not_found` ("no Artifactory endpoint answered; check
  the port input"): the `port` or `routerPort` is almost certainly wrong, or the workload is not
  Artifactory. On a Service row, and on a pod's `router` health row, a 404 stays `served: false`
  with `ok: null`. The router is part of every Artifactory 7 edition, OSS included, so a 404 on the
  router port is not an edition difference.
- **Empty lists.** `pods: []` means the selector matches no pod. `serviceCount: 0` with
  `servicesRead: true` means no Service selects the pods; with `servicesRead: false` the Service
  list was refused and says nothing. Pods that are not Running are listed with `queried: false`;
  when the workload wants replicas and no pod is Running the result is partial, with an
  `unreachable` error scoped `pods`. At most 10 pods and 10 Services are asked; the task has 100
  seconds, after which what was not asked gets a `timeout` error.
- **Cuts.** `pods` and `services` stop at `maxItems` (default 200), router services at 50, text at
  1024 characters.
- **Not read.** Artifactory's `/api/system/*` and topology endpoints need credentials and are not
  called; no pod env or Secret is fetched (the reads are `jsonpath` projections).

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the same workload.
- A rollout that stalled: `kubernetes:k8s-workload-health/rollout-status`.
- Storage trouble (full volume, pending claim): `kubernetes:k8s-pvc-health/usage` and
  `kubernetes:k8s-pvc-health/binding-status`.
- The database Artifactory depends on: check its own workload with the matching skill, for example
  `kubernetes:k8s-postgres-operator-health/patroni-status` or the `postgres` plugin.

## Tests

Fixtures are recorded on kind against a stub: a real Artifactory is too heavy for the 4 GiB test
cluster, so `tests/kind/scenarios/30-k8s-artifactory-health.yaml` runs nginx pods (limit 64Mi) that
serve the documented liveness, readiness and router responses (healthy, bare `OK` for Artifactory
and `{"code":"OK"}` for the router's probes; and a variant whose Artifactory liveness is
`{"code":"OK"}`, whose readiness is 503 and whose router lists a service UNHEALTHY) with the labels
and ports of the official JFrog Helm chart, a Service per variant and one more whose
`targetPort` is a number. Replay needs no cluster.
