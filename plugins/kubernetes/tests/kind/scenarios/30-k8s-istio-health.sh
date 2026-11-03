#!/usr/bin/env bash
# rw-istio: wait for the simulated mesh to be running (istio-ingressgateway is meant to be
# stuck pulling its image, so it is not waited for). Idempotent.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
for d in meshed meshed-quiet bare bare-optout istiod; do
  "${K[@]}" -n rw-istio rollout status "deploy/${d}" --timeout=240s
done
