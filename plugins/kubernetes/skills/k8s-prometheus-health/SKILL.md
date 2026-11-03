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
(`kubectl get --raw /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/...`): no
exec, no port-forward, no secret read. That needs `list` on `pods` and `get` on
`pods/proxy` in the Prometheus namespace, plus `get` on the `prometheuses` resource.
The pods are found by the labels the operator sets (`app.kubernetes.io/name=prometheus`,
`prometheus=<name>`); the port is the container port named `spec.portName` (default `web`),
the scheme is https when `spec.web.tlsConfig` is set and `spec.routePrefix` is honoured.
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
  starting, replaying its WAL).
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
  `list` and `watch`.

## How to read the result

Outputs are facts, not verdicts.

- **Pods answer alike or not.** Replicas scrape the same targets and evaluate the same rules;
  shards scrape different targets. Targets and rules are merged over the pods that answered
  (`queriedPods`); a target is as bad as its worst pod (`downOnPods`). At most 10 pods are
  asked; a pod that is not Running is listed and not asked.
- **Nothing answered is an error.** No running pod is `unreachable`; a pod that fails is an error
  scoped `pod/<name>` and its fields stay `null`; a reply that is not the expected JSON or text
  (a login page from an auth proxy, a 404 from an old route prefix) is an error, never an
  empty or healthy answer. `permission_denied` on `pods/proxy` ends the task. Through the
  proxy a 503 from the server is all that is seen (its text is lost), so `ready: false`
  carries no reason.
- **Empty lists.** `targets.unhealthy: []` with `total > 0` means every target is up and fresh.
  `total: 0` means the Prometheus has no active target: nothing is being scraped.
  `rules.groupCount: 0` means no rule file is loaded.
  `serviceMonitors: []` means none exists in the namespaces the Prometheus watches.
- **Not selected.** An unset `serviceMonitorSelector` selects nothing (the operator's rule);
  `{}` selects everything. A ServiceMonitor with `selected: false` is not an error: a release
  label usually differs. `activeTargets: 0` on a selected one means the operator did not
  turn it into a scrape pool or its Service/port matched no endpoints; check that the Service
  exists, the port name matches and the account in `serviceAccountAccess` may discover.
  `selected: null` means the namespace labels could not be read (`complete: false`).
- **Reload and corruption.** `reloadConfigSuccess: false` means the running config is the
  last good one, not the current one. A non-zero `corruptionCount` is cumulative since start.
- **Normal states that look like failures.** `unknown` targets right after start (not
  yet scraped); rule `health: unknown` before the first evaluation; a freshly restarted pod
  with a small `uptimeSeconds` and `headSeries`; `overdue: null` on a target whose Prometheus does not
  report `scrapeInterval` (older versions): the age is still given.
- **Cuts.** Lists stop at `maxItems` (default 200), nested lists at 50; errors and queries are
  cut at 1024 characters and flagged `...Truncated`.
- **Not read.** `discoveredLabels` (they carry relabelled parameters such as tokens), Secret
  data, and scrape credentials.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the Prometheus StatefulSet or the operator.
- A down target's `lastError` points at the scraped service: check it with
  `kubernetes:k8s-workload-health/pod-status`, and the Service with
  `kubernetes:k8s-ingress-health/backends`.
- A selected ServiceMonitor with no targets: confirm the Service labels match its
  `selector` and its `endpoints[].port` is a Service port name; fix the RBAC the
  `serviceAccountAccess` section shows as denied.
- A rule error: the `query` and `lastError` are in `failingRules`; fix the expression or the
  `PrometheusRule`.
