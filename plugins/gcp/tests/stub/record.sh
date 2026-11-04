#!/bin/sh
# Records one fixture of the gcp plugin against the stub, through harness.py, always.
#
#   plugins/gcp/tests/stub/record.sh <skill>[/<task>] <stub-scenario> [rwtask test args...]
#
# for example
#   record.sh gcp-bucket-health/bucket-config healthy --resource gcp/projects/shop-prod-1234
#   record.sh gcp-bucket-health/bucket-size broken --scenario broken-bucket \
#       --resource gcp/projects/shop-prod-1234/buckets/shop-archive
#
# The fixture is named after the stub scenario unless the arguments carry their own `--scenario`
# (a second fixture of the same stub scenario, such as broken-bucket). Add `--force` to replace one.
# rwtask is `$RWTASK`, else the one on PATH. This is the ONLY way to record: a bare
# `rwtask test --record` would run this machine's real gcloud, which is logged in to shared
# infrastructure (see README.md).
set -eu
if [ "$#" -lt 2 ]; then
  sed -n '2,13p' "$0" >&2
  exit 2
fi
skill=$1
scenario=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../../.." && pwd)   # the repository root: plugins/gcp/tests/stub -> ../../../..
named=no
for arg in "$@"; do
  [ "$arg" = "--scenario" ] && named=yes
done
[ "$named" = yes ] || set -- --scenario "$scenario" "$@"
cd "$root"
exec python3 "$here/harness.py" "$scenario" -- "${RWTASK:-rwtask}" test "$skill" \
  --plugin-dir plugins/gcp --record "$@"
