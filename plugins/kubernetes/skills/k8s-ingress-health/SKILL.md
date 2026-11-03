---
name: k8s-ingress-health
description: >-
  Health facts for one Kubernetes Ingress: for the default backend and every rule
  path, the backend Service and port, whether the Service and that port exist and
  how many of its endpoints are ready; the TLS hosts and Secret names; the GKE
  per-backend health annotation when the controller wrote one; the assigned load
  balancer address, whether the named IngressClass exists, and whether a
  LoadBalancer backend Service has a different address from the Ingress; and
  Warning events on the Ingress and its Services. Use when an Ingress returns 404
  or 503, has no address, or its backends are reported unhealthy.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/ingress]
  tags: [health, networking, ingress, services]
---

## When to use

The target is an Ingress and traffic to one of its hosts or paths fails (404,
502, 503, timeouts), it never gets an address, or the load balancer reports its
backends unhealthy. Start with `backends`: most Ingress failures are a backend
that does not exist, names a port the Service lacks, or has no ready endpoints.

Every task needs `get` on the Ingress. `backends` also reads Services and
EndpointSlices, `address` reads Services and the **cluster-scoped** IngressClass
(needs a ClusterRole, not a Role), `warning-events` reads Services and lists
Events, all in the Ingress's namespace. No task reads Secrets: TLS Secret names
are listed from the Ingress spec and never looked up. No task needs a cloud
credential; the GKE data is an annotation on the Ingress.

## What each task tells you

- `backends`: `backends` has one entry per default backend and per rule path
  (`source`, `host`, `path`). A Service backend says `serviceExists`,
  `portExists` (matched by port number or by port name), the `targetPort`, and
  `endpoints` with `ready` and `notReady` counts from the Service's
  EndpointSlices. A `resource` backend (a controller-specific object) is
  reported as `kind: resource` with nothing claimed about Services. `tls` lists
  each TLS entry's hosts and `secretName`. `gkeBackends` is the parsed
  `ingress.kubernetes.io/backends` annotation: each backend's `health`
  (HEALTHY, UNHEALTHY, UNKNOWN) with the not-healthy ones first, and
  `notHealthy`.
- `address`: `ingress.addresses` are the `status.loadBalancer.ingress` entries
  (`assigned` is true when there is one), with `ingressClassName` and the legacy
  `kubernetes.io/ingress.class` annotation. `ingressClass.exists` says whether
  the named class is there, with its controller. `serviceAddresses` lists each
  backend Service's type and, for a LoadBalancer Service, `differsFromIngress`:
  true when it has an address of its own that is not the Ingress's (the classic
  Ingress/Service conflict).
- `warning-events`: Warning events whose last occurrence is within `since`, on
  the Ingress and on each backend Service, grouped by object and reason with the
  latest message. Matched by uid when the object exists, by name otherwise.
  `inspected` lists the Ingress and the Services that were checked.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `backends: []` means the Ingress has no default backend and no
  rule paths. `events: []` means no Warning event was last seen within `since`
  (events expire after about an hour by default, so an old problem can have
  none). `gkeBackends.present: false` means the annotation is absent, which is
  normal off GKE and before the controller has synced; it is not a statement that
  backends are healthy.
- **`null` is unknown.** `serviceExists`, `portExists` and `endpoints` are `null`
  when the Service or EndpointSlice read was forbidden (a `permission_denied`
  error scoped `services` or `endpointslices` says so), and `ingressClass.exists`
  is `null` when the class read was forbidden (scope `ingressclasses`) or no class
  is named. Only `false` means missing. `gkeBackends.backends: null` means the
  annotation was present but not the expected JSON (error scope `annotations`).
  A result with errors is not a clean bill of health for the outputs it did return.
- **Cuts.** List outputs stop at `maxItems` (default 200); lists inside an entry
  (hosts, addresses) stop at 50. Messages are cut at 1024 characters and flagged
  `truncated`. `since` takes `ms|s|m|h|d` combinations (default `1h`); anything
  else is an `invalid_input` error.
- **Normal states that look like failures.** On kind, minikube and any cluster
  without an ingress controller an Ingress has no address and no events: nothing
  is wrong with the Ingress. An `ExternalName` Service has no endpoints, and a
  Service with no selector has endpoints only if someone wrote them. A
  `notReady` endpoint during a rollout is expected; a `ready: 0` that stays is not.
  Endpoint `ready` counts say pods are Ready, not that the application listens on
  the `targetPort`. `ingressClassName: null` with the legacy class annotation set,
  or a default IngressClass, is a valid setup.

## What to try next

- Backend has `ready: 0` or `notReady > 0`: `kubernetes:k8s-workload-health/pod-status`
  and `pod-restarts` on the workload the Service selects, then
  `kubernetes:k8s-workload-logs/recent-logs`.
- Service or port missing: fix the Ingress or Service (a write; see
  `kubernetes:k8s-workload-ops`), then re-run `backends`.
- No address or missing IngressClass: check the ingress controller's own workload
  with `kubernetes:k8s-workload-health/rollout-status`.
- GKE backends UNHEALTHY with ready endpoints: the load balancer's health check
  is failing; inspect the load balancer with the `gcp` plugin (it needs a GCP
  credential, which this skill does not use).
- Many Ingresses in the namespace affected: `kubernetes:k8s-namespace-health/unhealthy-pods`.
