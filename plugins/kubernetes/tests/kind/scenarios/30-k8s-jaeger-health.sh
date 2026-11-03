#!/usr/bin/env bash
# k8s-jaeger-health: wait for Jaeger, make sure the scenario spans are in its in-memory store
# (a restart of the pod empties it: re-run the emit Job then), and wait for them to be queryable.
# Idempotent.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-jaeger
query() { # <path-and-query>
  "${K[@]}" get --raw "/api/v1/namespaces/${NS}/services/http:jaeger-query:16686/proxy$1"
}

"${K[@]}" -n "$NS" rollout status deploy/jaeger --timeout=240s
if ! query /api/services 2>/dev/null | grep -q '"checkout"'; then
  "${K[@]}" -n "$NS" delete job emit-spans --ignore-not-found --wait=true
  "${K[@]}" apply -f "$(dirname "${BASH_SOURCE[0]}")/30-k8s-jaeger-health.yaml"
fi
"${K[@]}" -n "$NS" wait --for=condition=complete job/emit-spans --timeout=240s
end=$((SECONDS + 120))
until query /api/services 2>/dev/null | grep -q '"gateway"'; do
  (( SECONDS < end )) || { echo "scenario spans did not reach jaeger in 120s" >&2; exit 1; }
  sleep 3
done
