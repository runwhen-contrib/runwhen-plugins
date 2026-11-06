---
name: k8s-certmanager-health
description: >-
  Health facts for a cert-manager Certificate: its Ready and Issuing conditions
  with reasons, validity window (notBefore, notAfter, renewalTime), days left
  until it expires and whether that is inside a warning window, the DNS names and
  Secret name it covers, whether the Issuer or ClusterIssuer it refers to exists
  and is Ready, and the CertificateRequests (and ACME Orders and Challenges)
  behind each issuance with their Approved, Denied, InvalidRequest and Ready
  conditions. Use
  when a TLS certificate is not Ready, a Secret is missing or stale, a
  certificate is about to expire or renewal is not happening, or an issuance
  keeps failing.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/certificate]
  tags: [health, certificates, cert-manager, tls]
---

## When to use

The target is a cert-manager `Certificate` and a service reports a TLS error, the
Secret named by the Certificate is missing or old, the Certificate is not Ready,
or an expiry alert fired. Start with `certificate-status`; if the Certificate is
not Ready or renewal is overdue, `requests` shows why the issuance did not
complete.

`certificate-status` needs `get` on the Certificate and on `issuers` in its
namespace, and on `clusterissuers`, which are **cluster-scoped** and need a
ClusterRole. A namespace-scoped identity still gets the Certificate facts; the
issuer part then has `exists: null`, `ready: null` and a `permission_denied`
error scoped `clusterissuers`. `requests` needs `list` on `certificaterequests`
and on `acme.cert-manager.io` `orders` and `challenges`. No task reads Secrets: the Certificate's
Secret is only named.

## What each task tells you

- `certificate-status`: `certificate.ready` and `certificate.issuing` are the
  Ready and Issuing conditions (status, reason, message), or `null` when the
  controller has not set them. `notBefore`, `notAfter`, `renewalTime` and
  `revision` come from the status. `expiresInDays` is the time left (negative
  once passed); `expiringSoon` is true when it is within the `expiryWarningDays`
  setting (default 14) or already past, and `expired` says which. These three
  and `renewalTimePassed` are `null` before the first issuance. `dnsNameCount`,
  `dnsNames`, `commonName` and `secretName` are from the spec; `sanCount` also
  counts IP addresses, URIs and email addresses. `issuer` names
  the referenced issuer, whether it `exists` and whether its Ready condition is
  true, with that condition's reason and message.
- `requests`: `requests` are the CertificateRequests owned by this Certificate,
  newest revision first. Each has `ready`, `approved`, `denied` and
  `invalidRequest` conditions (a condition that is not set is `null`),
  `revision`, `failureTime` and the `issuerRef` it used. `orders` has the ACME
  Orders those requests created: `state` (`pending`, `valid`, `invalid`,
  `errored`, ...), the failure `reason` and the number of authorizations.
  `challenges` has the ACME Challenges of those Orders: type, domain, `state`,
  `presented`, `processing` and `reason`, where a failed or waiting validation
  says why (for example an HTTP-01 check that gets a 404).

## How to read the result

Outputs are facts, not verdicts: nothing here says healthy or unhealthy.

- **Empty lists.** `requests: []` means the Certificate owns no
  CertificateRequest: it has not started an issuance, or the old ones were
  pruned (cert-manager keeps every revision unless `spec.revisionHistoryLimit`
  is set on the Certificate). `orders.items: []` means no Order
  belongs to the returned requests, which is normal for non-ACME issuers.
- **`null` is unknown.** A part that could not be read is `null` with a
  `permission_denied` error: `issuer.exists`/`issuer.ready` (scope
  `clusterissuers` or `issuers`), `orders` (scope `orders`). A Certificate that
  has no `notAfter` yet has `expiresInDays`, `expired` and `expiringSoon` `null`,
  not false: it has not been issued. A result with errors is not a clean bill of
  health for the outputs it did return.
- **Orders and Challenges not applicable.** `orders.applicable: false` or
  `challenges.applicable: false` means the cluster has no `acme.cert-manager.io`
  Order or Challenge type (cert-manager without ACME support): not an error and
  not a problem. `challenges: null` with an `orders` error means the Challenges
  were not read because they cannot be matched without the Orders.
- **External issuers.** `issuer.lookedUp: false` with no error means the
  Certificate refers to an issuer of another group or kind (an external issuer
  such as AWS PCA) that this skill does not check. `exists` and `ready` are then
  `null`, not false: look at that issuer's own controller and CRD.
- **Stale Ready.** Compare `ready.observedGeneration` with `certificate.generation`:
  when the condition was observed at an older generation, the Ready status
  describes the Certificate before its last spec change and may not hold.
- **Cuts.** `requests`, `orders.items` and `challenges.items` stop at `maxItems` (default 200); lists
  inside an entry (conditions, dnsNames) stop at 50. Messages are cut at 1024
  characters and flagged `truncated`.
- **Normal states that look like failures.** `issuing` is True while a renewal
  runs and `ready` stays True throughout, so a renewing Certificate is Ready and
  Issuing at once. Old CertificateRequests with `denied` or a `failureTime` are
  history if the Certificate has since issued a newer revision (compare
  `revision` with `certificate.revision`). A request waiting for approval has
  `approved: null`. An Issuer that exists but has no Ready condition yet has
  `ready: null`. A Certificate can be Ready with `expired: true` when the
  controller has not run since the expiry (cert-manager down or `renewalTime`
  passed): check `renewalTimePassed` and the cert-manager controller.

## What to try next

- Issuance blocked because the controller or webhook is not running:
  `kubernetes:k8s-workload-health/pod-status` and
  `kubernetes:k8s-workload-logs/recent-logs` on the `cert-manager` Deployments.
- The Secret named by `secretName` is not mounted or the ingress still serves
  the old certificate: `kubernetes:k8s-ingress-health/backends` and
  `kubernetes:k8s-ingress-health/address` for the Ingress that uses it.
- An ACME failure: read `challenges[].reason` (the Challenge's `status.reason`
  in `acme.cert-manager.io`) before anything else, then check the solver pod or
  solver Ingress with `kubernetes:k8s-namespace-health/unhealthy-pods` for the
  namespace.
- Many Certificates affected at once, or the ClusterIssuer is the problem: run
  `certificate-status` on each, or look at the issuer's own namespace.
