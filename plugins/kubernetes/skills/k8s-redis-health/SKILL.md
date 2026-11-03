---
name: k8s-redis-health
description: >-
  Health facts for a Redis running as a Kubernetes Deployment or StatefulSet,
  read from Redis itself with redis-cli inside each ready pod: PING and its
  round trip in ms, role (primary or replica), connected replicas, the replica
  link (master_link_status), used memory against maxmemory, evicted keys,
  rejected connections, clients, and the last RDB and AOF status. A second task
  writes, reads back and deletes a time-stamped test key on one primary to prove
  writes work. Standalone and replication setups only (not cluster or Sentinel);
  Valkey is supported. Use when an application cannot reach or write to Redis, a
  replica has fallen behind, or memory is full. Never reads Secrets or prints a
  password.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment, kubernetes/statefulset]
  tags: [health, redis, cache, replication]
---

## When to use

The target is the Deployment or StatefulSet that runs Redis (pods whose container runs
`redis-server`) and the symptom is a Redis one: connection errors, `READONLY` errors from a
client, evictions, a replica out of sync, a full memory. Run `ping` first: it asks every ready pod.
Run `read-write-check` to prove the primary accepts writes.

Both tasks run `redis-cli` inside the container with `kubectl exec`, because Redis has no HTTP
API. That needs `pods/exec` in the target's namespace. A password-protected Redis works only when
the container's own environment holds the password: the skill tries `REDIS_PASSWORD`, then the
contents of the file `REDIS_PASSWORD_FILE` names, then `VALKEY_PASSWORD`, then the file
`VALKEY_PASSWORD_FILE` names, inside the pod. It never reads a Secret, never puts a password on a
command line and never prints one. A Redis that needs a password the container does not have is
`permission_denied`. Valkey (container named `valkey`, `valkey-cli` only) is supported but has not
been tried on kind.

What it does not cover, stated up front:

- Redis Cluster and Sentinel. `read-write-check` refuses cluster mode with `invalid_input`
  (`unsupported_mode`); a Sentinel container (no `role` in INFO) is `invalid_input` "not a Redis
  server" for both tasks. `ping` on a cluster node works and reports `mode: cluster`.
- A Redis that serves only TLS: plain `redis-cli` cannot talk to it.
- A port other than 6379: set the `port` input (passed as `redis-cli -p`).
- Bitnami's chart runs the master and the replicas as two StatefulSets (`<name>-master`,
  `<name>-replicas`): target the master one for `read-write-check`; on the replicas StatefulSet it
  fails `not_found` (every pod is a replica).

## What each task tells you

- `ping`: per ready pod, `ping.ok` (the reply was PONG), `ping.response`, and `ping.latencyMs`
  (the whole `kubectl exec`, so an upper bound that includes the exec round trip). `info` holds
  `replication` (`role`, `connectedReplicas`, `replicas` with state and lag, and on a replica
  `masterLinkStatus`, `masterLinkDownSinceSeconds`, `masterLastIoSecondsAgo`,
  `masterSyncInProgress`), `memory` (`usedBytes`, `maxBytes`, `usedPercent`, `overThreshold`
  against the `memoryPercentThreshold` setting, `maxmemoryPolicy`), `stats` (`evictedKeys`,
  `rejectedConnections`, both lifetime counters), `clients`, and `persistence` (`rdb` and `aof`
  last status). `mode` is Redis' own `redis_mode` (standalone, cluster). A pod that is not ready has `queried: false`,
  and so has one reached after the task's 45 second budget ran out (its `error` is `timeout`).
  `counts` totals the pods.
- `read-write-check`: writes to ONE primary, the first pod by name whose role is master:
  `check.primaries` has that one entry with the `set`, `get` and `del` steps (`ok`, `latencyMs`,
  `reply`), `valueMatches` (GET returned what was written) and `keyRemoved`. `check.otherPrimaries`
  names any other pod with role master: several masters without replication is itself worth
  flagging. `check.key` is `rw:healthcheck:<time of the run>`, set with `NX` and a 60 second expiry;
  a nil reply (the key already exists) is a failed step with reply `(nil)` and nothing is deleted.
  A run that proves nothing fails instead of reporting `ran: false`: no ready pod is `unreachable`,
  no pod with role master is `not_found`, cluster or Sentinel is `invalid_input`. A dry run says
  `reason: dry_run`. `candidates` lists each pod's role and mode. Writes, so it needs approval;
  run it as a dry run first to see the primary it would use.

## How to read the result

- A field that is `null` is one this Redis version or role does not report, never zero. On a
  primary the `master*` fields are null; `masterLinkDownSinceSeconds` is null when Redis reports -1
  (a link that has not come up since the pod started).
- `evictedKeys` and `rejectedConnections` are counters since the server started; a restart resets
  them. A non-zero `evictedKeys` with `maxmemoryPolicy` of `noeviction` cannot happen; with
  `allkeys-lru` it is the cache doing its job unless the hit rate is also falling.
- `usedPercent` is null when `maxBytes` is 0: no `maxmemory` is set, which is the Redis default.
  That is not "no limit": the container's memory limit still applies, and a Redis with no
  `maxmemory` is OOM-killed at it. Compare `usedBytes` with the container's limit
  (`kubernetes:k8s-workload-health/pod-restarts` shows OOMKilled).
- `role: slave` with `masterLinkStatus: down` is a replica cut off from its primary. A primary that
  lists fewer `replicas` than expected is the same fault seen from the other side.
- A pod in `instances` with `queried: false` is not ready: it was listed, not asked. An empty
  `instances` means the workload owns no pods (scaled to zero). A pod with an `error` could not be
  asked (the result is `partial` and an error names the pod and why).
- `info.persistence.loading: true` means the pod is still loading its dataset: PING answers an
  error reply (`ping.ok: false`) until it finishes.
- Lists stop at `maxItems` (default 200); `counts` is before the cap. Events are not read.

## What to try next

- Pods not ready, restarting or OOM-killed: `kubernetes:k8s-workload-health/pod-status`,
  `kubernetes:k8s-workload-health/pod-restarts`, and
  `kubernetes:k8s-workload-health/rollout-status`.
- The Redis log: `kubernetes:k8s-workload-logs/recent-logs`.
- Data-volume problems behind a failed write or persistence status:
  `kubernetes:k8s-pvc-health/binding-status` and `usage`.
- A stuck rollout or crash loop that needs a restart: `kubernetes:k8s-workload-ops/rollout-restart`.
