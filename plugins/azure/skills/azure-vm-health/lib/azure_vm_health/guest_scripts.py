"""The fixed diagnostic scripts run-os-check sends into a Linux VM, and nothing else.

These are constants of the skill. No caller input, setting or API reply is ever put into them, so
the text that runs as root in the guest is exactly the text reviewed here (its SHA-256 is part of the
task's output). Each one is read-only: it reads /proc, runs `df` or asks the package manager what
it already knows from its local database. None of them refreshes a package index, installs, writes a
file, or prints a file's contents or the environment. Each prints a RW_BEGIN line, its facts, and a
RW_END line carrying the exit status of the one command that matters; stderr is not discarded.
A test (test_azure_vm_health.py) keeps this file free of verbs that change a machine."""

import hashlib

DISK = """#!/bin/sh
# runwhen azure-vm-health run-os-check: disk v1. Read-only: df and nothing else.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN disk"
df -P -k -x tmpfs -x devtmpfs -x squashfs -x overlay
rc=$?
echo "RW_END disk rc=$rc"
exit 0
"""

MEMORY = """#!/bin/sh
# runwhen azure-vm-health run-os-check: memory v1. Read-only: reads /proc/meminfo.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN memory"
grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo
rc=$?
echo "RW_END memory rc=$rc"
exit 0
"""

UPTIME = """#!/bin/sh
# runwhen azure-vm-health run-os-check: uptime v1. Read-only: reads /proc/uptime and /proc/loadavg.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN uptime"
cat /proc/uptime /proc/loadavg
rc=$?
echo "RW_END uptime rc=$rc"
exit 0
"""

# The package manager is asked what it knows from its local database: apt-get -s (simulate) and
# yum/dnf -C (cache only) never refresh an index. A manager that errors makes rc non-zero (dnf and
# yum use 100 for "updates are available"), and the error text goes to stdout under error=.
PATCHES = """#!/bin/sh
# runwhen azure-vm-health run-os-check: patches v1. Read-only: simulation and cache-only queries.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN patches"
rc=0
if command -v apt-get >/dev/null 2>&1; then
  echo "manager=apt"
  out=$(apt-get -s -o Debug::NoLocking=1 upgrade 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "error=$(printf '%s\\n' "$out" | head -n 3)"
  else
    echo "pending=$(printf '%s\\n' "$out" | grep -c '^Inst ')"
    echo "security=$(printf '%s\\n' "$out" | grep '^Inst ' | grep -c -i 'security')"
    printf '%s\\n' "$out" | grep '^Inst ' | head -n 10 | awk '{print "package=" $2}'
  fi
  stamp=/var/lib/apt/periodic/update-success-stamp
  if [ -e "$stamp" ]; then
    echo "index_age_seconds=$(( $(date +%s) - $(stat -c %Y "$stamp") ))"
  else
    echo "index_age_seconds=unknown"
  fi
  if [ -e /var/run/reboot-required ]; then echo "reboot_required=yes"; else echo "reboot_required=no"; fi
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then tool=dnf; else tool=yum; fi
  echo "manager=$tool"
  out=$($tool -C -q check-update 2>&1)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "pending=0"
  elif [ "$rc" -eq 100 ]; then
    rc=0
    echo "pending=$(printf '%s\\n' "$out" | grep -c -E '^[[:alnum:]_.+-]+\\.[[:alnum:]_]+[[:space:]]+[^[:space:]]+[[:space:]]+[^[:space:]]+')"
  else
    echo "error=$(printf '%s\\n' "$out" | head -n 3)"
  fi
  echo "index_age_seconds=unknown"
  echo "reboot_required=unknown"
else
  echo "manager=unknown"
fi
echo "RW_END patches rc=$rc"
exit 0
"""

SCRIPTS = {"disk": DISK, "memory": MEMORY, "uptime": UPTIME, "patches": PATCHES}
CHECKS = tuple(SCRIPTS)


def sha256(check):
    """The SHA-256 of the script a check sends: what a reviewer can match against this file."""
    return hashlib.sha256(SCRIPTS[check].encode("utf-8")).hexdigest()
