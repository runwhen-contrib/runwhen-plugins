#!/usr/bin/env bash
# Delete the rw-plugins kind cluster (and the forbidden kubeconfig, which holds a token).
set -euo pipefail
FORBIDDEN_KUBECONFIG="${FORBIDDEN_KUBECONFIG:-/Users/samirpatil/Documents/runwhen-auto/envs/v13-lane-f/kind-forbidden.kubeconfig}"
kind delete cluster --name rw-plugins
rm -f "$FORBIDDEN_KUBECONFIG"
