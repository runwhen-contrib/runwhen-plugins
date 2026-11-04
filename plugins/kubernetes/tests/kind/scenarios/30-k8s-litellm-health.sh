#!/usr/bin/env bash
# k8s-litellm-health: wait for the three stub proxies in rw-litellm: `litellm` Ready, `litellm-degraded`
# and `litellm-draining` Running but not Ready (their readiness probe gets 503) with their Service
# endpoints reported not ready. Idempotent: only waits.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-litellm

"${K[@]}" -n "$NS" rollout status deploy/litellm --timeout=240s
for stub in litellm-degraded litellm-draining; do
  "${K[@]}" -n "$NS" wait --for=jsonpath='{.status.phase}'=Running pod -l app.kubernetes.io/name=$stub --timeout=240s
  end=$((SECONDS + 120))
  until [[ "$("${K[@]}" -n "$NS" get endpointslices -l kubernetes.io/service-name=$stub \
    -o jsonpath='{.items[*].endpoints[*].conditions.ready}')" == "false" ]]; do
    (( SECONDS < end )) || { echo "$stub endpoint did not become not-ready in 120s" >&2; exit 1; }
    sleep 3
  done
done
