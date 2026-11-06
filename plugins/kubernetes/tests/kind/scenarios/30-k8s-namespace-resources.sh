#!/usr/bin/env bash
# rw-nsres: the VPA instances on the synthetic CRD, and the status a VPA
# recommender would write (there is no controller). `sized` has a recommendation
# well above its requests; `unsized` has one for a workload that sets no requests;
# `rollout` targets a kind this skill does not read and has none. Idempotent.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
"${K[@]}" wait --for=condition=Established crd/verticalpodautoscalers.autoscaling.k8s.io --timeout=60s
"${K[@]}" apply -f - <<'YAML'
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: sized
  namespace: rw-nsres
spec:
  targetRef: {apiVersion: apps/v1, kind: Deployment, name: sized}
  updatePolicy: {updateMode: "Off"}
---
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: unsized
  namespace: rw-nsres
spec:
  targetRef: {apiVersion: apps/v1, kind: Deployment, name: unsized}
  updatePolicy: {updateMode: Auto}
---
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: rollout
  namespace: rw-nsres
spec:
  targetRef: {apiVersion: argoproj.io/v1alpha1, kind: Rollout, name: nope}
YAML
"${K[@]}" -n rw-nsres patch verticalpodautoscalers.autoscaling.k8s.io sized \
  --subresource=status --type=merge -p '{
  "status": {
    "conditions": [{"type": "RecommendationProvided", "status": "True",
                    "lastTransitionTime": "2026-11-03T00:00:00Z"}],
    "recommendation": {"containerRecommendations": [{
      "containerName": "app",
      "lowerBound": {"cpu": "15m", "memory": "10Mi"},
      "target": {"cpu": "25m", "memory": "16Mi"},
      "uncappedTarget": {"cpu": "25m", "memory": "16Mi"},
      "upperBound": {"cpu": "100m", "memory": "64Mi"}}]}}}'
"${K[@]}" -n rw-nsres patch verticalpodautoscalers.autoscaling.k8s.io unsized \
  --subresource=status --type=merge -p '{
  "status": {
    "conditions": [{"type": "RecommendationProvided", "status": "True",
                    "lastTransitionTime": "2026-11-03T00:00:00Z"}],
    "recommendation": {"containerRecommendations": [{
      "containerName": "main",
      "lowerBound": {"cpu": "10m", "memory": "8Mi"},
      "target": {"cpu": "12m", "memory": "10Mi"},
      "uncappedTarget": {"cpu": "12m", "memory": "10Mi"},
      "upperBound": {"cpu": "50m", "memory": "32Mi"}}]}}}'

# Wait for the states the tests record: the good pods Running, bad-image pulling-failed.
"${K[@]}" -n rw-nsres wait --for=condition=Available deploy/sized deploy/unsized --timeout=180s
"${K[@]}" -n rw-nsres wait --for=condition=Ready pod/requests-only --timeout=180s
end=$((SECONDS + 180))
until "${K[@]}" -n rw-nsres get pods -l app=bad-image \
  -o jsonpath='{range .items[*].status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}' \
  | grep -qxE 'ImagePullBackOff|ErrImagePull'; do
  if (( SECONDS >= end )); then echo "TIMEOUT waiting for bad-image to fail its pull" >&2; exit 1; fi
  sleep 2
done
