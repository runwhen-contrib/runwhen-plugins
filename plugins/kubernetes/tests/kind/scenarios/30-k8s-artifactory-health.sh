#!/usr/bin/env bash
# k8s-artifactory-health: wait for the two nginx stubs in rw-artifactory to reach the states the
# scenario needs: `artifactory` ready with liveness, readiness and router health answering, and
# `artifactory-broken` running with readiness answering 503 (its pod is therefore not Ready and its
# Service has no endpoints). Idempotent: only waits.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-artifactory
proxy() { # <pod> <port> <path>
  "${K[@]}" get --raw "/api/v1/namespaces/${NS}/pods/http:$1:$2/proxy$3"
}
svc() { # <service> <port> <path>
  "${K[@]}" get --raw "/api/v1/namespaces/${NS}/services/http:$1:$2/proxy$3"
}

"${K[@]}" -n "$NS" rollout status sts/artifactory --timeout=240s
end=$((SECONDS + 240))
until "${K[@]}" -n "$NS" get pod artifactory-broken-0 -o jsonpath='{.status.phase}' | grep -qx Running; do
  (( SECONDS < end )) || { echo "artifactory-broken-0 did not start in 240s" >&2; exit 1; }
  sleep 3
done

until proxy artifactory-0 8081 /artifactory/api/v1/system/readiness | grep -qx OK; do
  (( SECONDS < end )) || { echo "artifactory-0 readiness is not OK in 240s" >&2; exit 1; }
  sleep 3
done
until proxy artifactory-0 8082 /router/api/v1/system/health | grep -q '"state":"HEALTHY"'; do
  (( SECONDS < end )) || { echo "artifactory-0 router health did not answer in 240s" >&2; exit 1; }
  sleep 3
done
until proxy artifactory-0 8082 /router/api/v1/system/readiness | grep -q '"code":"OK"'; do
  (( SECONDS < end )) || { echo "artifactory-0 router readiness did not answer in 240s" >&2; exit 1; }
  sleep 3
done
# the Service whose only port is unnamed with a numeric targetPort answers through its number
until svc artifactory-numeric 80 /artifactory/api/v1/system/liveness | grep -qx OK; do
  (( SECONDS < end )) || { echo "Service artifactory-numeric did not answer in 240s" >&2; exit 1; }
  sleep 3
done
# the broken stub: liveness answers, readiness is the proxy's 503 line (kubectl exits non-zero)
until proxy artifactory-broken-0 8081 /artifactory/api/v1/system/liveness | grep -q '"code":"OK"'; do
  (( SECONDS < end )) || { echo "artifactory-broken-0 liveness is not OK in 240s" >&2; exit 1; }
  sleep 3
done
if proxy artifactory-broken-0 8081 /artifactory/api/v1/system/readiness >/dev/null 2>&1; then
  echo "artifactory-broken-0 readiness answered 200; the scenario needs 503" >&2
  exit 1
fi
