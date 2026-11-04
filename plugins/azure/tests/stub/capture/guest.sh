#!/usr/bin/env bash
# Render the guest side of the run-os-check scenarios: what the four fixed scripts of
# lib/azure_vm_health/guest_scripts.py print.
#
#   plugins/azure/tests/stub/capture/guest.sh
#
# The scripts are run for real, in a throwaway debian container with no network, so their formatting is the
# machine's own: `patches` against a stand-in `apt-get` that prints the documented `apt-get -s` simulation
# lines (apt-get itself would need an index; the container has none) and a 40-day-old
# update-success-stamp; `disk`, `memory` and `uptime` print the container's real df, /proc/meminfo and
# /proc/uptime. Only the figures of disk, memory and uptime are then replaced by the scenario's (the
# `*-broken.txt` files are the same output with a nearly full disk, little memory and a long uptime): the
# stub cannot make a container's disk 93% full. The result is committed in capture/guest/*.txt; this
# script is how the format was obtained, and re-running it overwrites the patches files only.
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
cat > "$scripts/run-patches.sh" <<'IN'
set -eu
mkdir -p /fake /var/lib/apt/periodic /var/run
cat > /fake/apt-get <<'APT'
#!/bin/sh
# the documented `apt-get -s upgrade` lines
cat <<'OUT'
Reading package lists...
Building dependency tree...
Reading state information...
Calculating upgrade...
The following packages will be upgraded:
  libssl3t64 openssl linux-image-azure
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst openssl [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst linux-image-azure [6.8.0.1030.36] (6.8.0.1031.37 Ubuntu:24.04/noble-updates [amd64])
Conf libssl3t64 (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Conf openssl (3.0.13-0ubuntu3.5 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Conf linux-image-azure (6.8.0.1031.37 Ubuntu:24.04/noble-updates [amd64])
OUT
APT
chmod +x /fake/apt-get
touch -d '40 days ago' /var/lib/apt/periodic/update-success-stamp
touch /var/run/reboot-required
PATH=/fake:$PATH sh /scripts/patches.sh
IN
docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh /scripts/run-patches.sh > "$here/guest/patches-broken.txt"
docker run --rm --network none -v "$scripts:/scripts:ro" debian:trixie-slim sh -c 'mkdir -p /var/lib/apt/periodic; touch -d "1 hour ago" /var/lib/apt/periodic/update-success-stamp; sh /scripts/patches.sh' > "$here/guest/patches-healthy.txt"
echo "wrote capture/guest/patches-broken.txt and patches-healthy.txt"
