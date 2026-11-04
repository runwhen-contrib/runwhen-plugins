---
name: azure-aks-health
description: >-
  Control-plane and Azure Resource Manager health of one AKS cluster, read with az: Azure's own
  Resource Health verdict, provisioning and power state (Running or Stopped), Kubernetes version
  against the versions AKS offers in the region and the upgrades available, node pools (count,
  provisioning and power state, autoscaler bounds and whether the count sits at one), access and
  network settings, diagnostic settings, failed operations in the Activity Log over a window, and
  node CPU, memory and disk from Azure Monitor against thresholds. Use when a cluster is stuck
  updating or failed, an upgrade or node pool operation did not finish, a cluster may be stopped
  or no longer among the versions AKS offers, or the nodes look short of capacity. Anything inside the cluster (pods,
  nodes as Kubernetes sees them, events) belongs to the kubernetes plugin.
runwhen:
  role: health
  appliesTo:
    types: [azure/aks-cluster]
  tags: [health, aks, kubernetes, version, nodepool]
---

## When to use

A cluster will not finish an update or upgrade, shows `Failed`, is unreachable because it was
stopped, runs a version AKS is about to stop supporting, or a node pool did not scale. Each task
answers one question about the cluster the run targets; they read only, through the connection's
own az session (`azure/cli`), and return no credential, certificate or kubeconfig.

The target is `azure/subscriptions/<id>/resourcegroups/<group>/aks-clusters/<name>`. Every task looks
the cluster up first (`az aks show -g`): a cluster that is gone is `not_found`. The `commandTimeout`
setting (default 60s) bounds each az command; the metrics and activity tasks take a `window`
(default `24h`, 1m to 30d) that ends at the run's reference time.

This skill sees the cluster from Azure's side. It does not use the cluster's kubeconfig and cannot
see pods, Kubernetes nodes or events (D72): see "What to try next" for the kubernetes skills.

## What each task tells you

- `resource-health` returns `health`: `availabilityState` (Available, Degraded, Unavailable,
  Unknown), `healthy`, Azure's `summary` and `detailedStatus` (cut to 1024 characters), the reason,
  when it occurred and was reported, and the number of service-impacting events. It does not
  register Microsoft.ResourceHealth: if the subscription never did, the run fails with `dependency`.
- `cluster-status` returns `cluster`: `provisioningState` and `transitioning`, `powerState` and
  `running`, the requested and current Kubernetes version, `supportPlan`, `skuTier`, whether the API
  server is private and how many authorized IP ranges it has, local accounts, Entra and Azure RBAC,
  identity, network plugin, policy and outbound type, auto-upgrade channels, monitoring add-ons, and
  `diagnostics` (settings, whether one reaches Log Analytics, the log categories on).
- `node-pools` returns `nodePools`: per pool the `count`, `provisioningState`, `powerState`, `mode`,
  autoscaler `minCount`, `maxCount`, `atMaxCount`, `atMinCount`, the requested and current version
  (`versionDiffers`), VM size, Spot or Regular and zones; with the totals, `failedPools` and
  `notRunningPools`.
- `version-support` returns `version`: the cluster's minor against the minors AKS offers
  (`minorSupported`, `minorIsPreview`, `minorSupportPlans`, `planCoversMinor`, `minorsBehind`), whether the exact
  version is still offered (`patchOffered`) and the newest patch of the minor, and the control plane
  `upgrades` AKS offers (`available`, `versions`, `latestStable`).
- `activity-failures` returns `activity`: failed operations of the cluster's resource group that
  concern the cluster or a node pool, grouped by operation, target, sub-status and error code, with
  counts, first and last time and the error message Azure recorded; `eventsScanned`, `scanCut`.
- `node-metrics` returns `metrics`: `cpu`, `memory` and `disk`, each the node usage percentage per
  `metricInterval` (default 1h), average and busiest node, with the intervals with and without a
  value, `averageMax`, `peakMax`, the newest 50 intervals and how many go over the setting's
  threshold (`cpuThresholdPercent`, `memoryThresholdPercent`, `diskThresholdPercent`, default 80).

## How to read the result

- `provisioningState: Failed` or `Canceled` is final: the last operation did not complete, and
  `activity-failures` has its error. `transitioning: true` (Updating, Upgrading, Scaling, Starting,
  Stopping...) is in progress: read it again before concluding.
- `powerState: Stopped` with `provisioningState: Succeeded` is a cluster someone stopped, not a
  broken one. Its pools report `Stopped`, the metrics have no values and Resource Health may say so.
- `hasData: false`, or `grainsMissing` above zero: Azure Monitor had no datapoint for those
  intervals. That is no information, not a healthy cluster; `averageOverThreshold` is only about
  intervals that have a value. `lastGrainMissing: true`: `averageLast` is an older reading.
  `peakOverThreshold` is the busiest node: one hot node can cross while the average does not.
- `atMaxCount: true` (autoscaler on, count at `maxCount`): the autoscaler cannot add a node, so pods
  that need one stay Pending. `autoscaleEnabled: false` has no bounds (`minCount` and `maxCount` are
  null): the count is fixed. `versionDiffers: true` on a pool: it is on a different version from the
  one requested, as during an upgrade or after a failed one.
- `minorSupported: false`: the cluster's minor is not in the list AKS currently offers in the region.
  That is a fact about the list, not a stated end-of-support date. `planCoversMinor: false` on an
  offered minor: only a plan other than the cluster's (for example long-term support, while the
  cluster is on `KubernetesOfficial`) covers that minor. `minorSupported: null`: the offered list could not be read (the run is
  `partial`, error scoped `versions`). AKS gives no end-of-support date through the API, so the
  task reports none. `patchOffered: false` on a supported minor: the patch was retired, upgrade the
  patch. `upgrades.available: false` means AKS offers nothing newer from this version.
- `authorizedIpRangeCount: 0` with `privateCluster: false` means the API server accepts connections
  from any address. `localAccountsDisabled: false` means static admin credentials still work.
- `diagnostics: null`: the settings could not be read (scoped error `diagnostics`);
  `settingsCount: 0` means there are none, so no control plane log (kube-audit, kube-apiserver)
  is kept.
- `activity.failures.items` empty with `scanCut: false` means no failed operation of the cluster in
  the window. With `scanCut: true` the group had more failed events than `activityScanLimit`
  (default 500) and the list may be incomplete. The activity log is read per resource group, so
  `eventsForOtherResources` counts what belongs to its neighbours. The caller is not read.
  Activity log entries appear a few minutes after the operation.
- Lists are cut at `maxItems` (default 200; `omitted` says how many), nested lists at 50, and text
  fields at 1024 characters (`truncated`).
- The run fails with a code, never an empty result, classified from az's own `ERROR:` lines (the
  exit code decides first; warnings az prints on a successful command are ignored):
  `permission_denied` (`AuthorizationFailed`, `Forbidden`, an AADSTS code, no `az login`; the
  message names the action), `not_found` (the cluster or subscription is gone), `rate_limited`
  (`TooManyRequests`, 429), `unreachable` (cannot connect, DNS, TLS), `timeout`, `dependency` (no
  az, or Microsoft.ResourceHealth not registered), anything else `internal`. In `cluster-status`,
  `version-support`, `node-metrics` a secondary read that fails leaves its part null with the code
  scoped (`diagnostics`, `versions`, `upgrades`, `cpu`, `memory`, `disk`) and the run is `partial`.

## What to try next

- Pods Pending, nodes NotReady or workloads failing inside the cluster: those are Kubernetes
  questions. Run `kubernetes:k8s-node-health/node-conditions` and
  `kubernetes:k8s-node-health/node-utilization` for the nodes as Kubernetes sees them,
  `kubernetes:k8s-node-health/overcommit` when `node-pools` shows `atMaxCount: true`,
  `kubernetes:k8s-namespace-health/unhealthy-pods` and
  `kubernetes:k8s-workload-health/pod-status` for the pods. They need the cluster as a Kubernetes
  connection.
- `provisioningState: Failed`: `activity-failures` names the failed operation and its error code
  (a quota error is fixed in the subscription's quota, an extension error in the node image or
  network); then run `cluster-status` again after the fix. This skill reads only.
- `minorSupported: false` or `minorsBehind` above zero: plan the upgrade with `version-support`'s
  `upgrades.versions`, one minor at a time; do it in Azure (`az aks upgrade`), not here.
- CPU or memory over the threshold with `atMaxCount: true`: raise the pool's `maxCount` or add
  capacity; with the autoscaler off, scale the pool. `node-pools` shows the bounds.
- `resource-health` Degraded: it is Azure's side; check back or open a support case. With Available,
  read `activity-failures` and the Kubernetes skills.
- `azure:azure-base/discover` lists the subscription's clusters and other resources.

## Tests

The plugin is stub-recorded: the fixtures were recorded against `tests/stub/bin/az`, a stand-in for
the az CLI that serves canned answers (`tests/stub/README.md`), not against a real subscription. The
answers themselves were produced by the pinned az 2.92.0 running offline against the ARM bodies
Microsoft documents (`tests/stub/capture/capture_aks.sh`), so output formatting and error texts are
the CLI's own. Re-record against a real account when a credential exists.
