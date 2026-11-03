#!/usr/bin/env bash
# k8s-litellm-health: wait for the three stub proxies in rw-litellm: `litellm` and `litellm-degraded`
# Ready, `litellm-draining` Running but not Ready (its readiness probe gets 503) with its Service
# endpoint reported not ready. Idempotent: only waits.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-litellm

"${K[@]}" -n "$NS" rollout status deploy/litellm --timeout=240s
"${K[@]}" -n "$NS" rollout status deploy/litellm-degraded --timeout=240s
"${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Running pod -l app.kubernetes.io/name=litellm-draining --timeout=240s
end=$((SECONDS + 120))
until [[ "$("${K[@]}" -n "$NS" get endpointslices -l kubernetes.io/service-name=litellm-draining \
  -o jsonpath='{.items[*].endpoints[*].conditions.ready}')" == "false" ]]; do
  (( SECONDS < end )) || { echo "litellm-draining endpoint did not become not-ready in 120s" >&2; exit 1; }
  sleep 3
done
