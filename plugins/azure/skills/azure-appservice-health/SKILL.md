---
name: azure-appservice-health
description: >-
  Health of one Azure App Service web app, function app or App Service plan: state and availability,
  plan status, SKU and instance count, autoscale settings, Azure's Resource Health verdict, Azure
  Monitor metrics (Http5xx, Http4xx, response time, requests, plan CPU and memory percentage,
  function executions), deployment slots and recent deployments, and configuration facts (HTTPS only,
  minimum TLS, FTPS, always on, managed identity). Also restart, start, stop, swap-slot and scale as
  write tasks with a dry run. App settings and connection strings are never read. Use when a web app or
  function app returns errors or is slow, stopped or flapping, a deployment or swap went wrong, or a
  plan looks starved.
runwhen:
  role: health
  appliesTo:
    types: [azure/web-app, azure/app-service-plan]
  tags: [health, appservice, webapp, functionapp, plan]
---

## When to use

A web app or function app answers 5xx, is slow, was stopped, or its latest deployment may have failed;
or a plan is starved of CPU or memory, or its instance count looks wrong. Each task answers one question
about the object the run targets. A function app is a `azure/web-app` whose `kind` contains `functionapp`.
The read tasks only read, through the connection's own az session (`azure/cli`), by `az rest` GETs of the
App Service objects and `az monitor metrics list`. Nothing they call returns an app setting, a connection
string or a publishing credential, and the outputs never carry one.

The target is `azure/subscriptions/<id>/resourcegroups/<group>/web-apps/<name>` or `.../app-service-plans/<name>`.
Every task reads the object first: one that is gone is `not_found`. Tasks marked "app or plan" accept either:
on a web app they read the plan the app runs on (which may be in another resource group). The
`commandTimeout` setting (default 60s) bounds each read; the metric tasks take a `window` (default `24h`, 1m to
30d) that ends at the run's reference time, in grains of `metricInterval` (default 1h).

## What each task tells you

- `app-state` (web app): `state` (Running, Stopped), `availabilityState` (Normal, Limited,
  DisasterRecoveryMode), `usageState` (Exceeded: a plan quota is used up), `enabled`, kind, host name and plan.
- `plan-state` (app or plan): `planStatus`, `sku` (name, tier, size, capacity), `instanceCount`,
  `maximumNumberOfWorkers`, `numberOfSites`, elastic-scale, per-site-scaling, zone-redundant and spot flags.
- `autoscale` (app or plan): the autoscale settings that target the plan: `enabled`, each profile's
  `minimum`, `maximum`, `default`, `ruleCount`, and `atMaximum` (the plan has reached the profile's ceiling; only for an enabled
  setting, and it is a fact about each profile: which one is active now is not evaluated). The list is the
  subscription's, because a setting can live in any resource group.
- `resource-health` (app or plan): Azure's `availabilityState`, `summary`, `detailedStatus`, reason and times.
  It does not register Microsoft.ResourceHealth: if the subscription never did, the run fails with `dependency`.
- `configuration` (web app): `httpsOnly`, `minTlsVersion`, `scmMinTlsVersion`, `ftpsState`, `alwaysOn`,
  `healthCheckPath`, `runtime` (a custom container's image without credentials; `COMPOSE|` and `KUBE|` only as the prefix), `publicNetworkAccess`, `clientCertMode`, `vnetIntegrated` and the managed
  `identity` type (a count of user-assigned identities, no ids).
- `deployments` (web app): the slots, and the deployment records of `window` (default 7d): status, which one is
  `active`, deployer, message (the committer's text: cut to 256, e-mail addresses and URL credentials masked), `failedInWindow`, `inProgress` and the `latest` record.
- `http-metrics` (web app): `requests`, `http5xx`, `http4xx`, `responseTime` (seconds), each per interval.
- `function-metrics` (function app): `executions`, `executionUnits` and `httpServerErrors` (Http5xx).
- `plan-metrics` (app or plan): `cpu`, `memory` (percent) and `diskQueue`.
- Write tasks, all with a dry run that reads and checks and sends nothing: `restart`, `start`, `stop` (web app),
  `swap-slot` (web app; `sourceSlot`, `targetSlot` default `production`; refused while a swap is under way) and `scale`
  (plan only; `instances` and/or `sku`, absolute values; it reads the autoscale settings first and refuses an instance
  count outside an enabled setting's bounds unless `overrideAutoscale: true`). Each reports before and after state. Read each task's effects before approving it.

## How to read the result

- Every metric field is `{grains, withData, missing, min, max, last, lastGrainMissing, total, points, crossings...}`.
  `withData: 0` or `missing` above zero is no information, not a quiet app; `crossed` is only about
  intervals that have a value. A metric field that is null could not be read: the run is `partial` with the
  reason scoped to that field. `lastGrainMissing: true` means `last` is older than the newest interval.
- `http5xx.crossed` counts intervals above `http5xxThreshold` (default 50 per interval; it is per
  `metricInterval`, so a 1h grain and a 5m grain need different thresholds). `responseTime` is in seconds.
- `plan-metrics` on a plan with a Consumption (Dynamic, Y1) SKU has no CPU or memory metric: all three fields
  null and the run `failed`. A function app on a Consumption plan normally has `alwaysOn: false`.
- `function-metrics.httpServerErrors` counts failed HTTP-triggered functions only. Failures of queue, timer or
  event triggers are not platform metrics (Application Insights holds them); do not read their absence as success.
- `state: Stopped` with `running: false` is how a stopped app looks: not an error of the task. `limited: true` means
  availabilityState Limited and `quotaExceeded: true` that a plan quota is used up; read `availabilityState` itself for
  DisasterRecoveryMode.
- `autoscale.configured: null` means the autoscale list was cut short (`listComplete: false`) and held nothing
  for the plan: unknown, not "no autoscale" (the whole subscription is searched, up to 5 pages and 120s).
- `deployments.deployments.total: 0` with `listed: 0`: Azure holds no deployment records. An app deployed from a
  package or a container without Kudu has none; that is no data, not a clean deploy. Slots exist only on
  Standard and above, so `slots.total: 0` on a Basic plan is normal.
- Write tasks: `applied: false` with `dryRun: true` is a dry run. `reachedExpected` / `reachedRequested` false
  after an applied call is not a failure, Azure may lag: read `app-state` or `plan-state` again.
- Lists are cut at `maxItems` (default 200; `omitted` says how many), nested lists at 50, and text fields at
  1024 characters (`truncated`).
- The run fails with a code, never an empty result, classified from az's own `ERROR:` lines (the exit code decides
  first; warnings az prints on a successful command are ignored): `permission_denied` (AuthorizationFailed,
  Forbidden, an AADSTS code, no `az login`), `not_found` (the app, plan, slot or subscription is gone),
  `rate_limited` (TooManyRequests, 429), `unreachable`, `timeout`, `dependency` (no az, a provider that is not
  registered), `invalid_input`; anything else is `internal`.

## What to try next

- `state: Stopped`: `azure:azure-appservice-health/start` after finding out who stopped it.
- 5xx up and `deployments` shows a failed or just-finished deployment: `swap-slot` again exchanges the slots back, but
  it is a second change of what production serves (with its own warm-up); or redeploy from your pipeline. A redeploy from a package is not in this skill (it needs the package).
- `plan-metrics` shows CPU or memory over the threshold with `autoscale.atMaximum: true` or no autoscale:
  `scale` the plan (dry run first), then read `plan-state` again.
- `resource-health` Degraded or Unavailable: it is Azure's side; check back or open a support case. With
  Available, look at `http-metrics` and `configuration` (`alwaysOn: false` lets an idle app unload).
- The app runs on AKS or calls a Key Vault: `azure:azure-kv-health/availability`, or
  `kubernetes:k8s-workload-health/pod-status` for the cluster side. `azure:azure-base/discover` lists the
  subscription's other apps and plans.
- Activity log, Advisor, Application Insights and app log analysis are not in this skill.

## Tests

The plugin is stub-recorded: the fixtures were recorded against `tests/stub/bin/az`, a stand-in for the az CLI that
serves canned responses shaped after the CLI and ARM references (`tests/stub/README.md`), not against a real
subscription. Re-record against a real account when a credential exists.
