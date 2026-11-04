#!/usr/bin/env bash
# Record the azure plugin's fixtures. This is the ONLY way they are recorded.
#
#   plugins/azure/tests/stub/record.sh [<skill>/<task>[/<fixture>] ...]
#
# Run it from anywhere (it finds the repository from its own path). RWTASK names the rwtask binary
# (default: rwtask on PATH). With no arguments every fixture of the manifest below is recorded; with
# arguments only those (`azure-kv-health/audit-failures`, or one fixture of it:
# `azure-kv-health/audit-failures/dedicated`).
#
# A bare `rwtask test --record` is not a recording path: it would run whatever `az` is first on the
# PATH, and on a machine with a logged-in az that is the real CLI, calling a real subscription. This
# script puts tests/stub/bin first on the PATH, names the stub scenario (AZURE_STUB_SCENARIO), and
# before recording anything it checks that `az` really is the stub: `az version` must answer with
# the stub's marker (the key "rw-azure-stub"). It then records each fixture with --force, keeps the
# hand-written `expect:` block the fixture already had (the recording writes only status and
# errorCodes; outputs are written by hand), and finally replays every fixture of the plugin with the
# stub OFF the PATH, so a fixture that only works while the stub is there fails here.
#
# A fixture that does not exist yet is recorded with the template expect block: write its expect
# block (outputs that prove the scenario) and run this script again.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin="$(cd "$here/../.." && pwd)"          # plugins/azure
repo="$(cd "$plugin/../.." && pwd)"          # the repository root
stub_bin="$here/bin"
rwtask="${RWTASK:-rwtask}"
marker='"rw-azure-stub": true'

SUB=4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71
GONE=00000000-0000-4000-8000-00000000dead

# skill/task  ->  the fixtures (stub scenarios) recorded for it. A fixture's stub scenario has the
# fixture's name. The target: healthy is kv-orders-prod, not-found is a vault that does not exist,
# anything else is kv-orders-dev (discover targets the subscription).
manifest=(
  "azure-base/discover:healthy forbidden throttled not-found group-forbidden"
  "azure-kv-health/resource-health:healthy broken forbidden throttled not-found"
  "azure-kv-health/availability:healthy broken forbidden throttled not-found"
  "azure-kv-health/configuration:healthy broken forbidden throttled not-found"
  "azure-kv-health/expiring-items:healthy broken forbidden throttled not-found data-plane-forbidden secrets-forbidden"
  "azure-kv-health/audit-failures:healthy broken forbidden throttled not-found no-diagnostics category-group dedicated"
  "azure-kv-health/performance:healthy broken forbidden throttled not-found"
)

resource_for() {   # task fixture
  local task="$1" fixture="$2" vault group
  if [ "$task" = "azure-base/discover" ]; then
    if [ "$fixture" = "not-found" ]; then echo "azure/subscriptions/$GONE"; else echo "azure/subscriptions/$SUB"; fi
    return
  fi
  case "$fixture" in
    healthy)   vault=kv-orders-prod; group=rg-orders-prod ;;
    not-found) vault=nope;           group=rg-orders-dev ;;
    *)         vault=kv-orders-dev;  group=rg-orders-dev ;;
  esac
  echo "azure/subscriptions/$SUB/resourcegroups/$group/keyvaults/$vault"
}

# --- the az on PATH must be the stub -----------------------------------------------------------
check_stub() {
  local found
  found="$(PATH="$stub_bin:$PATH" command -v az || true)"
  if [ "$found" != "$stub_bin/az" ]; then
    echo "record.sh: az on the PATH is $found, not the stub $stub_bin/az; refusing to record" >&2
    exit 1
  fi
  if ! PATH="$stub_bin:$PATH" AZURE_STUB_SCENARIO=healthy az version 2>/dev/null | grep -q "$marker"; then
    echo "record.sh: 'az version' did not answer with the stub marker ($marker); refusing to record" >&2
    exit 1
  fi
}

# --- keep the hand-written expect block across a recording ----------------------------------------
keep_expect() {    # old_file new_file
  python3 - "$1" "$2" <<'PY'
import re, sys
old_path, new_path = sys.argv[1:]
def split(text):
    m = re.search(r"^expect:\n", text, re.M)
    return (text[: m.start()], text[m.start():]) if m else (text, "")
old = open(old_path, encoding="utf-8").read()
new = open(new_path, encoding="utf-8").read()
_, old_expect = split(old)
head, _ = split(new)
if old_expect:
    open(new_path, "w", encoding="utf-8").write(head + old_expect)
PY
}

selected=" $* "      # the arguments, space-separated; empty means everything
wanted() {         # task fixture -> is it selected by the arguments?
  [ "$selected" = "  " ] && return 0
  case "$selected" in *" $1 "* | *" $1/$2 "*) return 0 ;; esac
  return 1
}

check_stub
cd "$repo"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
recorded=0
for row in "${manifest[@]}"; do
  task="${row%%:*}"
  for fixture in ${row#*:}; do
    wanted "$task" "$fixture" || continue
    skill="${task%%/*}" name="${task#*/}"
    file="$plugin/skills/$skill/tests/$name/$fixture.yaml"
    old="$scratch/old.yaml"
    if [ -f "$file" ]; then cp "$file" "$old"; else rm -f "$old"; fi
    echo "record: $task/$fixture  (stub scenario $fixture, target $(resource_for "$task" "$fixture"))"
    # the stub first on PATH; az's own state in a scratch directory; no credential variables
    env -u AZURE_CLIENT_ID -u AZURE_CLIENT_SECRET -u AZURE_TENANT_ID -u AZURE_SUBSCRIPTION_ID \
      PATH="$stub_bin:$PATH" AZURE_STUB_SCENARIO="$fixture" AZURE_CONFIG_DIR="$scratch/azure" \
      "$rwtask" test "$task" --plugin-dir "plugins/azure" --record --force \
        --scenario "$fixture" --resource "$(resource_for "$task" "$fixture")" >/dev/null
    if [ -f "$old" ]; then
      keep_expect "$old" "$file"
    else
      echo "record: $task/$fixture is NEW: write its expect block (outputs) and run record.sh again" >&2
    fi
    recorded=$((recorded + 1))
  done
done
[ "$recorded" -gt 0 ] || { echo "record.sh: nothing matched $*" >&2; exit 1; }

# --- replay with the stub off the PATH ---------------------------------------------------------------
clean_path="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$stub_bin" | paste -sd: -)"
for skill in azure-base azure-kv-health; do
  env -u AZURE_STUB_SCENARIO -u AZ_STUB_SCENARIO PATH="$clean_path" \
    "$rwtask" test "$skill" --plugin-dir "plugins/azure"
done
echo "record: $recorded fixtures recorded and replayed with the stub off the PATH"
