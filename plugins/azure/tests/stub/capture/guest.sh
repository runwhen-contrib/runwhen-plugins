#!/usr/bin/env bash
# Render the guest side of the run-os-check scenarios: what the four fixed scripts of
# lib/azure_vm_health/guest_scripts.py print.
#
#   plugins/azure/tests/stub/capture/guest.sh
#
# The scripts are run for real, in a throwaway debian container with no network, so their formatting is the
# machine's own: `patches` against a local apt repository built by apt-repo.sh (real `apt-get -s dist-upgrade` output, a
# kept-back kernel included) and a 40-day-old update-success-stamp; `disk`, `memory` and `uptime` print the container's real df, /proc/meminfo and
# /proc/uptime. Only the figures of disk, memory and uptime are then replaced by the scenario's (the
# `*-broken.txt` files are the same output with a nearly full disk, little memory and a long uptime): the
# stub cannot make a container's disk 93% full. The result is committed in capture/guest/*.txt; this
# script is how the format was obtained, and re-running it overwrites the patches files and apt-upgrade-vs-dist-upgrade.txt only.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts="$(mktemp -d)"
trap 'rm -rf "$scripts"' EXIT
python3 -I - "$here/../../../skills/azure-vm-health/lib" "$scripts" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from azure_vm_health.guest_scripts import SCRIPTS
for name, text in SCRIPTS.items():
    open(f"{sys.argv[2]}/{name}.sh", "w").write(text)
PY
cp "$here/apt-repo.sh" "$scripts/apt-repo.sh"
# the real simulation: `upgrade` against `dist-upgrade` on that repository, for the record
docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh -c \
  'sh /scripts/apt-repo.sh; echo "=== upgrade"; apt-get -s -o Debug::NoLocking=1 upgrade; echo "=== dist-upgrade"; apt-get -s -o Debug::NoLocking=1 dist-upgrade' \
  > "$here/guest/apt-upgrade-vs-dist-upgrade.txt"
docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh -c \
  'sh /scripts/apt-repo.sh; mkdir -p /var/lib/apt/periodic /var/run; touch -d "40 days ago" /var/lib/apt/periodic/update-success-stamp; touch /var/run/reboot-required; sh /scripts/patches.sh' \
  > "$here/guest/patches-broken.txt"
docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh -c \
  'mkdir -p /var/lib/apt/periodic; touch -d "1 hour ago" /var/lib/apt/periodic/update-success-stamp; sh /scripts/patches.sh' \
  > "$here/guest/patches-healthy.txt"
# what disk, memory and uptime print on a real machine, to compare with guest/{disk,memory,uptime}-*.txt
for check in disk memory uptime; do
  docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh "/scripts/$check.sh" > "$scripts/$check.real"
done
echo "wrote capture/guest/patches-*.txt and apt-upgrade-vs-dist-upgrade.txt; real formats of disk, memory, uptime:"
cat "$scripts"/*.real
