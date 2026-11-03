---
name: k8s-vault-health
description: >-
  Health facts for HashiCorp Vault running in Kubernetes. server-status asks
  every Running Vault pod its own unauthenticated /v1/sys/health through the
  API server's pod proxy (no port-forward, no exec, no token) and reports
  whether it is sealed, initialized, standby, a performance standby or a DR
  secondary, its version, cluster name and replication modes, next to the
  StatefulSet's ready replicas. csi-provider-status reads the Vault CSI provider
  DaemonSet: scheduled, ready and updated counts, container restarts and Warning
  events. Use when applications cannot read secrets, a Vault pod is not ready, a
  restart left Vault sealed, or the CSI provider is not on every node.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/statefulset, kubernetes/daemonset, kubernetes/namespace]
  tags: [health, vault, secrets, csi]
---

## When to use

Something that depends on Vault is failing: logins, secret reads, or pods that mount secrets through
the CSI provider. A Vault pod that restarts comes back **sealed** and does not serve until it is
unsealed, so run `server-status` first and read `sealed` before anything else.

Targets, by task:

- `server-status` takes the Vault **StatefulSet**, or the **namespace** (every StatefulSet in it
  labelled `app.kubernetes.io/name=vault`, as the Vault Helm chart labels it).
- `csi-provider-status` takes the CSI provider **DaemonSet**, or the **namespace** (every DaemonSet
  labelled `app.kubernetes.io/name=vault-csi-provider`).
- The other kind of workload is `invalid_input`; a namespace with no such workload is `not_found`.

The tasks only read. `server-status` calls the pod proxy subresource (`pods/proxy` get), so the
connection needs that verb in the Vault namespace. A namespace target also needs `get` on the
Namespace, which is cluster-scoped; without it the lists are still tried and the denial is reported.
No Secret is read and no Vault token is used.

## What each task tells you

- `server-status`: `pods` has, per pod, `health.sealed`, `initialized`, `standby`,
  `performanceStandby`, `version`, `clusterName`, `clusterId`, `replicationPerformanceMode` and
  `replicationDrMode`, plus `state` (one of `sealed`, `uninitialized`, `active`, `standby`,
  `performance_standby`, `dr_secondary`) and `healthCode`, the HTTP status Vault returns for that
  state (503, 501, 200, 429, 473, 472). These are facts, never errors: a standby or sealed Vault is
  answering. Sealed and uninitialized pods come first. `containerRestarts` shows a pod that has
  restarted (which is how it came back sealed). `statefulsets` has `desired`, `ready`, `current`,
  `updated` and `available`; `counts.byState` totals the states.
- `csi-provider-status`: `daemonsets` has `desired`, `current`, `ready`, `updated`, `available`,
  `unavailable`, `misscheduled`, `maxUnavailable` and `unavailableExceedsMax`; `pods` has each pod's
  node, containers (init ones flagged) with `restartCount`, `state`, `reason` and the last
  termination; `events` has Warning events about the DaemonSet and its pods within `since`
  (default 1h).

## How to read the result

- Vault answers a health request with a status code that names the state. The API server's proxy
  would turn those codes into errors and drop the body, so the task asks Vault to answer 200 for
  every state and reads the state from the body. `healthCode` is what Vault returns by default.
- On a Vault with several pods exactly one is `active`; the rest `standby` is normal. A sealed pod
  that reports `standby: true` is a Vault quirk: read `state`. `clusterName` and `clusterId` are null
  while sealed.
- `pods[].queried: false` is a pod that is not Running or has no address; it was listed, not asked.
  A pod with an `error` could not be asked (the result is `partial`; the code is
  `permission_denied` without `pods/proxy`, `unreachable` when nothing listens on the port, and
  `invalid_input` when `scheme` or `port` does not match the listener). If no pod could be asked,
  the task fails with that code.
- Set `scheme: https` for a TLS listener. The API server does not verify the pod's certificate, so a
  self-signed one works; the health endpoint is unauthenticated, and no credential is sent.
- A Vault pod that is not ready is normal while it is sealed or uninitialized: the chart's readiness
  probe fails on 503 and 501. `ready` is the pod's readiness; `state` is Vault's own answer.
- A CSI `desired` of 0 means no node matches the DaemonSet's selector, not that all is well.
  `unavailableExceedsMax: true` means more nodes lack a ready provider than the rollout allows.
- Events live about an hour on most clusters; an empty `events` list is not proof nothing happened
  earlier. Lists stop at `maxItems` (default 200); `counts` is before the cap.

## What to try next

- Pods not ready or restarting: `kubernetes:k8s-workload-health/pod-status`,
  `kubernetes:k8s-workload-health/pod-restarts`, `kubernetes:k8s-workload-health/rollout-status`.
- The Vault or CSI provider log (seal, storage and auth errors):
  `kubernetes:k8s-workload-logs/recent-logs` and `error-patterns` on the StatefulSet or DaemonSet.
- A manifest or configuration question (the old manifest-dump tasks):
  `kubernetes:k8s-workload-health/config-changes` and `probe-config`.
- A namespace-wide view when several workloads fail together:
  `kubernetes:k8s-namespace-health/unhealthy-pods` and `warning-events`.
- A Vault that stays sealed needs an unseal with its keys; that is a Vault operation outside this
  skill, which holds no credential.
