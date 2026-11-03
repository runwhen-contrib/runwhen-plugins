#!/usr/bin/env bash
# k8s-jenkins-health: wait for the three python stubs in rw-jenkins to reach the states the
# scenario needs: `jenkins` ready with an empty queue, `jenkins-stuck` with a queue item and an
# offline node, `jenkins-down` answering 503 on every path, and `jenkins-locked` answering 403 (anonymous read off) on the JSON API while
# /login stays 200. Idempotent: only waits.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-jenkins
proxy() { # <pod> <path>
  "${K[@]}" get --raw "/api/v1/namespaces/${NS}/pods/http:$1:8080/proxy$2"
}

# not jenkins-down: its pod never becomes Ready, so its rollout never completes
for sts in jenkins jenkins-stuck jenkins-locked; do
  "${K[@]}" -n "$NS" rollout status "sts/${sts}" --timeout=240s
done

end=$((SECONDS + 120))
for pod in jenkins-0 jenkins-stuck-0 jenkins-locked-0; do
  until proxy "$pod" /login | grep -q 'Jenkins'; do
    (( SECONDS < end )) || { echo "${pod} /login did not answer in 120s" >&2; exit 1; }
    sleep 3
  done
done
# the down stub: /login is the proxy's 503 line (kubectl exits non-zero)
until [ "$("${K[@]}" -n "$NS" get pod jenkins-down-0 -o jsonpath='{.status.phase}')" = Running ]; do
  (( SECONDS < end )) || { echo "jenkins-down-0 did not start in 120s" >&2; exit 1; }
  sleep 3
done
if proxy jenkins-down-0 /login >/dev/null 2>&1; then
  echo "jenkins-down-0 answered /login; the scenario needs 503" >&2
  exit 1
fi
proxy jenkins-stuck-0 /queue/api/json | grep -q '"stuck": true' \
  || { echo "jenkins-stuck-0 queue has no stuck item" >&2; exit 1; }
proxy jenkins-stuck-0 /computer/api/json | grep -q '"offline": true' \
  || { echo "jenkins-stuck-0 has no offline node" >&2; exit 1; }
# the locked stub: kubectl exits non-zero with the backend's 403
if proxy jenkins-locked-0 /queue/api/json >/dev/null 2>&1; then
  echo "jenkins-locked-0 answered the queue; the scenario needs 403" >&2
  exit 1
fi
