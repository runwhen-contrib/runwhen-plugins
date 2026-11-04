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
  restart left Vault sealed, or the CSI provider is not running on every node the DaemonSet targets.
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

The tasks only read. `server-status` calls the pod proxy subresource (`pods/proxy` get, and nothing
else: the helper's `kubectl proxy` makes no API call of its own), so the connection needs that verb
in the Vault namespace. A namespace target also needs `get` on the
Namespace, which is cluster-scoped; without it the lists are still tried and the denial is reported.
No Secret is read and no Vault token is used.

## What each task tells you

- `server-status`: `pods` has, per pod, `health.sealed`, `initialized`, `standby`,
  `performanceStandby`, `version`, `clusterName`, `clusterId`, `replicationPerformanceMode` and
  `replicationDrMode`, plus `state` (one of `sealed`, `uninitialized`, `removed`, `active`, `standby`,
  `performance_standby`, `dr_secondary`) and `healthCode`, the HTTP status Vault returns for that
  state (503, 501, 200, 429, 473, 472, 530). These are facts, never errors: a standby or sealed Vault is
  answering. Sealed, uninitialized and removed pods come first. `containerRestarts` shows a pod that has
  restarted (which is how it came back sealed). `statefulsets` has `desired`, `ready`, `current`,
  `updated` and `available`; `counts.byState` totals the states.
- `csi-provider-status`: `daemonsets` has `desired`, `current`, `ready`, `updated`, `available`,
  `unavailable`, `anyUnavailable`, `misscheduled`, `maxUnavailable` and `unavailableExceedsMax`;
  `pods` has each pod's
  node, containers (init ones flagged) with `restartCount`, `state`, `reason` and the last
  termination; `events` has Warning events about the DaemonSet and its pods within `since`
  (default 1h).

## How to read the result

- Vault answers a health request with a status code that names the state, and the task reads that
  status (200, 429, 472, 473, 501, 503, 530) as it comes, with the body for the other facts.
  `healthCode` is that status. The request goes through `ctx.k8s_proxy_get`, which never follows a
  redirect: a health endpoint that redirects is an `internal` error naming where it pointed.
- Ready does not mean unsealed, and not ready does not mean sealed: trust `state`, Vault's own
  answer, over the pod's `ready`.
- On a healthy Vault with several pods one is `active` and the rest `standby`. That is not
  guaranteed: during an election there may be none, and with DR or performance standbys (enterprise)
  other states appear next to the active one. A sealed pod that reports `standby: true` is a Vault
  quirk: read `state`. `clusterName` and `clusterId` are null while sealed.
- `state: removed` (health code 530) is a pod taken out of its raft cluster; it no longer serves.
  `initialized` and `sealed` are null when the 530 reply has no health body.
- `pods[].queried: false` is a pod that is not Running or has no address; it was listed, not asked.
  A pod with an `error` could not be asked (the result is `partial`; the code is
  `permission_denied` without `pods/proxy`, `unreachable` when nothing listens on the port, and
  `invalid_input` when `scheme` or `port` does not match the listener). If no pod could be asked,
  the task fails with that code.
- Set `scheme: https` for a TLS listener. The API server does not verify the pod's certificate, so a
  self-signed one works; the health endpoint is unauthenticated, and no credential is sent.
- A Vault pod that is not ready is normal while it is sealed or uninitialized: the chart's readiness
  probe fails on 503 and 501. `ready` is the pod's readiness; `state` is Vault's own answer.
- A StatefulSet that wants pods but has none Running to ask gives a `partial` result with an
  `unreachable` error for scope `pods` and `counts.queried: 0`: nothing was learned, which is not
  health. The task asks pods one by one inside a 45 second budget; a pod reached after it is spent
  is listed with a `timeout` error and `queried: false`.
- Errors that name the cause: `tls: bad certificate` is a listener that wants a client certificate
  (mutual TLS), which the API server's pod proxy cannot present (`unreachable`); a 404 from the
  proxy (`not_found`) usually means nothing serves Vault on the `port` input. An https listener asked over
  http answers 400 "HTTP request to an HTTPS server" and a plain one asked over https is a TLS
  handshake error; both are `invalid_input` naming the scheme to use.
- A CSI `desired` of 0 means no node matches the DaemonSet's selector, not that all is well. A
  nonzero `unavailable` (`anyUnavailable: true`) is a fault in steady state, even below
  `maxUnavailable`; only a rollout in progress explains it. `unavailableExceedsMax: true` means more
  nodes lack a ready provider than the rollout allows. `desired` counts the nodes the DaemonSet
  targets, so compare `ready` with it: the provider should run on every node the DaemonSet targets.
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
