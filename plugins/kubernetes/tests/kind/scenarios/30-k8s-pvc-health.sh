#!/usr/bin/env bash
# rw-broken/stuck: a claim stuck Terminating. The custom finalizer is never
# removed by anything, so once deleted it keeps its deletionTimestamp. Idempotent.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")
if ! "${K[@]}" -n rw-broken get pvc stuck >/dev/null 2>&1; then
  "${K[@]}" apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: stuck
  namespace: rw-broken
  finalizers: [rw.test/hold]
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests: {storage: 100Mi}
YAML
fi
if [ -z "$("${K[@]}" -n rw-broken get pvc stuck -o jsonpath='{.metadata.deletionTimestamp}')" ]; then
  "${K[@]}" -n rw-broken delete pvc stuck --wait=false
fi
