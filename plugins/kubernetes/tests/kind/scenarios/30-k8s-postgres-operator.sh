#!/usr/bin/env bash
# k8s-postgres-operator-health / -ops: the custom resources in rw-pgop and the status an
# operator would set. CRs are applied once the CRDs are established; status goes through the
# status subresource. The member pods and the patronictl stub are in the yaml next to this
# file. Idempotent: re-running resets the status below and the stub's state and log in each pod.
#
# Crunchy PostgresCluster (postgres-operator.crunchydata.com/v1beta1):
#   hippo         2 instances desired and ready; pgBackRest repo ready; scheduled full and incr done.
#                 Pods hippo-instance1-0 (primary, real postgres) and hippo-instance2-0 (replica).
#   hippo-broken  2 desired, 1 ready; stanza not created; the scheduled full backup failed and
#                 never completed. Pods are both replicas (no primary label), instance1 not
#                 Ready; the stub reports no Patroni leader, a replica 512 MB behind and one
#                 that failed to start.
# Zalando postgresql (acid.zalan.do/v1):
#   acid-zalando       Running, 2 instances; pods acid-zalando-0 (primary, real postgres), -1.
#   acid-sync-failed   SyncFailed, no pods at all.
set -euo pipefail
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")

"${K[@]}" wait --for=condition=Established --timeout=60s \
  crd/postgresclusters.postgres-operator.crunchydata.com crd/postgresqls.acid.zalan.do >/dev/null

"${K[@]}" apply -f - <<'YAML'
apiVersion: postgres-operator.crunchydata.com/v1beta1
kind: PostgresCluster
metadata: {name: hippo, namespace: rw-pgop}
spec:
  postgresVersion: 16
  instances:
    - {name: instance1, replicas: 2}
  backups:
    pgbackrest:
      repos:
        - name: repo1
          schedules: {full: "0 2 * * 0", incremental: "0 */6 * * *"}
---
apiVersion: postgres-operator.crunchydata.com/v1beta1
kind: PostgresCluster
metadata: {name: hippo-broken, namespace: rw-pgop}
spec:
  postgresVersion: 15
  instances:
    - {name: instance1, replicas: 2}
  backups:
    pgbackrest:
      repos:
        - name: repo1
          schedules: {full: "0 2 * * 0"}
---
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata: {name: acid-zalando, namespace: rw-pgop}
spec:
  teamId: acid
  numberOfInstances: 2
  postgresql: {version: "16"}
  volume: {size: 1Gi}
---
apiVersion: acid.zalan.do/v1
kind: postgresql
metadata: {name: acid-sync-failed, namespace: rw-pgop}
spec:
  teamId: acid
  numberOfInstances: 2
  postgresql: {version: "15"}
  volume: {size: 1Gi}
YAML

# a real rolling-restart run leaves its annotation under spec.metadata; apply would keep it
for cr in hippo hippo-broken; do
  "${K[@]}" -n rw-pgop patch "postgresclusters.postgres-operator.crunchydata.com/$cr" \
    --type=merge -p '{"spec":{"metadata":null}}' >/dev/null
done

patch_status() { # <resource> <name> <json>
  "${K[@]}" -n rw-pgop patch "$1" "$2" --subresource=status --type=merge -p "$3" >/dev/null
}
PC=postgresclusters.postgres-operator.crunchydata.com
PG=postgresqls.acid.zalan.do

patch_status $PC hippo '{"status":{
  "observedGeneration":1,
  "instances":[{"name":"instance1","readyReplicas":2,"replicas":2,"updatedReplicas":2}],
  "conditions":[
    {"type":"PGBackRestReplicaRepoReady","status":"True","reason":"StanzaCreated","message":"pgBackRest dedicated repository host is ready","observedGeneration":1,"lastTransitionTime":"2026-11-02T10:00:00Z"},
    {"type":"PGBackRestReplicaCreate","status":"True","reason":"RepoBackupComplete","message":"pgBackRest replica create repo is ready for creating replicas","observedGeneration":1,"lastTransitionTime":"2026-11-02T10:05:00Z"}],
  "pgbackrest":{
    "repos":[{"name":"repo1","bound":true,"stanzaCreated":true,"replicaCreateBackupComplete":true,"repoOptionsHash":"7c5b9d7f4"}],
    "scheduledBackups":[
      {"type":"full","repo":"repo1","cronJobName":"hippo-repo1-full","startTime":"2026-11-01T02:00:03Z","completionTime":"2026-11-01T02:04:10Z","succeeded":1},
      {"type":"incr","repo":"repo1","cronJobName":"hippo-repo1-incr","startTime":"2026-11-03T18:00:02Z","completionTime":"2026-11-03T18:01:15Z","succeeded":1},
      {"type":"incr","repo":"repo1","cronJobName":"hippo-repo1-incr","startTime":"2026-11-03T12:00:02Z","completionTime":"2026-11-03T12:01:20Z","succeeded":1}]}}}'

patch_status $PC hippo-broken '{"status":{
  "observedGeneration":1,
  "instances":[{"name":"instance1","readyReplicas":1,"replicas":2,"updatedReplicas":2}],
  "conditions":[
    {"type":"PGBackRestReplicaRepoReady","status":"False","reason":"RepoHostNotReady","message":"pgBackRest dedicated repository host is not ready","observedGeneration":1,"lastTransitionTime":"2026-11-03T08:00:00Z"}],
  "pgbackrest":{
    "repos":[{"name":"repo1","bound":true,"stanzaCreated":false}],
    "scheduledBackups":[
      {"type":"full","repo":"repo1","cronJobName":"hippo-broken-repo1-full","startTime":"2026-11-03T02:00:03Z","failed":3}]}}}'

# observedGeneration follows the object's generation (a real run of rolling-restart bumps it)
for cr in hippo hippo-broken; do
  gen=$("${K[@]}" -n rw-pgop get "$PC/$cr" -o jsonpath='{.metadata.generation}')
  patch_status $PC "$cr" "{\"status\":{\"observedGeneration\":$gen}}"
done

patch_status $PG acid-zalando '{"status":{"PostgresClusterStatus":"Running"}}'
patch_status $PG acid-sync-failed '{"status":{"PostgresClusterStatus":"SyncFailed"}}'

# Pods: wait for the ones that can be Ready (hippo-broken-instance1-0 never is), then reset
# the stub's per-pod state so the recorded members are what `patronictl list` prints.
for p in hippo-instance1-0 hippo-instance2-0 hippo-broken-instance2-0 acid-zalando-0 acid-zalando-1; do
  "${K[@]}" -n rw-pgop wait --for=condition=Ready "pod/$p" --timeout=180s >/dev/null
done
"${K[@]}" -n rw-pgop wait --for=jsonpath='{.status.phase}'=Running pod/hippo-broken-instance1-0 --timeout=180s >/dev/null
for p in hippo-instance1-0:database hippo-instance2-0:database hippo-broken-instance1-0:database \
         hippo-broken-instance2-0:database acid-zalando-0:postgres acid-zalando-1:postgres; do
  "${K[@]}" -n rw-pgop exec "${p%%:*}" -c "${p##*:}" -- rm -f /tmp/patroni-state.json /tmp/patronictl.log
done
