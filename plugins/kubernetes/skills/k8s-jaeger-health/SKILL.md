---
name: k8s-jaeger-health
description: >-
  HTTP error facts from Jaeger's query API, read through the API server's pod proxy
  (no exec, no port-forward, no Secret, no credential): per service Jaeger knows, the spans
  in the look-back window (default 1h) with an HTTP status of 400 or more, counted per
  operation with the status codes, URL paths (ids and query strings removed) and sample trace
  IDs. Finds 5xx server spans that carry OpenTelemetry's error status whatever the code, plus
  the 4xx codes you list and any status a returned trace happens to carry. Use when users
  report 5xx or 4xx responses, to find which service and operation produce them, or to see
  what Jaeger holds. Not proof that no service has errors. The target is the Deployment or
  StatefulSet that runs Jaeger's query service (all-in-one, or the query component).
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
proxy (`ctx.k8s_proxy_get`, which never follows a redirect). That
needs `get` on the workload, `list` on `pods` and `get` on `pods/proxy` in its namespace. The port
is the `port` input (the override), else the port named `query`, `http-query` or `query-http` of
the main container (the one whose image is Jaeger's, else the first that is not a sidecar), else
16686; `scheme` defaults to `http`. A Jaeger that needs a login in front of it answers 401/403
or redirects to a login page: a 401/403 is a `dependency` error naming the status (the backend's, not
the API server's `permission_denied` on `pods/proxy`), and a redirect to another host is never
followed and is a `dependency` error naming the host.

## What each task tells you

- `http-errors`: per service (`services`; most server errors first) `serverErrorSpans` (inbound
  spans with status 500 to 599), `clientErrorSpans` (inbound, 400 to 499) and `outboundErrorSpans`
  (spans of `span.kind` client with 400 to 599: the service's own calls to others, kept apart);
  per `operations` entry the same counts, the count per status code (`byStatus`,
  `outboundByStatus`), the distinct `urlPaths` (at most 10) and up to five sample trace IDs. `window`
  is the look-back (`since`, default 1h, ending at the run's reference time). `queryPod` is the
  pod asked, with its `startTime`, `containerStartedAt` and `restartCount`.

## How to read the result

Outputs are facts, not verdicts.

- **What is covered.** Two passes, so the 100 seconds go to coverage first. Pass 1 asks every
  service for its traces with the tag `error=true`: OpenTelemetry marks a 5xx server span as an
  error whatever the code, so this finds 5xx (500, 507, 599...) without listing codes. Pass 2 asks,
  service by service, for each 4xx code in `statusCodes` (default 400, 401, 403, 404, 429) on both
  status attributes (`http.status_code`, `http.response.status_code`), because OpenTelemetry does not
  mark a 4xx as an error and Jaeger's tag search is exact match (`5..` matches nothing). Every
  span with a status of 400 to 599 in any returned trace is counted, listed or not. So: 5xx is
  covered when the span carries the error status (or sits in a trace another query returned);
  4xx only for the listed codes (408, 410 and 418 are missed unless you add them). A 5xx span from
  an instrumentation that does not set the error status, in a trace no query returns, is not
  counted. The task never proves that a service has no 5xx.
- **`limitReached: true` means a lower bound.** `limit` (default 100, at most 500) is the most
  traces one query returns. Jaeger returns the newest first on in-memory and Elasticsearch/OpenSearch
  storage; on Cassandra the order is not guaranteed, so a cut list may miss the newest. The top-level
  `limitReached` says some service hit it. A larger `limit` makes the query pod build and send up to
  that many whole traces per query (memory on the pod and in the reply); raise it for one service
  with `services` rather than for all, or narrow `since`.
- **Zero is a fact, not proof.** `serverErrorSpans: 0` with `queried: true` means none of the
  searched spans in the window had a 5xx. `tracesSeen` is how many distinct traces the queries
  returned for the service, whatever status they hold (traces with an error tag but no HTTP status
  count too). With head sampling, a low rate can hide rare 5xx. A Jaeger with no services at all is
  `services: []` and `serviceCount: 0`.
- **Partial services.** `complete: false` with `queried: true` means the service ran out of budget or
  one of its queries failed after pass 1; its counts come from the `queriesDone` of `queriesPlanned`
  queries that finished (a lower bound) and an error scoped `service/<name>` says why. `queried:
  false` means not even pass 1 finished: counts are `null`. A reply that is not Jaeger query JSON is
  an error, never no errors; when no service could be read the task fails. A path the pod does not
  serve (404) is `dependency`.
- **Budget.** One service costs 1 + 2 queries per listed code; the task has 100 seconds (it stops
  asking after that and after three timeouts in a row), enough for pass 1 over about 50 services,
  which is why `maxItems` defaults to 50. Services beyond it are not asked: `truncated`, and
  `servicesNotQueried` counts them with the ones that failed.
- **Time and storage.** The window is in microseconds since the epoch (`window.startMicros` /
  `endMicros`); Jaeger's own clock decides which traces are stored. In-memory Jaeger keeps only the
  latest traces (`--memory.max-traces`) and loses them on a restart: check `queryPod`'s
  `containerStartedAt` and `restartCount` before reading an empty result as quiet. With
  `storageMayBeSharded: true` (more than one replica of an all-in-one image, whose memory or Badger
  store lives in each pod; `storageBasis` says why) each pod holds only its own share and the
  counts are those of the pod asked; use a Jaeger on shared storage (Elasticsearch, Cassandra)
  behind the query service for the whole picture. The args and env of the pod are not read, so a
  shared backend configured there is not seen.
- **Only these values leave the task**: status code, operation name, service name, URL path and trace
  IDs. The path is `http.route` (a template) when the span has it, else the raw `url.path`,
  `http.target`, `url.full` or `http.url` with the query string, fragment and `;params`
  removed and every id-like segment (all digits, 12+ hex, uuid- or token-like, API-key prefixes)
  replaced by `{id}`. Operation names get the same treatment for any path or URL in them. No other
  tag, header, log or process value is returned. Operation names and paths are cut at 1024
  characters (`truncated`), `operations` at 50 per service (`operationsTruncated`), `urlPaths` at 10.
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
