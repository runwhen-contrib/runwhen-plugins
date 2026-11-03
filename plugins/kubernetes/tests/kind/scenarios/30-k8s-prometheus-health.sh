#!/usr/bin/env bash
# k8s-prometheus-health: the synthetic Prometheus and ServiceMonitor instances, once the CRDs are
# Established (a manifest cannot create them in the same apply), and the wait for the stub
# Prometheus to be ready and to have scraped. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")

"${K[@]}" wait --for=condition=Established --timeout=60s \
  crd/prometheuses.monitoring.coreos.com crd/servicemonitors.monitoring.coreos.com >/dev/null
"${K[@]}" apply -f "${HERE}/k8s-prometheus-health-instances.yaml"
"${K[@]}" -n rw-prometheus rollout status deploy/prometheus-rw-prom --timeout=240s
"${K[@]}" -n rw-prometheus-apps rollout status deploy/prometheus-rw-prom-clean --timeout=240s
# wait for a scrape of the down target and an evaluation of the broken rule group
end=$((SECONDS + 120))
until "${K[@]}" get --raw '/api/v1/namespaces/rw-prometheus/services/http:prometheus-operated:web/proxy/api/v1/rules' \
    | grep -q '"health":"err"'; do
  (( SECONDS < end )) || { echo "prometheus stub: no failing rule after 120s" >&2; exit 1; }
  sleep 3
done
