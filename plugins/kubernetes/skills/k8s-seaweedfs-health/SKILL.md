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
  reads and deletes a probe object through the S3 gateway without credentials.
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
`filer`, `s3`; a value containing `all-in-one` is one process serving every role; a `filer` pod run
with `-s3` also serves S3). Ports are settings (9333, 8080, 8888, 8333).

All HTTP goes through `/api/v1/namespaces/<ns>/pods/<pod>:<port>/proxy/...`, so the identity needs
`pods/proxy` `get` (and `update`, `delete` for `s3-check`) plus `list` on pods in the namespace. The
API server prints **no body for a non-2xx answer**: a failure shows only as its status, never as
SeaweedFS's explanation.

## What each task tells you

- `cluster-status`: `release.workloads` (desired against ready, images; `unread` names kinds the
  identity may not list) and `release.pods` (roles, phase, ready, restarts, containers with
  `init: true` for init containers). `master`: per master pod `healthz`, `isLeader`, the `leader` it
  names and raft `peers` (null on a single master); `leaderCount` (1 is normal) and `leaderAgreement`.
- `volumes`: `slots` (max, free, used, per data node, `belowMinFree`), `layouts` (writable volume ids
  per collection / replication / TTL), `volumes` (read-only volumes, per-collection bytes, `garbageRatio`
  and compaction candidates at `garbageThreshold`), `disk` (each volume server's disks, `belowMinFree`).
- `filer-status`: per filer pod `healthz` and `rootReadable`; `volumeServers` each volume pod against
  the master's data nodes (`registered`), and `unmatchedDataNodes`.
- `buckets`: per collection with volumes: `usedBytes`, filer `quotaState`/`quotaBytes`/`quotaUsedPercent`,
  `extendedKeys` (names only), `anonymousList` (`allowed`, `denied`, `no_such_bucket`); `gateway.anonymousListBuckets`.
- `s3-check`: `probe` with the `plan`, each step and its error, `contentMatches`, `anonymousWrite`,
  `cleanedUp` and `leftBehind`. It sends no credentials and never reads Secrets: a gateway that
  refuses an anonymous write makes the task `skipped` (reason says so) with nothing written. Dry run
  lists the bucket (a read) and reports the plan.

## How to read the result

An empty list is not a healthy one: a namespace with no SeaweedFS pod is `not_found`, not an empty
release; `layouts.count: 0` means the master has no volume yet; `volumes.readOnly: []` with
`volumeCount > 0` means none is read-only. A `null` output part (for example `slots`) is a read that
failed, and the matching error says why. Lists stop at `maxItems`
(`truncated` says so); at most `maxPods` pods per role are asked.

A master pod that is not Running is listed with `queried: false` and a reason, and when none is
Running the result is `partial` with an `unreachable` error. A pod that answers a health endpoint
with an error status is `unhealthy`; any other proxy failure is an error, never `ok`. A release whose
masters cannot be asked fails `volumes`, `filer-status` and `buckets` as `unreachable` (they have
nothing else to report). `leaderCount: 0` while
masters were queried is a cluster with no leader; `2` or more is split brain. `peers: null` is a
master that does not report raft peers (a single master); it is not zero peers.

Normal states that look like failures: a volume that reached the size limit stops taking writes
without being read-only (it just leaves `writables`); an empty layout list on a fresh install;
`entryFound: false` for a collection whose bucket was never created in the filer; `quotaState:
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
- `s3-check` skipped: the gateway needs credentials; test with an S3 client that holds them.
