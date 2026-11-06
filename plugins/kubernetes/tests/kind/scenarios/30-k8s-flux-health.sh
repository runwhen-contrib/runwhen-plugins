#!/usr/bin/env bash
# k8s-flux-health: the Flux objects in rw-flux and the status a controller would set.
# CRs are applied once the CRDs are established; status goes through the status
# subresource (a merge patch replaces `conditions` whole). Idempotent: re-running
# resets spec.suspend (set explicitly) and the status below.
#
# HelmRelease (helm.toolkit.fluxcd.io/v2):
#   podinfo-ok         Ready, applied == attempted (chart 6.5.1), history[0] deployed
#   podinfo-failed     Ready False (UpgradeFailed), Stalled True, attempted 6.6.0 but 6.5.1 still deployed
#                      (history[0] failed); dependsOn podinfo-ok, a missing release, and one in rw-flux-other
#   podinfo-suspended  spec.suspend true
#   podinfo-fresh      no status at all (a controller has not looked yet)
# Kustomization (kustomize.toolkit.fluxcd.io/v1):
#   apps-ok            Ready, applied == attempted
#   apps-broken        Ready False (BuildFailed), Stalled True, no Reconciling condition,
#                      attempted != applied; dependsOn apps-ok and one in rw-flux-other
#   apps-suspended     spec.suspend true
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")

"${K[@]}" wait --for=condition=Established --timeout=60s \
  crd/helmreleases.helm.toolkit.fluxcd.io crd/kustomizations.kustomize.toolkit.fluxcd.io >/dev/null

"${K[@]}" apply -f - <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: podinfo-ok, namespace: rw-flux}
spec:
  interval: 10m
  suspend: false
  chart:
    spec:
      chart: podinfo
      version: 6.5.x
      sourceRef: {kind: HelmRepository, name: podinfo}
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: podinfo-failed, namespace: rw-flux}
spec:
  interval: 10m
  suspend: false
  chart:
    spec:
      chart: podinfo
      version: 6.6.0
      sourceRef: {kind: HelmRepository, name: podinfo}
  dependsOn:
    - {name: podinfo-ok}
    - {name: not-here}
    - {name: infra, namespace: rw-flux-other}
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: podinfo-suspended, namespace: rw-flux}
spec:
  interval: 10m
  suspend: true
  chart:
    spec:
      chart: podinfo
      version: 6.5.x
      sourceRef: {kind: HelmRepository, name: podinfo}
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: podinfo-fresh, namespace: rw-flux}
spec:
  interval: 10m
  suspend: false
  chartRef: {kind: OCIRepository, name: podinfo}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: apps-ok, namespace: rw-flux}
spec:
  interval: 10m
  suspend: false
  path: ./apps
  prune: true
  sourceRef: {kind: GitRepository, name: fleet}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: apps-broken, namespace: rw-flux}
spec:
  interval: 10m
  suspend: false
  path: ./apps/broken
  prune: true
  sourceRef: {kind: GitRepository, name: fleet, namespace: flux-system}
  dependsOn:
    - {name: apps-ok}
    - {name: infra, namespace: rw-flux-other}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: apps-suspended, namespace: rw-flux}
spec:
  interval: 10m
  suspend: true
  path: ./apps
  prune: true
  sourceRef: {kind: GitRepository, name: fleet}
YAML

patch_status() { # <resource> <name> <json>
  "${K[@]}" -n rw-flux patch "$1" "$2" --subresource=status --type=merge -p "$3" >/dev/null
}
HR=helmreleases.helm.toolkit.fluxcd.io
KS=kustomizations.kustomize.toolkit.fluxcd.io

patch_status $HR podinfo-ok '{"status":{
  "observedGeneration":1,
  "lastHandledReconcileAt":"2026-11-03T20:00:00Z",
  "lastAttemptedRevision":"6.5.1",
  "history":[{"name":"podinfo-ok","namespace":"rw-flux","version":2,"chartName":"podinfo","chartVersion":"6.5.1","appVersion":"6.5.1","status":"deployed","lastDeployed":"2026-11-03T20:00:05Z"},
             {"name":"podinfo-ok","namespace":"rw-flux","version":1,"chartName":"podinfo","chartVersion":"6.5.0","appVersion":"6.5.0","status":"superseded","lastDeployed":"2026-11-02T20:00:05Z"}],
  "conditions":[
    {"type":"Ready","status":"True","reason":"UpgradeSucceeded","message":"Helm upgrade succeeded for release rw-flux/podinfo-ok.v2 with chart podinfo@6.5.1","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:00:05Z"},
    {"type":"Released","status":"True","reason":"UpgradeSucceeded","message":"Helm upgrade succeeded for release rw-flux/podinfo-ok.v2 with chart podinfo@6.5.1","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:00:05Z"}]}}'

patch_status $HR podinfo-failed '{"status":{
  "observedGeneration":1,
  "lastHandledReconcileAt":"2026-11-03T20:05:00Z",
  "lastAttemptedRevision":"6.6.0",
  "failures":5,
  "upgradeFailures":5,
  "history":[{"name":"podinfo-failed","namespace":"rw-flux","version":3,"chartName":"podinfo","chartVersion":"6.6.0","appVersion":"6.6.0","status":"failed","lastDeployed":"2026-11-03T20:05:00Z"},
             {"name":"podinfo-failed","namespace":"rw-flux","version":2,"chartName":"podinfo","chartVersion":"6.5.1","appVersion":"6.5.1","status":"deployed","lastDeployed":"2026-11-03T19:00:00Z"}],
  "conditions":[
    {"type":"Ready","status":"False","reason":"UpgradeFailed","message":"Helm upgrade failed for release rw-flux/podinfo-failed with chart podinfo@6.6.0: timed out waiting for the condition","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:05:00Z"},
    {"type":"Released","status":"False","reason":"UpgradeFailed","message":"Helm upgrade failed: timed out waiting for the condition","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:05:00Z"},
    {"type":"Stalled","status":"True","reason":"RetriesExceeded","message":"Failed to upgrade after 5 attempt(s)","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:05:00Z"}]}}'

patch_status $HR podinfo-suspended '{"status":{
  "observedGeneration":1,
  "lastAttemptedRevision":"6.5.1",
  "history":[{"name":"podinfo-suspended","namespace":"rw-flux","version":1,"chartName":"podinfo","chartVersion":"6.5.1","appVersion":"6.5.1","status":"deployed","lastDeployed":"2026-11-03T19:00:00Z"}],
  "conditions":[
    {"type":"Ready","status":"True","reason":"InstallSucceeded","message":"Helm install succeeded","observedGeneration":1,"lastTransitionTime":"2026-11-03T19:00:00Z"}]}}'

patch_status $KS apps-ok '{"status":{
  "observedGeneration":1,
  "lastHandledReconcileAt":"2026-11-03T20:00:00Z",
  "lastAppliedRevision":"main@sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "lastAttemptedRevision":"main@sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "inventory":{"entries":[{"id":"rw-flux_cfg__ConfigMap","v":"v1"},{"id":"rw-flux_app_apps_Deployment","v":"v1"}]},
  "conditions":[
    {"type":"Ready","status":"True","reason":"ReconciliationSucceeded","message":"Applied revision: main@sha1:aaaaaaaa","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:00:00Z"}]}}'

patch_status $KS apps-broken '{"status":{
  "observedGeneration":1,
  "lastHandledReconcileAt":"2026-11-03T20:05:00Z",
  "lastAppliedRevision":"main@sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "lastAttemptedRevision":"main@sha1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "conditions":[
    {"type":"Ready","status":"False","reason":"BuildFailed","message":"kustomize build failed: accumulating resources: missing kustomization file","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:05:00Z"},
    {"type":"Stalled","status":"True","reason":"BuildFailed","message":"kustomize build failed: accumulating resources: missing kustomization file","observedGeneration":1,"lastTransitionTime":"2026-11-03T20:05:00Z"}]}}'

patch_status $KS apps-suspended '{"status":{
  "observedGeneration":1,
  "lastAppliedRevision":"main@sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "lastAttemptedRevision":"main@sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "conditions":[
    {"type":"Ready","status":"True","reason":"ReconciliationSucceeded","message":"Applied revision: main@sha1:aaaaaaaa","observedGeneration":1,"lastTransitionTime":"2026-11-03T19:00:00Z"}]}}'
