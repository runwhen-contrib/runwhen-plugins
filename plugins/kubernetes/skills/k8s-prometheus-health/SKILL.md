---
name: k8s-prometheus-health
description: >-
  Health facts for a Prometheus managed by the prometheus-operator (a
  monitoring.coreos.com Prometheus resource), read through the API server's pod
  proxy: per pod whether it is healthy and ready, whether the last config reload
  succeeded, WAL corruptions and head series; active scrape targets up and down
  per scrape pool with the last scrape error and how old the last scrape is; rule
  groups and the rules that fail to evaluate; and which ServiceMonitors the
  Prometheus selects and whether each selected one produced a target, plus
  whether its ServiceAccount may discover in those namespaces. Use when metrics
  are missing, an alert did not fire, a target is down, a rule errors or a new
  ServiceMonitor is not scraped.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/prometheus]
  tags: [health, prometheus, monitoring, servicemonitor, scrape]
---

## When to use

The target is a `Prometheus` custom resource (prometheus-operator, or a
kube-prometheus-stack release) and metrics are missing or stale, a dashboard is
empty, an alert is silent or a ServiceMonitor you created is not scraped. Start with
`server-status`; then `targets` (is it scraping what it should), `rules` (do the
rules evaluate) and `servicemonitor-coverage` (is my ServiceMonitor picked up).

Every task reaches the server over HTTP through the API server's pod proxy
(`ctx.k8s_proxy_get`, which never follows a redirect): no
exec, no port-forward, no secret read. That needs `list` on `pods` and `get` on
`pods/proxy` in the Prometheus namespace, plus `get` on the `prometheuses` resource.
The pods are found by the labels the operator sets (`app.kubernetes.io/name=prometheus`,
`prometheus=<name>`); the port is the container port named `spec.portName` (default `web`),
the scheme is https when `spec.web.tlsConfig` is set and `spec.routePrefix` is honoured.
Out of scope or unreachable this way: `spec.listenLocal: true` (the server listens on the pod's
loopback, so the proxy cannot reach it), a `web` config that requires client certificates (mTLS:
the API server's proxy presents none), and `PrometheusAgent` (a different kind: no targets or
rules API). `spec.externalUrl` and a `--web.route-prefix` set through `spec.containers` or
`additionalArgs` are not read; only `spec.routePrefix` is, so a prefix set another way gives a 404
(an error) on every path.
`servicemonitor-coverage` assumes the discovery role the CR sets in `spec.serviceDiscoveryRole`,
and `Endpoints` when it is unset (the operator's default); the ServiceAccount check asks about
`endpointslices` instead of `endpoints` when the role is `EndpointSlice`.
`servicemonitor-coverage` also needs `list` on `servicemonitors` (cluster-wide when
`serviceMonitorNamespaceSelector` is set), `list` on `namespaces` (when it selects by
label) and `create` on `subjectaccessreviews`: the ServiceAccount check asks the API
server "may this account list services, pods and endpoints in namespace N" and needs no
impersonation. Operator logs are not read here: `kubernetes:k8s-workload-logs/recent-logs`
on the operator Deployment.

## What each task tells you

- `server-status`: per pod `healthy` (/-/healthy) and `ready` (/-/ready), `runtime`
  (`reloadConfigSuccess`, `lastConfigTime`, `corruptionCount`, `uptimeSeconds`,
  `storageRetention`) and `tsdb.headSeries`. `ready: false` is the server's 503 (still
  starting, replaying its WAL). `prometheus.conditions` is the operator's own `Available` and
  `Reconciled` status from the CR (a `Reconciled: False` means the operator could not apply the
  spec or its config); `prometheus.expectedReplicas` is `spec.replicas` (1 when unset) times
  `spec.shards` (1 when unset).
- `targets`: `up`, `down` and `unknown` counts overall and per `scrapePool`; `unhealthy`
  lists the targets that are down or unknown or `overdue`, each with `lastError` (URLs
  without query strings), `lastScrape`, `lastScrapeAgeSeconds` and `downOnPods`.
- `rules`: `groups` with `interval`, `evaluationTimeSeconds`, `evaluationExceedsInterval`
  and `unhealthyRuleCount`; `failingRules` with the evaluation `lastError` and the
  `query`.
- `servicemonitor-coverage`: `coverage.serviceMonitors` says per ServiceMonitor whether
  the Prometheus selects it (`selected`, else `notSelectedReason`) and, if so, its
  `activeTargets`; `selectedWithoutTargets` counts the selected ones that produced none.
  `serviceAccountAccess.checks` says per namespace and resource whether the account may
  `list` and `watch` (and any `evaluationError` the authorizer gave); a ServiceMonitor with
  `namespaceSelector.any: true` is checked cluster-wide, as namespace `""`. `coverage.notes` says
  what was not considered.

## How to read the result

Outputs are facts, not verdicts.

- **Pods answer alike or not.** Replicas scrape the same targets and evaluate the same rules;
  shards scrape different targets. Targets and rules are merged over the pods that answered
  (`queriedPods`); a target or rule group is as bad as its worst pod: the worst health, the
  oldest last scrape or evaluation, `overdue` if any pod says so (`downOnPods`). A target is told
  apart by its scrape pool and its full scrape URL, so targets that differ only by query string
  (blackbox probes) stay apart; only the displayed URL loses its query string. At most 10 pods
  are asked, one per shard (`operator.prometheus.io/shard`) first, then more replicas;
  `podCount` is every pod of the CR and `podsSkipped` the running ones not asked. A pod that is
  not Running is listed and not asked.
- **A task has 100 seconds.** After that, and for a path whose last three pods all timed out,
  the pods not asked get a `timeout` error scoped `pod/<name>` and the result is partial.
- **Nothing answered is an error.** No running pod is `unreachable` (for `server-status` a CR that
  expects pods, `expectedReplicas` above 0, but has none Running returns the pod list with an
  `unreachable` error scoped `pods`, so the result is partial; `replicas: 0` is not an error); a pod that fails is an error
  scoped `pod/<name>` and its fields stay `null`; a reply that is not the expected JSON or text
  (a login page from an auth proxy, a 404 from an old route prefix) is an error, never an
  empty or healthy answer. `permission_denied` on `pods/proxy` ends the task. The server's own
  503 is `ready: false`; an API server that cannot reach the pod or is overloaded is an error
  (`unreachable`), not `ready: false`. A 401/403 from the Prometheus itself (an auth proxy in
  front) is a `dependency` error naming the status and the start of its body, not
  `permission_denied` (that code is the API server's refusal of `pods/proxy`). A redirect to
  another path of the same pod is followed once; a redirect to another host (an SSO login) is
  never followed and is a `dependency` error naming the host.
- **Empty lists.** `targets.unhealthy: []` with `total > 0` means every target is up and fresh.
  `total: 0` means the Prometheus has no active target: nothing is being scraped.
  `rules.groupCount: 0` means no rule file is loaded.
  `serviceMonitors: []` means none exists in the namespaces the Prometheus watches.
- **Not selected.** An unset `serviceMonitorSelector` selects nothing (the operator's rule);
  `{}` selects everything. A ServiceMonitor with `selected: false` is not an error: a release
  label usually differs. `activeTargets: 0` on a selected one means the operator did not
  turn it into a scrape pool or its Service/port matched no endpoints; check that the Service
  exists, the port name matches and the account in `serviceAccountAccess` may discover.
  `selected: null` means the namespace labels could not be read (`complete: false`); a
  forbidden cluster-wide ServiceMonitor list also gives `complete: false`, with a note that
  ServiceMonitors in other namespaces were not considered.
- **Reload and corruption.** `reloadConfigSuccess: false` means the running config is the
  last good one, not the current one. A non-zero `corruptionCount` is cumulative since start.
- **Normal states that look like failures.** `unknown` targets right after start (not
  yet scraped); rule `health: unknown` before the first evaluation; a freshly restarted pod
  with a small `uptimeSeconds` and `headSeries`; `overdue: null` on a target whose Prometheus does not
  report `scrapeInterval` (older versions): the age is still given.
- **Cuts.** Lists stop at `maxItems` (default 200), nested lists at 50; errors and queries are
  cut at 1024 characters and flagged `...Truncated`.
- **Not read.** `discoveredLabels` (they carry relabelled parameters such as tokens), Secret
  data, and scrape credentials. URLs in outputs lose userinfo (up to the last `@` before the
  host), query string and fragment; a token inside the path is not recognised and is not stripped.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the Prometheus StatefulSet or the operator.
- A down target's `lastError` points at the scraped service: check it with
  `kubernetes:k8s-workload-health/pod-status`, and the Service with
  `kubernetes:k8s-ingress-health/backends`.
- `Reconciled: False` in `prometheus.conditions`, or a ServiceMonitor that is selected but never
  becomes a scrape pool: the operator rejected it or the config; its reconcile status is in the
  conditions and the reason (an invalid ServiceMonitor is named in the operator log) in
  `kubernetes:k8s-workload-logs/recent-logs` on the operator Deployment.
- A selected ServiceMonitor with no targets: confirm the Service labels match its
  `selector` and its `endpoints[].port` is a Service port name; fix the RBAC the
  `serviceAccountAccess` section shows as denied.
- A rule error: the `query` and `lastError` are in `failingRules`; fix the expression or the
  `PrometheusRule`.
