#!/usr/bin/env bash
# k8s-vault-health: wait for the Vault workloads of rw-vault and put sts/vault-sealed in the state
# of a restarted Vault: initialized (one key share, thrown away) and sealed. The pod is created
# uninitialized; initializing it and restarting its process (emptyDir keeps the file storage)
# leaves it sealed, and with no key nothing unseals it. Idempotent: it initializes only an
# uninitialized Vault, so a second run changes nothing.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}" -n rw-vault)
status() { "${K[@]}" exec vault-sealed-0 -c vault -- sh -c 'VAULT_ADDR=http://127.0.0.1:8200 vault status -format=json' 2>/dev/null || true; }

"${K[@]}" rollout status sts/vault --timeout=300s
"${K[@]}" rollout status ds/vault-csi-provider --timeout=240s
"${K[@]}" wait --for=condition=Initialized pod/vault-sealed-0 --timeout=300s
# the server answers on 8200 (status exits 2 for a sealed Vault, so its JSON is read, not its code)
until [[ -n "$(status)" ]]; do sleep 2; done

if [[ "$(status | grep -c '"initialized": false')" != 0 ]]; then
  "${K[@]}" exec vault-sealed-0 -c vault -- sh -c \
    'VAULT_ADDR=http://127.0.0.1:8200 vault operator init -key-shares=1 -key-threshold=1 -format=json >/dev/null'
  # restart the vault process: PID 1; the container comes back sealed
  "${K[@]}" exec vault-sealed-0 -c vault -- sh -c 'kill -TERM 1' || true
  until [[ "$(status | grep -c '"sealed": true')" != 0 ]] && [[ "$(status | grep -c '"initialized": true')" != 0 ]]; do sleep 2; done
fi
# the broken CSI stand-in is restarting and has failed its probes
until [[ "$("${K[@]}" get pods -l app.kubernetes.io/instance=broken -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')" -ge 1 ]]; do sleep 2; done
