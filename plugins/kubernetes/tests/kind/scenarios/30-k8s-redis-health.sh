#!/usr/bin/env bash
# k8s-redis-health: wait until the Redis workloads of rw-redis are up and the broken
# replica link is visible. Idempotent (only waits).
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}" -n rw-redis)

"${K[@]}" rollout status sts/redis --timeout=240s
"${K[@]}" rollout status deploy/redis-open --timeout=240s
"${K[@]}" rollout status deploy/redis-locked --timeout=240s
"${K[@]}" rollout status deploy/redis-sentinel --timeout=240s
"${K[@]}" rollout status deploy/redis-cluster --timeout=240s
# redis-orphan is ready as a process (its link to the missing master is what is down)
"${K[@]}" rollout status sts/redis-orphan --timeout=240s
# the replica of sts/redis has synced with its primary
until "${K[@]}" exec redis-1 -- sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli INFO replication' 2>/dev/null | grep -q 'master_link_status:up'; do
  sleep 2
done
