---
name: k8s-seaweedfs-health
description: >-
  Health facts for a SeaweedFS release running in Kubernetes (the Helm chart's
  master, volume, filer and S3 components, or its all-in-one pod): workload and
  pod readiness, master health and raft leadership, volume slots free against
  max, writable volumes per collection/replication/TTL layout, read-only volumes,
  volume server disk usage, filer health and whether the volume servers are
  registered with the master, per-bucket usage and quota, and whether an
  unauthenticated S3 caller can list buckets. One write task, s3-check, puts,
  reads and deletes a probe object in an existing bucket through the S3 gateway
  without credentials (it never creates or deletes a bucket).
  HTTP goes through the API server's pod proxy (no port-forward, no exec). Use
  when uploads fail, a volume fills or goes read-only, or storage looks wrong.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/namespace, kubernetes/statefulset]
  tags: [health, storage, seaweedfs, s3, volumes]
---

## When to use

An application cannot write to SeaweedFS (or its S3 endpoint), a volume server is down, or
capacity is in doubt. Start with `cluster-status` (are the pods up, is there one leader), then
`volumes` (slots, read-only volumes, disks), `filer-status` and `buckets`. `s3-check` is the only
task that writes.

Targets: a **StatefulSet** of the release (its `app.kubernetes.io/instance` label names the release)
or the **namespace** (it must hold one release, or pass `release`). Pods are found by
`app.kubernetes.io/name=seaweedfs` and the `app.kubernetes.io/component` label (`master`, `volume`,
`filer`, `s3`; a value containing `all-in-one` is one process serving master, volume and filer). A
pod serves S3 when its component is `s3`, or when its command or args enable it: `-s3`, `--s3`,
`-s3=true` or `-s3.port=...`, also inside a shell-form string such as `/bin/sh -ec "exec weed filer
... -s3 ..."`. An all-in-one or filer pod without such a flag serves no S3. Ports are settings (9333,
8080, 8888, 8333).

All HTTP goes through the API server's pod proxy, so the identity needs `pods/proxy` `get` (and
`update`, `delete` for `s3-check`) plus `list` on pods in the namespace. The reads (every GET) use
`ctx.k8s_proxy_get`; the `s3-check` PUT and DELETE use `ctx.k8s_proxy_request`. Both return the
workload's own status and body (a 403, 404 or 503 is data) and never follow a redirect: a redirect to
another host is reported as a `dependency` error naming the host, and no request is re-sent.

## What each task tells you

- `cluster-status`: `release.workloads` (desired against ready, images; `unread` names kinds the
  identity may not list) and `release.pods` (roles, phase, ready, restarts, containers with
  `init: true` for init containers). `master`: per master pod `healthz`, `isLeader` (false for a
  follower, which omits it), the `leader` it names (null when it names none: a fact) and raft `peers`
  ([] on a single master); `leaderCount` (1 is normal) and `leaderAgreement`. A member whose
  `/cluster/status` failed is `queried: false` with `notQueriedReason` set to the error code.
- `volumes`: `slots` (max, free, used, per data node, `belowMinFree`), `layouts` (writable volume ids
  per collection / replication / TTL), `volumes` (read-only volumes, per-collection bytes, `garbageRatio`
  and compaction candidates at `garbageThreshold`), `disk` (each volume server's disks, `belowMinFree`).
- `filer-status`: per filer pod `healthz` and `rootReadable`; `filer.filerPods: 0` is a release with no
  filer pod (the task is still `ok`); `volumeServers` each volume pod against the master's data nodes
  (`registered`: the data node's host is the pod IP, pod name or hostname, IPv6 brackets stripped), and
  `unmatchedDataNodes`.
- `buckets`: buckets are approximated by collections that have volumes. Missed: empty buckets (no
  volume yet), buckets of a filer group (another collection name) and erasure-coded volumes. Per
  collection: `usedBytes` (live bytes, `Size - DeletedByteCount`, the quota's basis), filer
  `quotaState`/`quotaBytes`/`quotaUsedPercent`, `extendedKeys` (names only), `anonymousList` (`allowed`,
  `denied`, `no_such_bucket`); `gateway.anonymousListBuckets`.
- `s3-check`: one object PUT, read back, DELETEd and confirmed gone in the **existing** `bucket` input
  (default `runwhen-s3-check`). It never creates or deletes a bucket: a bucket that does not exist makes
  the task `skipped` ("create it to enable the write check"), as does a release with no S3 gateway pod.
  `probe`: `bucketListing` (`allowed`, or `denied`: a write-only anonymous identity is possible, so the
  write is still tried), the `plan`, each step and its error, `contentMatches`, `anonymousWrite`,
  `passed`, `cleanedUp` and `leftBehind`. `passed` is true only when the PUT, read-back, DELETE and
  confirmation all worked and nothing is left; false when any of them failed (each also an error with
  scope `s3-check/<step>`, so the result is `partial`). `anonymousWrite: true` means an unauthenticated
  write was accepted from the pod network (the API-server proxy strips Authorization) and the read-back
  matched; it says nothing about clients outside the cluster. It sends no credentials and never reads
  Secrets: a gateway that refuses an anonymous write makes the task `skipped` with nothing written. The
  key is `runwhen-s3-check/<time to the microsecond>-<6 hex>.txt`. The first write to a bucket with no
  volume makes the master grow a batch of volumes that stay (slots taken) after the object is gone. The run is bounded to 50s and always
  keeps time to DELETE. Dry run lists the bucket (a read) and reports the plan.

## How to read the result

An empty list is not a healthy one: a namespace with no SeaweedFS pod is `not_found`, not an empty
release; `layouts.count: 0` means the master has no volume yet; `volumes.readOnly: []` with
`volumeCount > 0` means none is read-only. A `null` output part (for example `slots`) is a read that
failed, and the matching error says why. Lists stop at `maxItems`
(`truncated` says so); at most `maxPods` pods per role are asked.

A master pod that is not Running is listed with `queried: false` and a reason, and when none is
Running the result is `partial` with an `unreachable` error. A pod that answers a health endpoint
with another non-2xx status (400, 500, 503 ...: the workload's own, the helper tells it from the API
server's) is `unhealthy`; an API-server error (the pod unreachable or missing, a timeout, rate limiting)
and a redirect to another host (never followed) are errors, never `ok` and never `unhealthy`. A release whose
masters cannot be asked fails `volumes`, `filer-status` and `buckets` as `unreachable` (they have
nothing else to report). `leaderCount: 0` while
masters were queried is a cluster with no leader (members then show `leader: null`); `2` or more is
split brain. `peers: []` is a master that reports no raft peers (a single master).

Normal states that look like failures: a volume that reached the size limit stops taking writes
without being read-only (it just leaves `writables`); an empty layout list on a fresh install;
`entryFound: false` for a collection whose bucket was never created in the filer; `filer.filerPods: 0` is a release without a
filer; `quotaState:
disabled` is a quota set but not enforced; a `filer` pod without `-s3` serves no S3 (`gateway.podCount: 0`).
A volume server's disk figures are the filesystem its data dir sits on, not a SeaweedFS quota.

`anonymousList: allowed` or `anonymousListBuckets: allowed` is a fact about the gateway: some charts
ship an open S3 (no identities, or an `anonymous` identity with read), so objects are readable by anyone
who can reach it. Whether that is intended is for you to say.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status` and `rollout-status` on the
  StatefulSet, `kubernetes:k8s-workload-logs/recent-logs` for the master/volume logs, and
  `kubernetes:k8s-pvc-health/usage` for the data PVCs (`disk` is the node filesystem of the data dir).
- `slots.belowMinFree` or `readOnly` volumes: free space (disk), add volume servers or raise
  `-volume.max`; a read-only volume can be reopened with `weed shell` `volume.mark -writable`.
- `volumeServers.registered: false`: the volume server cannot reach the master; check its logs and the
  master address it was started with.
- `s3-check` skipped: the bucket does not exist (create it), or the gateway needs credentials (test with
  an S3 client that holds them), or the release serves no S3.
