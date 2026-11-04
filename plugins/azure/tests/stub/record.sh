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
# fixture's name. The target: healthy is kv-orders-prod (azure-aks-health: aks-orders-prod), not-found
# is a vault (a cluster) that does not exist, anything else is kv-orders-dev (aks-orders-dev); discover
# targets the subscription.
#
# A fixture may say more: `name=scenario@<group>/<plural>/<name>+input=value+input2=value`. The
# scenario is then not the fixture's name (azure-appservice-health has scenarios of its own, shared by
# its tasks), the target is the resource below the subscription (resourcegroups/<group>/<plural>/<name>)
# and each `+k=v` is a task input given to the recording. Fixtures of a write task are recorded with
# --dry-run when called `dry-run...`, else with --allow-write: against the stub nothing can change.
manifest=(
  "azure-base/discover:healthy forbidden throttled not-found group-forbidden"
  "azure-kv-health/resource-health:healthy broken forbidden throttled not-found"
  "azure-kv-health/availability:healthy broken forbidden throttled not-found"
  "azure-kv-health/configuration:healthy broken forbidden throttled not-found"
  "azure-kv-health/expiring-items:healthy broken forbidden throttled not-found data-plane-forbidden secrets-forbidden"
  "azure-kv-health/audit-failures:healthy broken forbidden throttled not-found no-diagnostics category-group dedicated"
  "azure-kv-health/performance:healthy broken forbidden throttled not-found"
  "azure-aks-health/resource-health:healthy broken forbidden throttled not-found"
  "azure-aks-health/cluster-status:healthy broken forbidden throttled not-found stopped monitor-forbidden"
  "azure-aks-health/node-pools:healthy broken forbidden throttled not-found stopped"
  "azure-aks-health/version-support:healthy broken forbidden throttled not-found"
  "azure-aks-health/activity-failures:healthy broken forbidden throttled not-found monitor-forbidden"
  "azure-aks-health/node-metrics:healthy broken forbidden throttled not-found stopped monitor-forbidden"
)

# --- azure-appservice-health ----------------------------------------------------------------------
APP_PROD=rg-orders-prod/web-apps/app-orders-api     APP_DEV=rg-orders-dev/web-apps/app-orders-web
FUNC_PROD=rg-orders-prod/web-apps/func-orders-jobs  FUNC_DEV=rg-orders-dev/web-apps/func-orders-ingest
PLAN_PROD=rg-orders-prod/app-service-plans/plan-orders-prod  PLAN_DEV=rg-orders-dev/app-service-plans/plan-orders-dev
APP_GONE=rg-orders-dev/web-apps/nope                PLAN_GONE=rg-orders-dev/app-service-plans/nope

std() {   # healthy-target broken-target gone-target [+inputs]: the five fixtures every task has
  local inputs="${4:-}"
  echo "healthy=appservice-healthy@$1$inputs broken=appservice-broken@$2$inputs forbidden=appservice-forbidden@$2$inputs" \
       "throttled=appservice-throttled@$2$inputs not-found=appservice-not-found@$3$inputs"
}
wr() {    # dry-run-scenario@target applied-scenario@target broken-target gone-target [+inputs]: a write task's fixtures
  local inputs="${5:-}"
  echo "dry-run=$1$inputs applied=$2$inputs forbidden=appservice-forbidden@$3$inputs throttled=appservice-throttled@$3$inputs" \
       "not-found=appservice-not-found@$4$inputs write-forbidden=appservice-write-forbidden@$3$inputs"
}
A=azure-appservice-health
manifest+=(
  "$A/app-state:$(std $APP_PROD $APP_DEV $APP_GONE)"
  "$A/plan-state:$(std $PLAN_PROD $PLAN_DEV $PLAN_GONE) via-app=appservice-broken@$APP_DEV"
  "$A/autoscale:$(std $PLAN_PROD $PLAN_DEV $PLAN_GONE) via-app=appservice-broken@$APP_DEV"
  "$A/resource-health:$(std $APP_PROD $APP_DEV $APP_GONE) plan-healthy=appservice-healthy@$PLAN_PROD plan-broken=appservice-broken@$PLAN_DEV"
  "$A/configuration:$(std $APP_PROD $APP_DEV $APP_GONE) config-forbidden=appservice-partial@$APP_DEV"
  "$A/deployments:$(std $APP_PROD $APP_DEV $APP_GONE) slots-forbidden=appservice-partial@$APP_DEV"
  "$A/http-metrics:$(std $APP_PROD $APP_DEV $APP_GONE) metric-forbidden=appservice-partial@$APP_DEV"
  "$A/function-metrics:$(std $FUNC_PROD $FUNC_DEV $APP_GONE) not-a-function=appservice-healthy@$APP_PROD"
  "$A/plan-metrics:$(std $PLAN_PROD $PLAN_DEV $PLAN_GONE) via-app=appservice-broken@$APP_DEV"
  "$A/restart:$(wr appservice-healthy@$APP_PROD appservice-healthy@$APP_PROD $APP_DEV $APP_GONE)"
  "$A/start:$(wr appservice-broken@$APP_DEV appservice-healthy@$APP_PROD $APP_DEV $APP_GONE)"
  "$A/stop:$(wr appservice-healthy@$APP_PROD appservice-broken@$APP_DEV $APP_DEV $APP_GONE)"
  "$A/swap-slot:$(wr appservice-healthy@$APP_PROD appservice-healthy@$APP_PROD $APP_DEV $APP_GONE +sourceSlot=staging) slot-not-found=appservice-healthy@$APP_PROD+sourceSlot=nope swap-in-progress=appservice-swap-busy@$APP_PROD+sourceSlot=staging"
  # scale: the plans' autoscale bounds are prod 2 to 5 and dev 2 to 3. The dry run asks for 4 of P2v3 on prod, the
  # applied one for the plan's own values (the healthy stub accepts it and nothing changes), the conflict one
  # for 5 on dev (refused) and the override for the same (a dry run); the refused writes ask for 3 of S3 on dev
  "$A/scale:dry-run=appservice-healthy@$PLAN_PROD+instances=4+sku=P2v3 dry-run-override=appservice-broken@$PLAN_DEV+instances=5+sku=S3+overrideAutoscale=true applied=appservice-healthy@$PLAN_PROD+instances=2+sku=P1v3 autoscale-conflict=appservice-broken@$PLAN_DEV+instances=5+sku=S3 forbidden=appservice-forbidden@$PLAN_DEV+instances=3+sku=S3 throttled=appservice-throttled@$PLAN_DEV+instances=3+sku=S3 not-found=appservice-not-found@$PLAN_GONE+instances=3+sku=S3 write-forbidden=appservice-write-forbidden@$PLAN_DEV+instances=3+sku=S3"
)
write_tasks=" $A/restart $A/start $A/stop $A/swap-slot $A/scale "

spec_parts() {     # fixture spec -> fixture scenario target inputs (one per line)
  local spec="$1" inputs="" target="" scenario fixture
  if [[ "$spec" == *+* ]]; then inputs="${spec#*+}"; spec="${spec%%+*}"; fi
  if [[ "$spec" == *@* ]]; then target="${spec#*@}"; spec="${spec%%@*}"; fi
  fixture="$spec"; scenario="$spec"
  if [[ "$spec" == *=* ]]; then fixture="${spec%%=*}"; scenario="${spec#*=}"; fi
  printf '%s\n%s\n%s\n%s\n' "$fixture" "$scenario" "$target" "$inputs"
}

resource_for() {   # task fixture
  local task="$1" fixture="$2" vault group
  if [ "$task" = "azure-base/discover" ]; then
    if [ "$fixture" = "not-found" ]; then echo "azure/subscriptions/$GONE"; else echo "azure/subscriptions/$SUB"; fi
    return
  fi
  case "$task" in
    azure-aks-health/*)
      case "$fixture" in
        healthy)   vault=aks-orders-prod; group=rg-orders-prod ;;
        not-found) vault=nope;            group=rg-orders-dev ;;
        *)         vault=aks-orders-dev;  group=rg-orders-dev ;;
      esac
      echo "azure/subscriptions/$SUB/resourcegroups/$group/aks-clusters/$vault"
      return ;;
  esac
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
  for spec in ${row#*:}; do
    { read -r fixture; read -r scenario; read -r target; read -r inputs; } < <(spec_parts "$spec")
    wanted "$task" "$fixture" || continue
    skill="${task%%/*}" name="${task#*/}"
    file="$plugin/skills/$skill/tests/$name/$fixture.yaml"
    old="$scratch/old.yaml"
    if [ -f "$file" ]; then cp "$file" "$old"; else rm -f "$old"; fi
    if [ -n "$target" ]; then resource="azure/subscriptions/$SUB/resourcegroups/$target"; else resource="$(resource_for "$task" "$fixture")"; fi
    extra=()
    if [[ "$write_tasks" == *" $task "* ]]; then
      case "$fixture" in dry-run*) extra+=(--dry-run) ;; *) extra+=(--allow-write) ;; esac
    fi
    if [ -n "$inputs" ]; then
      IFS=+ read -ra pairs <<< "$inputs"
      for pair in "${pairs[@]}"; do extra+=(--input "$pair"); done
    fi
    echo "record: $task/$fixture  (stub scenario $scenario, target $resource)"
    # the stub first on PATH; az's own state in a scratch directory; no credential variables
    env -u AZURE_CLIENT_ID -u AZURE_CLIENT_SECRET -u AZURE_TENANT_ID -u AZURE_SUBSCRIPTION_ID \
      PATH="$stub_bin:$PATH" AZURE_STUB_SCENARIO="$scenario" AZURE_CONFIG_DIR="$scratch/azure" \
      "$rwtask" test "$task" --plugin-dir "plugins/azure" --record --force \
        --scenario "$fixture" --resource "$resource" ${extra[@]+"${extra[@]}"} >/dev/null
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
for skill in azure-base azure-kv-health azure-aks-health azure-appservice-health; do
  env -u AZURE_STUB_SCENARIO -u AZ_STUB_SCENARIO PATH="$clean_path" \
    "$rwtask" test "$skill" --plugin-dir "plugins/azure"
done
echo "record: $recorded fixtures recorded and replayed with the stub off the PATH"
