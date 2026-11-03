---
name: k8s-jaeger-health
description: >-
  HTTP error facts from Jaeger's query API, read through the API server's pod proxy
  (no exec, no port-forward, no Secret, no credential): for each service Jaeger knows,
  the spans in the look-back window (default 1h) whose HTTP status is a server error
  (5xx) or a client error (4xx), counted per operation with the status codes, the URL
  paths (query string removed) and sample trace IDs. Use when users report 5xx or 4xx
  responses, to find which service and operation produce them, or to confirm that no
  service has traced a 5xx in the last hour. The target is the Deployment or StatefulSet
  that runs Jaeger's query service (all-in-one, or the query component).
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset]
  tags: [health, jaeger, tracing, http, errors]
---

## When to use

The target is the Deployment or StatefulSet that serves the Jaeger query API (`jaeger-query`, or
an all-in-one `jaeger`). The skill does not check that the workload is Jaeger: on another
workload `/api/services` answers HTML or 404 and the task fails with `internal` or `dependency`.
It reads what Jaeger holds, so it tells you about traffic that is traced and stored: a service
that is not instrumented, or whose spans carry no HTTP status attribute, shows no errors.

The task asks the first Running pod (Ready ones first) over HTTP through the API server's pod
proxy (`kubectl get --raw /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/api/...`). That
needs `get` on the workload, `list` on `pods` and `get` on `pods/proxy` in its namespace. The port
is the `port` input, else the container port named `query`, `http-query` or `query-http`, else
16686; `scheme` defaults to `http`. A Jaeger that needs a login in front of it answers 401/403
through the proxy, which reads as `permission_denied` on `pods/proxy`.

## What each task tells you

- `http-errors`: per service (`services`; most server errors first) `serverErrorSpans` (status 500
  or more), `clientErrorSpans` (400 to 499) and, per `operations` entry, the same two counts, the
  count per status code (`byStatus`), the distinct `urlPaths` (at most 10) and up to five
  `sampleServerTraceIds` / `sampleClientTraceIds` to open in the Jaeger UI. `window` is the
  look-back (`since`, default 1h, ending at the run's reference time); `statusCodes` and
  `statusKeys` say what was searched for.

## How to read the result

Outputs are facts, not verdicts.

- **How the search works.** Jaeger's tag search is exact match on one key and one value
  (`tags={"http.status_code":"500"}`; a range or a pattern like `5..` matches nothing). The task
  therefore makes one query per status code in the `statusCodes` setting (default 400, 401, 403,
  404, 429, 500, 502, 503, 504) and per status attribute key (`http.status_code` and the newer
  `http.response.status_code`), for every service. A span with a status that is not in the list
  (418, 501, 408) is not counted; add the code to `statusCodes`. A span whose status is on a
  different attribute (gRPC status, an `error=true` tag with no HTTP status) is not an HTTP error
  here.
- **`limitReached: true` means a lower bound.** `limit` (default 100) is the most traces one query
  returns; Jaeger returns the newest first. When a query returns that many, older error spans may
  be missing and the counts are at least what is shown. Raise `limit` (up to 1500) or narrow
  `since`.
- **Zero is a fact, not proof.** `serverErrorSpans: 0` with `queried: true` means no span with a
  searched status was stored in the window for that service. Check `tracesSeen` (how many traces
  matched any searched status) and that the service is sampled and instrumented. With head
  sampling, a low rate can hide rare 5xx. A Jaeger with no services at all is `services: []`
  and `serviceCount: 0`.
- **A reply that is not Jaeger query JSON is an error, never no errors.** An HTML page, text, JSON
  without `data`, or a body that lists `errors` is an error scoped `service/<name>` (or the task's
  when `/api/services` itself failed) and that service's counts are `null` with `queried: false`.
  When no service could be read the task fails. A path the pod does not serve (404) is `dependency`.
- **Time.** The window is in microseconds since the epoch, computed from the run's reference time
  (`window.startMicros` / `endMicros`); Jaeger's own clock decides which traces are stored. In-memory
  Jaeger keeps only the latest traces (`--memory.max-traces`): an old window may be gone.
- **Only these values leave the task**: status code, operation name, service name, URL path
  (from `url.path`, `http.target`, `url.full`, `http.url` or `http.route`; query string and
  fragment removed) and trace IDs. No other tag, header, log or process value is returned.
  Operation names and paths are cut at 1024 characters (`truncated`).
- **Cuts.** `services` stop at `maxItems` (default 200; later ones are not queried:
  `truncated`), `operations` at 50 per service (`operationsTruncated`), `urlPaths` at 10. One
  service costs two queries per searched status code; the task has 100 seconds, after which (and
  after three timeouts in a row) the services not asked get a `timeout` error and
  `queried: false`.
- **Filters.** `services` limits the run to the named services (`unknownServices` lists names Jaeger
  does not know); `excludeServices` leaves some out (`excludedServices` lists the ones that
  existed).
- **Normal states that look like failures.** A 404 on a probe or a missing static file is a client
  error; 401 on an endpoint that requires login is expected. Jaeger's own service
  (`jaeger-all-in-one` or `jaeger-query`) can appear with traces of its API calls.

## What to try next

- Server errors in one service: `kubernetes:k8s-workload-logs/error-patterns` and
  `kubernetes:k8s-workload-health/pod-status` on that service's workload, and
  `kubernetes:k8s-workload-health/rollout-status` if a rollout is recent.
- Errors that follow the traffic path: `kubernetes:k8s-ingress-health/backends` on the Ingress that
  fronts the service, and `kubernetes:k8s-istio-health/proxy-errors` where a mesh is in use.
- Jaeger itself unhealthy (no pods, storage): `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-pvc-health/usage` on the Jaeger workload.
