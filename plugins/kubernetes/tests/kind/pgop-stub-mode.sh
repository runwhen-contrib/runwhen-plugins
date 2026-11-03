#!/usr/bin/env bash
# Put the patronictl stub of one k8s-postgres-operator cluster (namespace rw-pgop) in a mode, in
# every pod of the cluster. Used when recording fixtures; 30-k8s-postgres-operator.sh resets all.
#   pgop-stub-mode.sh fail <cluster>        writes print "Failed: ..." and exit 0, change nothing
#   pgop-stub-mode.sh transient <cluster>   the leader is `restarting` (hippo only)
#   pgop-stub-mode.sh reset <cluster>       the recorded members, no mode, empty log
# Cluster: hippo | hippo-broken | acid-zalando. Context kind-rw-plugins unless K_CONTEXT is set.
set -euo pipefail
mode=${1:?usage: pgop-stub-mode.sh fail|transient|reset <cluster>}
cluster=${2:?usage: pgop-stub-mode.sh fail|transient|reset <cluster>}
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}" -n rw-pgop)
pods=$("${K[@]}" get pods -o name \
  -l "postgres-operator.crunchydata.com/cluster=$cluster"; \
  "${K[@]}" get pods -o name -l "application=spilo,cluster-name=$cluster")
[ -n "$pods" ] || { echo "no pods for cluster $cluster" >&2; exit 1; }
for p in $pods; do
  case "$mode" in
    fail) "${K[@]}" exec "$p" -- touch /tmp/patronictl.fail ;;
    transient) "${K[@]}" exec "$p" -- cp /etc/patroni-stub/transient.json /tmp/patroni-state.json ;;
    reset) "${K[@]}" exec "$p" -- sh -c 'rm -f /tmp/patroni-state.json /tmp/patronictl.log /tmp/patronictl.fail /tmp/patronictl.reinit.*' ;;
    *) echo "unknown mode $mode" >&2; exit 2 ;;
  esac
done
