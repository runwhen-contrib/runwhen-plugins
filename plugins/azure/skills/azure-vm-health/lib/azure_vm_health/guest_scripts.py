"""The fixed diagnostic scripts run-os-check sends into a Linux VM, and nothing else.

These are constants of the skill. No caller input, setting or API reply is ever put into them, so
the text that runs as root in the guest is exactly the text reviewed here (its SHA-256 is part of the
task's output). Each one is read-only: it reads /proc, runs `df` or asks the package manager what
it already knows from its local database. None of them refreshes a package index, installs, writes a
file, or prints a file's contents or the environment. Each prints a RW_BEGIN line, its facts, and a
RW_END line carrying the exit status of the one command that matters; stderr is not discarded.
A test (test_azure_vm_health.py) keeps this file free of verbs that change a machine."""

import hashlib

# Every command that can block (df on a hung mount, a package manager waiting on a lock or a mirror)
# runs under `timeout 30`; the exit status of a timeout is 124 and is read like any other failure.
# A client that gives up does not cancel a script already running in the guest: the 30 seconds are
# what bounds it there. df is `-l` (local file systems: a hung network mount cannot block it), and its
# rows are sorted by Use% and cut to 40 with the number of rows before the cut printed beside them.
DISK = """#!/bin/sh
# runwhen azure-vm-health run-os-check: disk v2. Read-only: df and nothing else.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN disk"
out=$(timeout 30 df -P -k -l -x tmpfs -x devtmpfs -x squashfs -x overlay 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "error=$(printf '%s\\n' "$out" | head -n 3)"
else
  printf '%s\\n' "$out" | head -n 1
  printf '%s\\n' "$out" | tail -n +2 | sort -k5 -n -r | head -n 40
  echo "rows_total=$(printf '%s\\n' "$out" | tail -n +2 | wc -l | tr -d ' ')"
fi
echo "RW_END disk rc=$rc"
exit 0
"""

MEMORY = """#!/bin/sh
# runwhen azure-vm-health run-os-check: memory v2. Read-only: reads /proc/meminfo.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN memory"
timeout 30 grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo
rc=$?
echo "RW_END memory rc=$rc"
exit 0
"""

UPTIME = """#!/bin/sh
# runwhen azure-vm-health run-os-check: uptime v2. Read-only: reads /proc/uptime and /proc/loadavg.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN uptime"
timeout 30 cat /proc/uptime /proc/loadavg
rc=$?
echo "RW_END uptime rc=$rc"
exit 0
"""

# The package manager is asked what it knows from its local database: apt-get -s (simulate) and
# yum/dnf -C (cache only) never refresh an index. `dist-upgrade` is the simulation that counts what
# `upgrade` keeps back (a new kernel or a package that needs a new dependency): those are updates
# too. A manager that errors or times out makes rc non-zero (dnf and yum use 100 for "updates are
# available"), and the error text goes to stdout under error=.
PATCHES = """#!/bin/sh
# runwhen azure-vm-health run-os-check: patches v2. Read-only: simulation and cache-only queries.
LC_ALL=C
export LC_ALL
echo "RW_BEGIN patches"
rc=0
if command -v apt-get >/dev/null 2>&1; then
  echo "manager=apt"
  out=$(timeout 30 apt-get -s -o Debug::NoLocking=1 dist-upgrade 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "error=$(printf '%s\\n' "$out" | head -n 3)"
  else
    echo "pending=$(printf '%s\\n' "$out" | grep -c '^Inst ')"
    echo "security=$(printf '%s\\n' "$out" | grep '^Inst ' | grep -c -i 'security')"
    echo "kept_back=$(printf '%s\\n' "$out" | awk '/have been kept back/{f=1;next} /^The following|^[0-9]+ upgraded/{f=0} f{n+=NF} END{print n+0}')"
    printf '%s\\n' "$out" | grep '^Inst ' | head -n 10 | awk '{print "package=" $2}'
  fi
  stamp=/var/lib/apt/periodic/update-success-stamp
  if [ -e "$stamp" ]; then
    echo "index_age_seconds=$(( $(date +%s) - $(timeout 30 stat -c %Y "$stamp") ))"
  else
    echo "index_age_seconds=unknown"
  fi
  if [ -e /var/run/reboot-required ]; then echo "reboot_required=yes"; else echo "reboot_required=no"; fi
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then tool=dnf; else tool=yum; fi
  echo "manager=$tool"
  out=$(timeout 30 $tool -C -q check-update 2>&1)
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
