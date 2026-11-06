#!/usr/bin/env bash
# k8s-cronjob-health: wait (bounded) until the rw-cron CronJobs have reached the states the
# tests rely on. The CronJobs themselves are applied from 30-k8s-cronjob-health.yaml; the
# controller does the rest, one minute at a time. Idempotent: it only reads.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}" -n rw-cron)

poll() {
  local timeout=$1 desc=$2; shift 2
  local end=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS >= end )); then
      echo "[30-k8s-cronjob-health] TIMEOUT after ${timeout}s waiting for: ${desc}" >&2
      return 1
    fi
    sleep 3
  done
  echo "[30-k8s-cronjob-health] ok: ${desc}"
}

field() { "${K[@]}" get cronjob "$1" -o "jsonpath=$2"; }
nonempty() { [[ -n "$(field "$1" "$2")" ]]; }
failed_job() {
  "${K[@]}" get jobs -o jsonpath='{range .items[*]}{.metadata.ownerReferences[0].name}{" "}{.status.conditions[?(@.type=="Failed")].status}{"\n"}{end}' \
    | grep -qx 'always-fails True'
}
# forbid-long has had two schedule slots with one Job active: the second fire was skipped
forbid_skipped() {
  "${K[@]}" get events --field-selector reason=JobAlreadyActive -o name | grep -q .
}

poll 240 "ok-every-minute has a lastSuccessfulTime" nonempty ok-every-minute '{.status.lastSuccessfulTime}'
poll 240 "always-fails has a Failed Job" failed_job
poll 240 "forbid-long has an active Job" nonempty forbid-long '{.status.active[0].name}'
poll 240 "forbid-long skipped a run (JobAlreadyActive)" forbid_skipped
