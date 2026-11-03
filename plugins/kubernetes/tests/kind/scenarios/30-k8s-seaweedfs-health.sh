#!/usr/bin/env bash
# Fills the two SeaweedFS releases of 30-k8s-seaweedfs-health.yaml once their pods are Ready:
#   swfs-ok      bucket photos (a 2 MB file, 100 MB quota enabled) and logs (small).
#   swfs-broken  volume.max 3, so growing one volume for each of dataa, datab and datac takes every
#                slot; dataa and datab hold a 3 MB file; dataa has a 1 MB quota enabled (used over
#                it) and datab one that is set but disabled; volume 2 (datab) is marked read-only.
#                datac is a collection with a volume but no bucket entry in the filer.
# Idempotent: a release that already has its bucket entries is left alone.
set -euo pipefail
CTX="${K_CONTEXT:-kind-rw-plugins}"
NS=rw-seaweedfs
K=(kubectl --context "$CTX" -n "$NS")

log() { printf '[30-seaweedfs] %s\n' "$*"; }

"${K[@]}" rollout status statefulset/swfs-ok statefulset/swfs-broken --timeout=300s >/dev/null
poll() { # poll <seconds> <desc> <command...>
  local end=$((SECONDS + $1)) desc=$2; shift 2
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < end)) || { log "TIMEOUT waiting for $desc"; return 1; }
    sleep 2
  done
}
proxy() { "${K[@]}" get --raw "/api/v1/namespaces/$NS/pods/http:$1:$2/proxy/$3"; }
inpod() { "${K[@]}" exec "$1" -- sh -c "$2"; }
weedshell() { inpod "$1" "printf '%b' '$2' | weed shell -master=localhost:9333"; }
has_bucket() { proxy "$1" 8888 "buckets/$2?metadata=true" >/dev/null 2>&1; }
upload() { # upload <pod> <bucket> <megabytes>
  inpod "$1" "head -c $(($3 * 1000000)) /dev/urandom > /tmp/f && curl -sf -F file=@/tmp/f http://localhost:8888/buckets/$2/f1 >/dev/null"
}
topology_ready() { proxy "$1" 9333 dir/status | grep -q '"Max":'; }

poll 120 "swfs-ok topology" topology_ready swfs-ok-0
poll 120 "swfs-broken topology" topology_ready swfs-broken-0

if ! has_bucket swfs-ok-0 photos; then
  log "seeding swfs-ok"
  upload swfs-ok-0 photos 2
  upload swfs-ok-0 logs 1
  weedshell swfs-ok-0 's3.bucket.quota -name=photos -op=set -sizeMB=100\n'
fi

if ! has_bucket swfs-broken-0 dataa; then
  log "seeding swfs-broken"
  for c in dataa datab datac; do proxy swfs-broken-0 9333 "vol/grow?count=1&collection=$c" >/dev/null; done
  upload swfs-broken-0 dataa 3
  upload swfs-broken-0 datab 3
  weedshell swfs-broken-0 's3.bucket.quota -name=dataa -op=set -sizeMB=1\ns3.bucket.quota -name=datab -op=set -sizeMB=10\ns3.bucket.quota -name=datab -op=disable\n'
  ip=$("${K[@]}" get pod swfs-broken-0 -o jsonpath='{.status.podIP}')
  vid=$(proxy swfs-broken-0 9333 vol/status | python3 -c '
import json, sys
d = json.load(sys.stdin)
for dc in d["Volumes"]["DataCenters"].values():
    for rack in dc.values():
        for vols in rack.values():
            for v in vols:
                if v["Collection"] == "datab":
                    print(v["Id"])
')
  weedshell swfs-broken-0 "lock\nvolume.mark -node $ip:8080 -volumeId $vid -readonly\nunlock\n" >/dev/null
fi
ro_marked() { proxy swfs-broken-0 9333 vol/status | grep -q '"ReadOnly":true'; }
poll 60 "datab volume read-only on the master" ro_marked
log "done"
