#!/usr/bin/env bash
# k8s-argocd-health: Application instances in rw-argo and the status Argo CD
# would write (nothing reconciles them; the CRD is the synthetic one from
# 30-k8s-argocd-health.yaml). Idempotent: apply, then merge-patch the status
# subresource with fixed values.
#   web         Synced / Healthy, a plain git path
#   helm-web    Synced / Healthy, a Helm chart (two releases in history)
#   drifted     OutOfSync / Degraded: a Degraded Deployment, an OutOfSync Service, manual sync
#   failed-sync OutOfSync / Missing: multi-source Helm, ComparisonError, failed sync (retried 5x)
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
"${K[@]}" wait --for=condition=Established crd/applications.argoproj.io --timeout=60s

"${K[@]}" apply -f - <<'YAML'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
  namespace: rw-argo
spec:
  project: default
  destination: {server: "https://kubernetes.default.svc", namespace: web}
  source: {repoURL: "https://git.example.test/org/web.git", path: deploy/web, targetRevision: main}
  syncPolicy:
    automated: {prune: true, selfHeal: true}
    syncOptions: [CreateNamespace=true]
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: helm-web
  namespace: rw-argo
spec:
  project: default
  destination: {server: "https://kubernetes.default.svc", namespace: helm-web}
  source:
    repoURL: https://charts.example.test
    chart: nginx
    targetRevision: 15.x
    helm:
      releaseName: web
      valueFiles: [values-prod.yaml]
      parameters:
        - {name: image.tag, value: v1.2.3}
      valuesObject: {replicaCount: 2}
  syncPolicy:
    automated: {}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: drifted
  namespace: rw-argo
spec:
  project: default
  destination: {name: in-cluster, namespace: shop}
  source: {repoURL: "https://git.example.test/org/shop.git", path: deploy, targetRevision: HEAD}
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: failed-sync
  namespace: rw-argo
spec:
  project: platform
  destination: {server: "https://kubernetes.default.svc", namespace: api}
  sources:
    - repoURL: https://charts.example.test
      chart: api
      targetRevision: 3.x
      helm:
        releaseName: api
        valueFiles: ["$values/envs/prod/values.yaml", values-extra.yaml]
        parameters:
          - {name: image.tag, value: v3.1.0}
    - repoURL: "https://git.example.test/org/config.git"
      targetRevision: main
      ref: values
  syncPolicy:
    automated: {prune: true, selfHeal: true}
    retry: {limit: 5}
YAML

patch() { # patch <app> <status-json>
  "${K[@]}" -n rw-argo patch application "$1" --subresource=status --type=merge \
    -p "{\"status\": $2}"
}

patch web '{
  "reconciledAt": "2026-11-03T10:00:05Z", "sourceType": "Directory",
  "sync": {"status": "Synced", "revision": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"},
  "health": {"status": "Healthy"},
  "operationState": {"phase": "Succeeded", "message": "successfully synced (all tasks run)",
    "startedAt": "2026-11-03T10:00:00Z", "finishedAt": "2026-11-03T10:00:04Z",
    "syncResult": {"revision": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "resources": [
      {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "web", "name": "web", "status": "Synced", "message": "deployment.apps/web configured"}]}},
  "resources": [
    {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "web", "name": "web", "status": "Synced", "health": {"status": "Healthy"}},
    {"version": "v1", "kind": "Service", "namespace": "web", "name": "web", "status": "Synced", "health": {"status": "Healthy"}},
    {"version": "v1", "kind": "ConfigMap", "namespace": "web", "name": "web-config", "status": "Synced"}],
  "history": [{"id": 0, "revision": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "deployedAt": "2026-11-03T10:00:04Z", "deployStartedAt": "2026-11-03T10:00:00Z",
    "source": {"repoURL": "https://git.example.test/org/web.git", "path": "deploy/web", "targetRevision": "main"}}]
}'

patch helm-web '{
  "reconciledAt": "2026-11-03T11:00:05Z", "sourceType": "Helm",
  "sync": {"status": "Synced", "revision": "15.2.0"},
  "health": {"status": "Healthy"},
  "operationState": {"phase": "Succeeded", "message": "successfully synced (all tasks run)",
    "startedAt": "2026-11-03T11:00:00Z", "finishedAt": "2026-11-03T11:00:04Z",
    "operation": {"initiatedBy": {"automated": true}},
    "syncResult": {"revision": "15.2.0", "resources": []}},
  "resources": [
    {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "helm-web", "name": "web-nginx", "status": "Synced", "health": {"status": "Healthy"}},
    {"version": "v1", "kind": "Service", "namespace": "helm-web", "name": "web-nginx", "status": "Synced", "health": {"status": "Healthy"}}],
  "history": [
    {"id": 0, "revision": "15.1.0", "deployedAt": "2026-11-02T09:00:04Z", "deployStartedAt": "2026-11-02T09:00:00Z",
     "source": {"repoURL": "https://charts.example.test", "chart": "nginx", "targetRevision": "15.x", "helm": {"valueFiles": ["values-prod.yaml"]}}},
    {"id": 1, "revision": "15.2.0", "deployedAt": "2026-11-03T11:00:04Z", "deployStartedAt": "2026-11-03T11:00:00Z",
     "initiatedBy": {"automated": true},
     "source": {"repoURL": "https://charts.example.test", "chart": "nginx", "targetRevision": "15.x", "helm": {"valueFiles": ["values-prod.yaml"]}}}]
}'

patch drifted '{
  "reconciledAt": "2026-11-03T12:00:05Z", "sourceType": "Directory",
  "sync": {"status": "OutOfSync", "revision": "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"},
  "health": {"status": "Degraded", "message": "Deployment shop exceeded its progress deadline"},
  "conditions": [{"type": "SharedResourceWarning", "message": "Service/shop is part of applications rw-argo/web and rw-argo/drifted", "lastTransitionTime": "2026-11-03T11:30:00Z"}],
  "operationState": {"phase": "Succeeded", "message": "successfully synced (all tasks run)",
    "startedAt": "2026-11-03T09:00:00Z", "finishedAt": "2026-11-03T09:00:06Z",
    "syncResult": {"revision": "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567", "resources": []}},
  "resources": [
    {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "shop", "name": "shop", "status": "Synced", "health": {"status": "Degraded", "message": "Deployment shop exceeded its progress deadline"}},
    {"version": "v1", "kind": "Service", "namespace": "shop", "name": "shop", "status": "OutOfSync", "health": {"status": "Healthy"}},
    {"group": "networking.k8s.io", "version": "v1", "kind": "Ingress", "namespace": "shop", "name": "shop", "status": "Synced", "health": {"status": "Progressing", "message": "Waiting for LoadBalancer to be ready"}},
    {"version": "v1", "kind": "ConfigMap", "namespace": "shop", "name": "shop-config", "status": "OutOfSync", "requiresPruning": true},
    {"version": "v1", "kind": "Secret", "namespace": "shop", "name": "shop-tls", "status": "Synced"}],
  "history": [{"id": 0, "revision": "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567", "deployedAt": "2026-11-03T09:00:06Z", "deployStartedAt": "2026-11-03T09:00:00Z",
    "source": {"repoURL": "https://git.example.test/org/shop.git", "path": "deploy", "targetRevision": "HEAD"}}]
}'

patch failed-sync '{
  "reconciledAt": "2026-11-03T13:00:05Z", "sourceTypes": ["Helm", "Directory"],
  "sync": {"status": "OutOfSync", "revisions": ["3.1.0", "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"]},
  "health": {"status": "Missing"},
  "conditions": [
    {"type": "ComparisonError", "message": "Failed to load target state: failed to generate manifest for source 1 of 2: rpc error: code = Unknown desc = helm template . failed: Error: values file envs/prod/values.yaml not found", "lastTransitionTime": "2026-11-03T12:58:00Z"},
    {"type": "SyncError", "message": "Failed sync attempt: one or more objects failed to apply", "lastTransitionTime": "2026-11-03T12:59:00Z"}],
  "operationState": {"phase": "Failed", "message": "one or more objects failed to apply, reason: admission webhook \"policy.example.test\" denied the request: containers must set resource limits",
    "startedAt": "2026-11-03T12:57:00Z", "finishedAt": "2026-11-03T12:59:00Z", "retryCount": 5,
    "operation": {"initiatedBy": {"automated": true}},
    "syncResult": {"revisions": ["3.1.0", "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"], "resources": [
      {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "api", "name": "api", "status": "SyncFailed", "message": "admission webhook \"policy.example.test\" denied the request: containers must set resource limits", "syncPhase": "Sync"},
      {"version": "v1", "kind": "Service", "namespace": "api", "name": "api", "status": "Synced", "message": "service/api created"}]}},
  "resources": [
    {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "api", "name": "api", "status": "OutOfSync", "health": {"status": "Missing"}, "requiresPruning": false},
    {"version": "v1", "kind": "Service", "namespace": "api", "name": "api", "status": "Synced", "health": {"status": "Healthy"}}],
  "history": [{"id": 0, "revisions": ["3.0.0", "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"], "deployedAt": "2026-11-02T08:00:04Z", "deployStartedAt": "2026-11-02T08:00:00Z",
    "initiatedBy": {"automated": true},
    "sources": [
      {"repoURL": "https://charts.example.test", "chart": "api", "targetRevision": "3.x", "helm": {"valueFiles": ["$values/envs/prod/values.yaml", "values-extra.yaml"]}},
      {"repoURL": "https://git.example.test/org/config.git", "targetRevision": "main", "ref": "values"}]}]
}'
