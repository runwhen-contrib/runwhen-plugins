#!/usr/bin/env bash
# k8s-loki-health: wait for the two Lokis in rw-loki to reach the states the scenario needs:
# `loki` (StatefulSet) ready with an ACTIVE ring member, `loki-stale` (Deployment) running with
# /ready answering 503 and its ring member UNHEALTHY (it never heartbeats after registering, and
# the ring heartbeat timeout is 15s). Idempotent: only waits.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-loki
proxy() { # <pod> <path>
  "${K[@]}" get --raw "/api/v1/namespaces/${NS}/pods/http:$1:3100/proxy$2"
}

"${K[@]}" -n "$NS" rollout status sts/loki --timeout=240s
"${K[@]}" -n "$NS" rollout status deploy/loki-stale --timeout=240s
stale=$("${K[@]}" -n "$NS" get pods -l app.kubernetes.io/name=loki-stale -o jsonpath='{.items[0].metadata.name}')

end=$((SECONDS + 180))
until proxy loki-0 /ready 2>/dev/null | grep -qx ready; do
  (( SECONDS < end )) || { echo "loki-0 did not become ready in 180s" >&2; exit 1; }
  sleep 3
done
until proxy "$stale" /ring 2>/dev/null | grep -q '"state":"UNHEALTHY"'; do
  (( SECONDS < end )) || { echo "loki-stale ring member did not become UNHEALTHY in 180s" >&2; exit 1; }
  sleep 3
done
