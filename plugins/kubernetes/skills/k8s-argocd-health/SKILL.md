---
name: k8s-argocd-health
description: >-
  Health facts for an Argo CD Application (argoproj.io): sync status and the
  revision it was compared to, health status and message, conditions
  (ComparisonError, SyncError, InvalidSpecError), the last sync operation (phase,
  message, retry count, resources that failed to apply), the automated sync
  policy (prune, selfHeal), the source repositories and tracked revisions
  (multi-source aware), the tracked resources that are unhealthy or OutOfSync,
  and for Helm sources the chart, release name, value file names and the last
  deployed revision. Use when an Application is OutOfSync, Degraded, stuck
  Progressing or Missing, a sync failed, or you need to know what Argo CD last
  deployed.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/application]
  tags: [health, gitops, argocd, helm]
---

## When to use

The target is an Argo CD `Application` (the CR, in the namespace Argo CD watches,
usually `argocd`) and a deployment is not converging: it is OutOfSync, Degraded,
Progressing for too long, a sync failed, or someone asks which revision is live.
Start with `sync-status` for the overall picture, then `unhealthy-resources` for
which tracked objects are the problem, and `helm-source` when the source is a
Helm chart.

All three tasks read only the Application object (`get` on `applications` in
its own namespace; no ClusterRole, no Secrets, no logs, no events). They never
touch the workloads: those live in the destination namespace, which
`application.destination` names.

## What each task tells you

- `sync-status`: `sync` has the sync status (`Synced`, `OutOfSync`, `Unknown`)
  and the revision(s) it was compared to. `health` is Argo CD's rolled-up health
  and message. `conditions` are the Application's own conditions (type, message):
  `ComparisonError` means the desired state could not be computed (bad chart,
  missing values file, unreachable repository), `SyncError` a failed sync.
  `operation` is the last sync: phase (`Succeeded`, `Failed`, `Error`,
  `Running`), message, `startedAt`, `finishedAt`, `retryCount`, the revision(s)
  and `failures`, the resources the sync could not apply with Argo CD's message.
  `application` has the project, destination, `operationInProgress`, the sync
  policy (`automated`, `prune`, `selfHeal`, `retryLimit`) and `sourceTypes` as
  detected by Argo CD. `sources` lists each source: repository (credentials
  stripped), path or chart, tracked `targetRevision`, `ref`.
- `unhealthy-resources`: the entries of `status.resources` whose health is not
  `Healthy`, or that are `OutOfSync`, worst health first: group, kind,
  namespace, name, `syncStatus`, `health`, message, `requiresPruning` (live but
  no longer in git) and `hook`. `total` is everything Argo CD tracks;
  `unhealthyCount` and `outOfSyncCount` count before the `maxItems` cut.
- `helm-source`: `helm.sources` has, for each Helm source (a `helm` block or a
  `chart`), the chart or path, `targetRevision`, `releaseName`, the **names**
  of value files and parameters, and `hasInlineValues`. Values themselves are
  never returned. `lastDeployed.entry` is the newest `status.history` entry (id,
  revision(s), `deployedAt`, sources) and `historyCount` how many exist.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty and null.** `operation: null` means no sync operation was ever
  recorded. `conditions: []` means none are set. `unhealthy-resources` with
  `items: []` and `total: 0` means Argo CD recorded no resources (never
  compared, or a `ComparisonError`), which is not the same as all healthy; with
  `total` above zero it means every tracked resource is Healthy (or has no
  health check) and in sync. `helm.sources: []` and `isHelm: false` means no
  source is Helm. `lastDeployed.entry: null` means nothing was ever deployed.
- **Freshness.** `sync.reconciledAt` is when Argo CD last compared. Compare
  `sync.revision` with `lastDeployed.entry.revision`: they differ while a newer
  commit or chart version has been seen but not yet synced.
- **Cuts.** List outputs stop at `maxItems` (default 200); lists inside an entry
  (value files, failures, sources) stop at 50. Messages are cut at 1024
  characters and flagged `truncated`. No events are read, so there is no event
  look-back.
- **Normal states that look like failures.** `OutOfSync` with no `automated`
  policy is normal for a manually synced Application. `Progressing` is expected
  during a rollout; `Suspended` is a paused resource (a suspended CronJob).
  `Missing` is expected before the first sync. A resource with no `health` has
  no health check and is listed only when OutOfSync. A `Failed` operation with
  `retryCount` below `retryLimit` is still being retried. Hooks (`hook: true`)
  are short-lived. Argo CD may mark a resource OutOfSync for fields a controller
  rewrites (`ignoreDifferences` not set).
- **Path-based Helm.** A directory holding a `Chart.yaml` is rendered with Helm
  even without a `helm` block; Argo CD records that in `sourceTypes`, which
  `helm-source` also uses for `isHelm`, though then `sources` may be empty.

## What to try next

- The workloads are in `application.destination.namespace`. Read their state with
  `kubernetes:k8s-workload-health/rollout-status` and `pod-status`, and
  `kubernetes:k8s-namespace-health/unhealthy-pods` for the whole namespace.
- Application logs: this skill no longer scans pod logs for the Application (the
  classic task read pods in the destination namespace by an instance label). Run
  `kubernetes:k8s-workload-logs/error-patterns` or `recent-logs` on the
  Deployment, StatefulSet or DaemonSet named in `unhealthy-resources`.
- A `Degraded` Deployment: `kubernetes:k8s-workload-health/warning-events`.
- Fixing the sync (a refresh, a sync, a rollback) is a write on Argo CD and is
  not done here; `kubernetes:k8s-workload-ops/rollback` acts on a Deployment
  directly and Argo CD will revert it unless self-heal is off.
