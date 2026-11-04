---
name: k8s-litellm-health
description: >-
  Health facts for a LiteLLM proxy running as a Deployment or StatefulSet, read from its
  unauthenticated health endpoints through the API server's pod proxy (no exec, no
  port-forward, no Secret, no master key): whether each pod answers /health/liveliness and
  /health/liveness, what /health/readiness says (ready or not, the database state, and the
  cache, litellm version and success callbacks when the proxy exposes them), and which Services
  select the pods, with their ports, ready endpoints and target-port declaration. Use when
  the proxy's clients get errors or timeouts, a pod is Running but not ready, the proxy lost
  its database, or after a rollout of LiteLLM.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset]
  tags: [health, litellm, llm, liveness, readiness, service]
---

## When to use

The target is the Deployment or StatefulSet that runs the LiteLLM proxy (default port 4000). The
skill does not check that the workload is LiteLLM: on another workload the health paths answer 404
or HTML and the tasks fail with `dependency` or `internal`. Start with `readiness`; if clients
cannot reach the proxy, `service` shows whether a Service selects the pods and has ready endpoints.

`liveness` and `readiness` reach each pod over HTTP through the API server's pod proxy
(`kubectl get --raw /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/health/...`): no exec,
no port-forward, no Secret read, no credential sent. That needs `get` on the workload, `list` on
`pods` and `get` on `pods/proxy` in its namespace. The port is the `port` input, else the container
port named `http`, `litellm` or `http-litellm` of the main container (the one whose image is
LiteLLM's, else the first that is not a sidecar; `port` is the override), else 4000; `scheme`
defaults to `http`. `service`
reads only Kubernetes objects: `get` on the workload, `list` on `services` and on
`discovery.k8s.io` `endpointslices`.

## What each task tells you

- `liveness`: per pod `alive` (true when every served path answered 200 with LiteLLM's alive text,
  false for a 503) and, per path (`/health/liveliness`, LiteLLM's own spelling, and
  `/health/liveness`), `served`, `httpStatus` and `alive`; `aliveCount`, `notAliveCount`, and the
  workload's `replicas` and `readyReplicas`.
- `readiness`: per pod `ready` (true for a 200 whose `status` is `healthy`, false for a 503 or
  another status), `httpStatus`, `db` (`connected`, `Not connected` when no database is
  configured, `disconnected`), and, when the proxy serves its detailed payload, `cache` (type),
  `litellmVersion` and `successCallbacks`; `detailed` says whether they were there. For a 503,
  `cause` (see below). `queriedCount` and `unaskedCount` count the pods asked and not asked.
- `service`: for each Service whose selector picks the workload's pods: its `ports` (port,
  targetPort, protocol), `exposesPort` (a Service port equals `servicePort`, default 4000: the
  Service side only), `targetPortMatches` (a targetPort leads to `containerPort`, the port the
  proxy listens on in the template's main container), `targetPortDeclared` (the pod template
  declares that container port) and `endpoints` (`readyEndpoints`, `notReadyEndpoints` over its
  EndpointSlices, one count per pod).

## How to read the result

Outputs are facts, not verdicts.

- **`ready: false` and `alive: false` come with a cause where it can be told.** The proxy does not
  pass a 503's body on, so `db` and `status` are `null`. Read from LiteLLM 1.102.1 (older versions
  may differ): `/health/liveliness` answers 503 only once a graceful shutdown has begun;
  `/health/readiness` answers 503 then too, and also while a configured database is not connected
  (unless `allow_requests_on_db_unavailable` keeps the pod in rotation). So the task asks liveness
  after a readiness 503 and sets `cause`: `db_unreachable_likely` (liveness 200: the database is
  down, or in the rare case building the detailed payload failed), `draining` (liveness 503 too: a
  shutdown, e.g. a preStop `/health/drain`), `undetermined` (liveness could not be read; an error
  scoped `pod/<name>` says why). `livenessHttpStatus` is what liveness gave. For the database
  itself use `kubernetes:k8s-workload-logs/recent-logs`. An API server that is itself overloaded
  also answers 503, which reads as not ready here. A 403 from the backend reads as
  `permission_denied` on `pods/proxy`.
- **The public payload is small.** Recent LiteLLM answers `{"status":"healthy","db":...}` on
  `/health/readiness`; `cache`, `litellmVersion` and `successCallbacks` are `null` and
  `detailed: false` unless `general_settings.allow_public_health_readiness_details` is set. The
  detailed payload at `/health/readiness/details` needs a key and is not read.
- **`db: "Not connected"` is normal without a database** (a proxy with no `DATABASE_URL`): it is a
  fact, not a fault. `db: "disconnected"` with a 200 (older versions, or
  `allow_requests_on_db_unavailable` set) means the database is configured and unreachable while
  the proxy keeps serving; 1.102.1 without that setting answers 503 instead.
- **A reply that is not the expected one is an error, never healthy.** A 200 page that is not the
  alive text, readiness JSON without `status`, a pod that cannot be reached: each is an error scoped
  `pod/<name>` and the field stays `null`. When no pod answers at all the task fails. A pod that
  serves neither liveness path, or not `/health/readiness`, is a `dependency` error.
- **A 404 on one liveness path is a fact** (`served: false`): older proxies serve only the
  `liveliness` spelling. A pod that serves at least one path and answers alive on it is alive.
- **Empty lists.** `pods: []` with `podCount: 0` means the selector matches no pod (`replicas`
  says whether that is intended). Pods that are not Running are listed with `queried: false` and
  not asked; at most 10 pods are asked, and the task stops asking after 100 seconds or three
  timeouts in a row (the pods not asked get a `timeout` error scoped `pod/<name>`). Pods that are
  not Running, or Running ones past the cap or `maxItems`, are counted in `unaskedCount` and named
  in an error scoped `pods/not-running` or `pods/not-asked` (so the result is `partial`: it does
  not cover every pod).
  `services: []` means no Service in the namespace selects the pods (clients cannot reach them
  through a Service); `endpoints: null` means the EndpointSlices could not be read (a scoped
  `permission_denied` error).
- **Ready endpoints.** An endpoint counts as ready unless its `ready` condition is false.
  `notReadyEndpoints > 0` with `readyEndpoints: 0` is a Service that routes nowhere: the pods fail
  their readiness probe. A dual-stack Service has an IPv4 and an IPv6 EndpointSlice listing the
  same pod; it is counted once (by its `targetRef`), as not ready if either entry is.
  `exposesPort: false` with `targetPortMatches: true` is a Service on another port (8080) that still
  leads to the proxy; `exposesPort: true` with `targetPortMatches: false` is a Service on 4000 whose
  targetPort leads somewhere else. A `targetPortDeclared: false` number is legal (a container may listen
  on an undeclared port); a declared-name mismatch leaves the endpoints without a port.
- **Cuts.** `pods` and `services` stop at `maxItems` (default 200), ports and callbacks at 50;
  text fields are cut at 1024 characters (`truncated`).
- **Not read.** `/model/info`, `/v1/models`, the deep `/health` and anything else that needs the
  master key are not asked; no Secret, environment variable or `printenv` output is read. Spend and
  budget governance belongs to a separate skill on a connection with the key.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status`,
  `kubernetes:k8s-workload-health/pod-restarts` and `kubernetes:k8s-workload-logs/recent-logs` on the
  same workload.
- A rollout that stalled: `kubernetes:k8s-workload-health/rollout-status`.
- Clients reach the proxy through an Ingress: `kubernetes:k8s-ingress-health/backends` on it.
- The database behind the proxy is a PostgreSQL operator cluster:
  `kubernetes:k8s-postgres-operator-health/cluster-status` (when `db` is `disconnected`).
