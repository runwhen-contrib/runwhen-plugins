---
name: k8s-ingress-health
description: >-
  Health facts for one Kubernetes Ingress: for the default backend and every rule
  path, the backend Service and port, whether the Service and that port exist and
  how many of the port's endpoints are ready; the TLS hosts and Secret names; the
  GKE per-backend health annotation and the load balancer object names it
  records; the assigned load balancer address, whether the IngressClass exists
  (or which one is the default), and whether a LoadBalancer backend Service has a
  different address from the Ingress; and Warning events on the Ingress and its
  Services. Use when an Ingress returns 404 or 503, has no address, or its
  backends are reported unhealthy.
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

Every task needs `get` on the Ingress and `list` on Services (one list of the
namespace, filtered here). `backends` also lists EndpointSlices, `address` reads
the **cluster-scoped** IngressClass (`get` by name, or `list` when the Ingress
names none; needs a ClusterRole, not a Role), `warning-events` lists Events. No
task reads Secrets: TLS Secret names are listed from the Ingress spec and never
looked up. No task needs a cloud credential; the GKE data is annotations on the
Ingress.

## What each task tells you

- `backends`: `backends.items` has one entry per default backend and per rule
  path (`source`, `host`, `path`). A Service backend says `serviceExists`,
  `portExists` (matched by port number or by port name), the `targetPort`,
  `serviceType`, `hasSelector` and `publishNotReady`, and `endpoints`:
  `ready` and `notReady` counts, plus `serving` and `terminating`, taken only
  from the EndpointSlices that carry **that backend's Service port** (a pod that
  does not expose the port is not counted for it). A pod listed in several slices
  counts once, and is ready if ready in any. A `resource` backend (a
  controller-specific object) is reported as `kind: resource` with nothing
  claimed about Services. `tls` lists each entry's hosts and `secretName`.
  `gkeBackends` is the parsed `ingress.kubernetes.io/backends` annotation: each
  backend's `health` (HEALTHY, UNHEALTHY, UNKNOWN), not-healthy first, with the
  `service` and `port` read out of NEG-style names (`k8s1-<hash>-<ns>-<svc>-<port>-<hash>`)
  when they parse, else null. `gkeLoadBalancer` has the forwarding-rule,
  url-map, target-proxy and static-ip names the controller recorded, to hand to a
  `gcp` plugin skill.
- `address`: `ingress.addresses` are the `status.loadBalancer.ingress` entries
  (`assigned` is true when there is one), with `ingressClassName` and the legacy
  `kubernetes.io/ingress.class` annotation. `ingressClass.classSource` is `spec`
  (class named: `exists`, `controller`, `isDefault` are read), `annotation` (a
  legacy controller string, nothing looked up) or `none`; with `none` the
  IngressClasses are listed: `defaultClasses`, `effectiveClass` (the one default,
  else null) and `multipleDefaults`. `serviceAddresses` lists each backend
  Service's type and, for a LoadBalancer Service, `differsFromIngress`: true when
  it has an address of its own that is not the Ingress's (the classic
  Ingress/Service conflict).
- `warning-events`: Warning events whose last occurrence is within `since`, on
  the Ingress and on each backend Service, grouped by object and reason with the
  latest message. Matched by uid when the object exists, by name otherwise.
  `count` is the events' own counter, which counts over the event's whole
  lifetime, not only the window. `inspected` lists the Ingress and the Services
  that were checked.

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `backends.items: []` means the Ingress has no default backend
  and no rule paths. `events.items: []` means no Warning event was last seen
  within `since` (events expire after about an hour by default, so an old
  problem can have none). `gkeBackends.present: false` means the annotation is
  absent, which is normal off GKE and before the controller has synced; it is not
  a statement that backends are healthy.
- **`{ready: 0, notReady: 0}`** means no endpoint is listed for that port at all,
  which is different from `notReady > 0`. It is normal for an `ExternalName`
  Service and a Service with no selector (`hasSelector: false`, endpoints only if
  someone wrote them); otherwise the selector matches no pod, the pods have no IP
  yet, or no pod exposes the port. `publishNotReady: true` publishes not-ready
  pods too, so `ready` then undercounts what receives traffic. `terminating`
  endpoints are usually also not ready.
- **`null` is unknown.** `serviceExists`, `portExists` and `endpoints` are `null`
  when the Service or EndpointSlice read was forbidden (a `permission_denied`
  error scoped `services` or `endpointslices` says so), or when the Service was
  past the lookup cap (see below). `endpoints` is also `null` when the port does
  not exist on the Service. `ingressClass.exists: null` with `classSource: spec`
  and `lookedUp: false` is a forbidden read (scope `ingressclasses`); with
  `classSource: none` it only means no class is named, and `defaultClasses: null`
  there is a forbidden list. Only `false` means missing. `gkeBackends.backends:
  null` means the annotation was present but not the expected JSON (error scope
  `annotations`). A result with errors is not a clean bill of health for the
  outputs it did return.
- **`portExists` is not authoritative for `ExternalName`.** An ExternalName
  Service with no `spec.ports` has nothing to match, so `portExists` is `null`,
  not `true`: the port is whatever the external name serves.
- **Cuts.** List outputs are `{items, total, truncated}` and stop at `maxItems`
  (default 200); `total` is the full count. At most 100 distinct backend Services
  are looked up; `servicesTruncated` says some were not, and their facts are null.
  Lists inside an entry (hosts, addresses) stop at 50 and are flagged
  `truncated`. Names and messages are cut at 1024 characters and messages flagged
  `truncated`. `since` takes `ms|s|m|h|d` combinations (default `1h`); anything
  else is an `invalid_input` error.
- **Normal states that look like failures.** On kind, minikube and any cluster
  without an ingress controller an Ingress has no address and no events: nothing
  is wrong with the Ingress. A `notReady` endpoint during a rollout is expected; a
  `ready: 0` that stays is not. Endpoint `ready` counts say pods are Ready, not
  that the application listens on the `targetPort`. `classSource: none` with a
  default IngressClass, or the legacy class annotation, is a valid setup; several
  defaults (`multipleDefaults: true`) make new Ingresses without a class fail
  admission.

## What to try next

- Backend has `ready: 0` or `notReady > 0`: `kubernetes:k8s-workload-health/pod-status`
  and `pod-restarts` on the workload the Service selects, then
  `kubernetes:k8s-workload-logs/recent-logs`.
- Service or port missing: fix the Ingress or Service (a write; see
  `kubernetes:k8s-workload-ops`), then re-run `backends`.
- No address or missing IngressClass: check the ingress controller's own workload
  with `kubernetes:k8s-workload-health/rollout-status`.
- GKE backends UNHEALTHY with ready endpoints: the load balancer's health check
  is failing; inspect the load balancer named in `gkeLoadBalancer` with the `gcp`
  plugin (it needs a GCP credential, which this skill does not use).
- Many Ingresses in the namespace affected: `kubernetes:k8s-namespace-health/unhealthy-pods`.
