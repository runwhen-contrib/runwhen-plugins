#!/usr/bin/env bash
# Create (or reuse) the single-node kind cluster `rw-plugins`, apply the healthy
# and broken scenarios, wait until the broken states are reached, and write a
# kubeconfig for the rw-healthy-only viewer identity.
#
# Idempotent. Every kubectl call passes --context explicitly; this script never
# changes the current kube context.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER=rw-plugins
CTX="kind-${CLUSTER}"
FORBIDDEN_KUBECONFIG="${FORBIDDEN_KUBECONFIG:-/Users/samirpatil/Documents/runwhen-auto/envs/v13-lane-f/kind-forbidden.kubeconfig}"
K=(kubectl --context "$CTX")

log() { printf '[up.sh] %s\n' "$*"; }

# poll <timeout-seconds> <description> <command...>: retry until the command succeeds.
poll() {
  local timeout=$1 desc=$2; shift 2
  local end=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS >= end )); then
      log "TIMEOUT after ${timeout}s waiting for: ${desc}"
      return 1
    fi
    sleep 2
  done
  log "ok: ${desc}"
}

# waiting_reason <app-label> <reason...>: any pod of the app in rw-broken has a
# container waiting with one of the reasons.
waiting_reason() {
  local app=$1; shift
  local got
  got=$("${K[@]}" -n rw-broken get pods -l "app=${app}" \
    -o jsonpath='{range .items[*].status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}')
  local r
  for r in "$@"; do grep -qx "$r" <<<"$got" && return 0; done
  return 1
}

# crashloop: CrashLoopBackOff is only the visible status during backoff; between
# restarts the pod shows Error. Accept either once the container has restarted 3+ times.
crash_looping() {
  waiting_reason crashloop CrashLoopBackOff && return 0
  local n
  n=$("${K[@]}" -n rw-broken get pods -l app=crashloop \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
  (( ${n:-0} >= 3 ))
}

oom_killed() {
  "${K[@]}" -n rw-broken get pods -l app=oom \
    -o jsonpath='{range .items[*].status.containerStatuses[*]}{.lastState.terminated.reason}{"\n"}{end}' \
    | grep -qx OOMKilled
}

progress_stalled() {
  [[ "$("${K[@]}" -n rw-broken get deploy stuck-rollout \
    -o jsonpath='{.status.conditions[?(@.type=="Progressing")].status}')" == "False" ]]
}

# 1. Cluster
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "kind cluster ${CLUSTER} already exists, reusing"
else
  log "creating kind cluster ${CLUSTER}"
  # kind switches the current kube context to the new cluster; put it back so
  # an unrelated default context (often a shared cluster) stays the default.
  PREV_CTX=$(kubectl config current-context 2>/dev/null || true)
  kind create cluster --config "${HERE}/cluster.yaml" --wait 120s
  if [[ -n "$PREV_CTX" && "$PREV_CTX" != "$CTX" ]]; then
    kubectl config set current-context "$PREV_CTX" >/dev/null
    log "restored current-context to ${PREV_CTX}"
  fi
fi
"${K[@]}" cluster-info >/dev/null

# 2. Scenarios
"${K[@]}" apply -f "${HERE}/scenarios/00-namespaces.yaml"
"${K[@]}" apply -f "${HERE}/scenarios/10-healthy.yaml"
"${K[@]}" apply -f "${HERE}/scenarios/20-broken.yaml"
"${K[@]}" apply -f "${HERE}/rbac/forbidden.yaml"

# stuck-rollout: create with a good image, wait for it, then roll to a bad one.
# Done once only; re-applying would revert the image and restart the clock.
if ! "${K[@]}" -n rw-broken get deploy stuck-rollout >/dev/null 2>&1; then
  "${K[@]}" apply -f "${HERE}/scenarios/stuck-rollout.yaml"
  "${K[@]}" -n rw-broken rollout status deploy/stuck-rollout --timeout=240s
  "${K[@]}" -n rw-broken set image deploy/stuck-rollout app=nginx:does-not-exist-0.0.0
fi

# 3. Healthy state
"${K[@]}" -n rw-healthy wait --for=condition=Available deploy/web --timeout=300s
poll 300 "statefulset db ready" \
  "${K[@]}" -n rw-healthy wait --for=jsonpath='{.status.readyReplicas}'=1 sts/db --timeout=5s
"${K[@]}" -n rw-healthy wait --for=jsonpath='{.status.phase}'=Bound pvc/data --timeout=120s

# 4. Broken state
poll 240 "crashloop in CrashLoopBackOff (or restarted 3+ times)" crash_looping
poll 240 "bad-image in ImagePullBackOff/ErrImagePull" waiting_reason bad-image ImagePullBackOff ErrImagePull
poll 240 "oom lastState OOMKilled" oom_killed
poll 120 "stuck-rollout Progressing=False" progress_stalled
"${K[@]}" -n rw-broken wait --for=condition=Failed job/fails --timeout=240s

# 5. Forbidden kubeconfig (holds a token: lives outside the repo)
SERVER=$("${K[@]}" config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$("${K[@]}" config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
TOKEN=$("${K[@]}" -n rw-healthy create token rw-viewer --duration=8760h)
umask 077
cat > "$FORBIDDEN_KUBECONFIG" <<KCFG
apiVersion: v1
kind: Config
clusters:
  - name: ${CTX}-forbidden
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: rw-viewer
    user:
      token: ${TOKEN}
contexts:
  - name: ${CTX}-forbidden
    context:
      cluster: ${CTX}-forbidden
      user: rw-viewer
      namespace: rw-healthy
current-context: ${CTX}-forbidden
KCFG
log "wrote ${FORBIDDEN_KUBECONFIG}"
log "done"
