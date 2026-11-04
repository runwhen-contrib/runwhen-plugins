---
name: azure-kv-health
description: >-
  Health of one Azure Key Vault: Azure's own Resource Health verdict, the Availability metric over a
  window (every interval counted, none read as healthy), soft delete, purge protection, network and
  authorization configuration, secrets, certificates and keys that are expired or expire within N
  days, failed operations in the audit log (Log Analytics), and request volume and latency against
  thresholds. Use when an application cannot read a secret or certificate, a vault is slow or
  throttling, a certificate or credential may have lapsed, or a Key Vault needs a quick audit of its
  protection settings.
runwhen:
  role: health
  appliesTo:
    types: [azure/keyvault]
  tags: [health, keyvault, expiry, security]
---

## When to use

An application reports Key Vault errors (403, 429, timeouts), a secret or certificate may have
expired, or you are asked whether a vault is protected (soft delete, purge protection, private
access). Each task answers one question about the vault the run targets; they read only, through the
connection's own az session (`azure/cli`), and never return a secret, key or certificate value.

Every task looks the vault up first (`az keyvault show`): a vault that is gone is `not_found`. The
`commandTimeout` setting (default 60s) bounds each az command; the metrics tasks take a `window`
(default `24h`, 1m to 30d) that ends at the run's reference time.

## What each task tells you

- `resource-health` returns `health`: `availabilityState` (Available, Degraded, Unavailable,
  Unknown), `healthy`, Azure's `summary` and `detailedStatus` (cut to 1024 characters), the reason,
  when it occurred and was reported, and the number of service-impacting events. It does not register
  the Microsoft.ResourceHealth provider: if the subscription never did, the run fails with
  `dependency`.
- `availability` returns `availability`: the Availability metric (percent) per `metricInterval`
  (default 1h). `hasData`, the intervals with and without a value, `min`, `max`, `last`, the newest 50
  intervals and `belowThreshold` (some interval below `availabilityMinPercent`, default 100).
- `configuration` returns `configuration`: `softDeleteEnabled`, retention, `purgeProtectionEnabled`,
  `rbacAuthorization`, `publicNetworkAccess`, the firewall's default action, rule, private endpoint
  and access policy counts.
- `expiring-items` takes `thresholdDays` (default 31) and returns `expiry`: secrets, certificates and
  keys already expired or expiring within the threshold, soonest first, with `daysRemaining`; how many
  of each kind were listed (`scanned`) and how many have no expiry date (`noExpiry`).
- `audit-failures` returns `audit`: the diagnostic settings, `auditLoggingToWorkspace`, and the failed
  operations (HTTP 400 or above) of the window grouped by operation and status.
- `performance` returns `performance`: `requests` (ServiceApiHit per interval) and `latency`
  (ServiceApiLatency average and maximum in ms), each with the intervals over `requestThreshold`
  (default 1000) or `latencyThresholdMs` (default 500).

## How to read the result

- `availability.hasData: false`, or `grainsMissing` above zero: Azure Monitor had no datapoint for
  those intervals. That is no information, not a healthy vault; `belowThreshold` is only about
  intervals that have a value. The whole window is covered, not just its last interval.
- `softDeleteEnabled: true` with a newer API version that reports nothing: soft delete is always on
  for vaults created since 2020, and only an explicit `false` means off. `purgeProtectionEnabled:
  false` is the common finding: purge protection is opt-in and cannot be turned off once on.
- `rbacAuthorization: false`: the data plane is governed by access policies, and `accessPolicyCount`
  says how many; a skill cannot show their content.
- `publicNetworkAccess: Enabled` with `networkAclDefaultAction: Allow` is an open vault.
- `expiry.items.items` empty with `scanned` counts above zero: nothing expires within the threshold.
  `scanned: null` for a kind: it could not be listed, and the run is `partial` with the reason scoped
  to `secrets`, `certificates` or `keys`. Item metadata is a data-plane read that Reader on the
  control plane does not include: grant Key Vault Reader (RBAC vaults) or list permission (access
  policy vaults).
- `audit.auditLoggingToWorkspace: false`: no diagnostic setting sends AuditEvent to a Log Analytics
  workspace, so there is nothing to query and `failures` is null. That is not "no failures": it is a
  finding about the vault's logging. `failures.items` empty with `workspacesQueried` above zero means
  no failed operation in the window. Log Analytics ingestion lags a few minutes.
- `performance.requests` or `.latency` null: that metric could not be read (the run is `partial`,
  error scoped `requests` or `latency`); the other stands. `crossings` counts intervals beyond the
  threshold, so a short spike is visible even when `last` is back to normal.
- Lists are cut at `maxItems` (default 200; `omitted` says how many), nested lists at 50, and text
  fields at 1024 characters (`truncated`).
- The run fails with a code, never an empty result, classified from az's own `ERROR:` lines (the exit
  code decides first; warnings az prints on a successful command are ignored):
  - `permission_denied`: `AuthorizationFailed`, `Forbidden`, an AADSTS code, no `az login`.
  - `unreachable`: cannot connect, DNS, TLS, and a vault firewall or private link refusing the
    runner (`ForbiddenByFirewall`, "Client address is not authorized").
  - `not_found`: the vault or subscription is gone. `rate_limited`: `TooManyRequests`, 429.
  - `timeout`: the command timeout. `dependency`: no az, or a resource provider (Microsoft.ResourceHealth)
    that is not registered. Anything else is `internal`.

## What to try next

- Findings in `configuration` (no purge protection, open network access): change them in Azure; this
  skill reads only. `azure:azure-base/discover` lists the subscription's other vaults.
- `expiry` shows an expired certificate or secret: rotate it, then run `expiring-items` again.
- `audit` shows 403s on an operation: the caller lacks a data-plane role on the vault; the
  `permission_denied` text of `expiring-items` names the action to grant.
- Latency or availability dips with `resource-health` Degraded: it is Azure's side; check back, or open
  a support case. With Available, look at the callers (`audit` for throttling, `performance.requests`).
- The workload reading the vault runs on Kubernetes: `kubernetes:k8s-workload-health/pod-status` on it.

## Tests

The plugin is stub-recorded: the fixtures were recorded against `tests/stub/bin/az`, a stand-in for
the az CLI that serves canned responses shaped after the CLI reference (`tests/stub/README.md`), not
against a real subscription. Re-record against a real account when a credential exists.
