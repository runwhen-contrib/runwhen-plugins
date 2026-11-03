#!/usr/bin/env bash
# rw-certs: instances of the synthetic cert-manager CRDs and the status a controller
# would have written (no controller runs here). Idempotent: an object that already has
# a status is left alone, so notAfter does not move on a second run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
NS=rw-certs

for crd in certificates certificaterequests issuers clusterissuers; do
  "${K[@]}" wait --for=condition=Established "crd/${crd}.cert-manager.io" --timeout=60s
done
"${K[@]}" apply -f "${HERE}/k8s-certmanager-health-instances.yaml"

# ts <offset-days> [offset-seconds]: RFC 3339 UTC, relative to now.
ts() { python3 -c 'import sys,datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(days=float(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
# has_status <kind> <name>: has the object a status already
has_status() { [ -n "$("${K[@]}" -n "$NS" get "$1" "$2" -o jsonpath='{.status}')" ]; }
patch_status() { # <kind> <name> <json merge patch for the status>
  "${K[@]}" -n "$NS" patch "$1" "$2" --subresource=status --type=merge -p "$3"
}
cond() { # <type> <status> <reason> <message> <transition>
  printf '{"type":"%s","status":"%s","reason":"%s","message":"%s","lastTransitionTime":"%s","observedGeneration":1}' "$1" "$2" "$3" "$4" "$5"
}
NOW=$(ts 0)

# issuers
if ! has_status issuers.cert-manager.io ca-ready; then
  patch_status issuers.cert-manager.io ca-ready \
    "{\"status\":{\"conditions\":[$(cond Ready True KeyPairVerified 'Signing CA verified' "$NOW")]}}"
fi
if ! has_status issuers.cert-manager.io broken-issuer; then
  patch_status issuers.cert-manager.io broken-issuer \
    "{\"status\":{\"conditions\":[$(cond Ready False ErrGetKeyPair 'Error getting keypair for CA issuer: secret missing-key-pair not found' "$NOW")]}}"
fi
if ! has_status clusterissuers.cert-manager.io rw-cluster-issuer; then
  patch_status clusterissuers.cert-manager.io rw-cluster-issuer \
    "{\"status\":{\"conditions\":[$(cond Ready True IsReady 'Signing CA verified' "$NOW")]}}"
fi

# certificates
if ! has_status certificates.cert-manager.io ready-cert; then
  patch_status certificates.cert-manager.io ready-cert "{\"status\":{
    \"conditions\":[$(cond Ready True Ready 'Certificate is up to date and has not expired' "$NOW")],
    \"notBefore\":\"$(ts -30)\",\"notAfter\":\"$(ts 3000)\",\"renewalTime\":\"$(ts 2900)\",\"revision\":1}}"
fi
if ! has_status certificates.cert-manager.io expiring-cert; then
  patch_status certificates.cert-manager.io expiring-cert "{\"status\":{
    \"conditions\":[$(cond Ready True Ready 'Certificate is up to date and has not expired' "$NOW")],
    \"notBefore\":\"$(ts -85)\",\"notAfter\":\"$(ts 5)\",\"renewalTime\":\"$(ts -10)\",\"revision\":1}}"
fi
if ! has_status certificates.cert-manager.io failed-cert; then
  patch_status certificates.cert-manager.io failed-cert "{\"status\":{
    \"conditions\":[$(cond Ready False Failed 'The certificate request has failed to complete and will be retried: Failed to wait for order resource' "$NOW"),
                    $(cond Issuing True Failed 'The certificate request has failed to complete and will be retried' "$NOW")],
    \"lastFailureTime\":\"$(ts -0.01)\",\"revision\":0}}"
fi
if ! has_status certificates.cert-manager.io orphan-cert; then
  patch_status certificates.cert-manager.io orphan-cert "{\"status\":{
    \"conditions\":[$(cond Ready False Pending 'Referenced Issuer not found' "$NOW")]}}"
fi
if ! has_status certificates.cert-manager.io external-cert; then
  patch_status certificates.cert-manager.io external-cert "{\"status\":{
    \"conditions\":[$(cond Ready False Pending 'Waiting for CertificateRequest to complete' "$NOW")]}}"
fi
# new-cert keeps no status at all: a Certificate the controller has not looked at yet.

# certificate requests: owned by their Certificate, so the uid is read at run time
owner_ref() { # <certificate name>
  local uid
  uid=$("${K[@]}" -n "$NS" get certificates.cert-manager.io "$1" -o jsonpath='{.metadata.uid}')
  printf '[{"apiVersion":"cert-manager.io/v1","kind":"Certificate","name":"%s","uid":"%s","controller":true,"blockOwnerDeletion":true}]' "$1" "$uid"
}
make_request() { # <name> <certificate> <revision> <issuer> <status merge patch>
  if ! "${K[@]}" -n "$NS" get certificaterequests.cert-manager.io "$1" >/dev/null 2>&1; then
    "${K[@]}" apply -f - <<YAML
apiVersion: cert-manager.io/v1
kind: CertificateRequest
metadata:
  name: $1
  namespace: $NS
  annotations: {cert-manager.io/certificate-revision: "$3"}
  ownerReferences: $(owner_ref "$2")
spec:
  request: Zm9v
  issuerRef: {name: $4, kind: Issuer, group: cert-manager.io}
YAML
  fi
  has_status certificaterequests.cert-manager.io "$1" || patch_status certificaterequests.cert-manager.io "$1" "$5"
}
make_request ready-cert-1 ready-cert 1 ca-ready \
  "{\"status\":{\"conditions\":[$(cond Approved True cert-manager.io 'Certificate request has been approved by cert-manager.io' "$NOW"),
   $(cond Ready True Issued 'Certificate fetched from issuer successfully' "$NOW")]}}"
make_request failed-cert-1 failed-cert 1 broken-issuer \
  "{\"status\":{\"failureTime\":\"$(ts -0.5)\",\"conditions\":[$(cond Approved True cert-manager.io 'Certificate request has been approved by cert-manager.io' "$NOW"),
   $(cond Ready False Failed 'Failed to wait for order resource failed-cert-1-123 to become ready: order is in errored state' "$NOW")]}}"
make_request failed-cert-2 failed-cert 2 broken-issuer \
  "{\"status\":{\"failureTime\":\"$(ts -0.01)\",\"conditions\":[$(cond Denied True ApprovalDenied 'Denied by policy' "$NOW"),
   $(cond Ready False Denied 'The CertificateRequest was denied by an approval controller' "$NOW"),
   $(cond InvalidRequest True RequestInvalid 'The CSR PEM requests a duration of 1h0m0s, which is shorter than the minimum' "$NOW")]}}"
