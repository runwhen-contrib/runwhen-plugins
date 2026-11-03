#!/usr/bin/env bash
# rw-karpenter: instances of the synthetic Karpenter CRDs and the status a controller would
# have written (no Karpenter runs here). Idempotent: an object that already has a status is left
# alone, so timestamps do not move on a second run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K=(kubectl --context "${K_CONTEXT:-kind-rw-plugins}")

for crd in nodepools.karpenter.sh nodeclaims.karpenter.sh ec2nodeclasses.karpenter.k8s.aws; do
  "${K[@]}" wait --for=condition=Established "crd/${crd}" --timeout=60s
done
"${K[@]}" apply -f "${HERE}/k8s-karpenter-health-instances.yaml"

# has_status <kind> <name>: has the (cluster-scoped) object a status already
has_status() { [ -n "$("${K[@]}" get "$1" "$2" -o jsonpath='{.status}')" ]; }
patch_status() { # <kind> <name> <json merge patch for the status>
  "${K[@]}" patch "$1" "$2" --subresource=status --type=merge -p "$3"
}
ts() { python3 -c 'import sys,datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(minutes=float(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
cond() { # <type> <status> <reason> <message>
  printf '{"type":"%s","status":"%s","reason":"%s","message":"%s","lastTransitionTime":"%s","observedGeneration":1}' "$1" "$2" "$3" "$4" "$(ts 0)"
}

# EC2NodeClasses
has_status ec2nodeclasses.karpenter.k8s.aws default || patch_status ec2nodeclasses.karpenter.k8s.aws default \
  "{\"status\":{\"conditions\":[$(cond Ready True Ready ''),$(cond AMIsReady True AMIsReady ''),$(cond SubnetsReady True SubnetsReady '')]}}"
has_status ec2nodeclasses.karpenter.k8s.aws broken || patch_status ec2nodeclasses.karpenter.k8s.aws broken \
  "{\"status\":{\"conditions\":[$(cond Ready False SubnetsNotFound 'SubnetSelector did not match any Subnets'),$(cond SubnetsReady False SubnetsNotFound 'SubnetSelector did not match any Subnets')]}}"

# NodePools
POOLS=nodepools.karpenter.sh
has_status $POOLS healthy-pool || patch_status $POOLS healthy-pool "{\"status\":{
  \"conditions\":[$(cond Ready True Ready ''),$(cond ValidationSucceeded True ValidationSucceeded ''),$(cond NodeClassReady True NodeClassReady '')],
  \"resources\":{\"cpu\":\"8\",\"memory\":\"32Gi\",\"nodes\":\"2\",\"pods\":\"20\"}}}"
has_status $POOLS full-pool || patch_status $POOLS full-pool "{\"status\":{
  \"conditions\":[$(cond Ready True Ready ''),$(cond ValidationSucceeded True ValidationSucceeded ''),$(cond NodeClassReady True NodeClassReady '')],
  \"resources\":{\"cpu\":\"100\",\"memory\":\"400Gi\",\"nodes\":\"3\"}}}"
has_status $POOLS broken-pool || patch_status $POOLS broken-pool "{\"status\":{
  \"conditions\":[$(cond Ready False NodeClassNotReady 'NodeClassRef not ready'),$(cond ValidationSucceeded True ValidationSucceeded ''),$(cond NodeClassReady False NodeClassNotReady 'NodeClassRef not ready')],
  \"resources\":{\"cpu\":\"4\",\"nodes\":\"1\"}}}"
has_status $POOLS orphan-pool || patch_status $POOLS orphan-pool "{\"status\":{
  \"conditions\":[$(cond Ready False NodeClassNotFound 'NodeClass not found on cluster'),$(cond NodeClassReady False NodeClassNotFound 'NodeClass not found on cluster')]}}"
has_status $POOLS external-pool || patch_status $POOLS external-pool "{\"status\":{
  \"conditions\":[$(cond Ready True Ready ''),$(cond NodeClassReady True NodeClassReady '')]}}"
has_status $POOLS aks-pool || patch_status $POOLS aks-pool "{\"status\":{
  \"conditions\":[$(cond Ready True Ready ''),$(cond NodeClassReady True NodeClassReady '')]}}"

# NodeClaims: owned by their pool (the uid is read now), labelled like Karpenter labels them
CLAIMS=nodeclaims.karpenter.sh
make_claim() { # <name> <pool> <capacity-type> <instance-type> <class> <status merge patch>
  if ! "${K[@]}" get $CLAIMS "$1" >/dev/null 2>&1; then
    local uid
    uid=$("${K[@]}" get $POOLS "$2" -o jsonpath='{.metadata.uid}')
    "${K[@]}" apply -f - <<YAML
apiVersion: karpenter.sh/v1
kind: NodeClaim
metadata:
  name: $1
  labels:
    karpenter.sh/nodepool: $2
    karpenter.sh/capacity-type: $3
    node.kubernetes.io/instance-type: $4
    topology.kubernetes.io/zone: us-east-1a
  ownerReferences:
    - {apiVersion: karpenter.sh/v1, kind: NodePool, name: $2, uid: "$uid", controller: true, blockOwnerDeletion: true}
spec:
  nodeClassRef: {group: karpenter.k8s.aws, kind: EC2NodeClass, name: $5}
  requirements:
    - {key: node.kubernetes.io/instance-type, operator: In, values: [$4]}
YAML
  fi
  has_status $CLAIMS "$1" || patch_status $CLAIMS "$1" "$6"
}
READY_CLAIM="\"conditions\":[$(cond Launched True Launched ''),$(cond Registered True Registered ''),$(cond Initialized True Initialized ''),$(cond Ready True Ready '')"
make_claim healthy-pool-aaaaa healthy-pool spot m5.large default \
  "{\"status\":{\"nodeName\":\"ip-10-0-1-10.ec2.internal\",\"providerID\":\"aws:///us-east-1a/i-0aaaa\",$READY_CLAIM,$(cond Consolidatable True Consolidatable '')]}}"
make_claim healthy-pool-bbbbb healthy-pool on-demand c5.xlarge default \
  "{\"status\":{\"nodeName\":\"ip-10-0-1-11.ec2.internal\",\"providerID\":\"aws:///us-east-1a/i-0bbbb\",$READY_CLAIM,$(cond Drifted True AMIDrift 'AMI drift detected')]}}"
make_claim full-pool-ccccc full-pool on-demand m5.4xlarge default \
  "{\"status\":{\"nodeName\":\"ip-10-0-2-20.ec2.internal\",\"providerID\":\"aws:///us-east-1a/i-0cccc\",$READY_CLAIM]}}"
make_claim broken-pool-ready1 broken-pool on-demand m5.large broken \
  "{\"status\":{\"nodeName\":\"ip-10-0-3-30.ec2.internal\",\"providerID\":\"aws:///us-east-1a/i-0dddd\",$READY_CLAIM]}}"
# never registers: launched, but the node did not join the cluster
make_claim broken-pool-stuck1 broken-pool spot m5.xlarge broken \
  "{\"status\":{\"providerID\":\"aws:///us-east-1a/i-0eeee\",\"conditions\":[$(cond Launched True Launched ''),$(cond Registered Unknown NodeNotFound 'Node not registered with cluster'),$(cond Initialized Unknown NodeNotFound 'Node not registered with cluster'),$(cond Ready Unknown ReconcilingDependents 'Node not registered with cluster')]}}"

# the pods cannot schedule: wait until the scheduler has said so
for p in pending-full pending-broken pending-any; do
  for _ in $(seq 1 60); do
    [ "$("${K[@]}" -n rw-karpenter get pod "$p" -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}')" = Unschedulable ] && break
    sleep 1
  done
done
