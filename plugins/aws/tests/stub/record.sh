#!/bin/sh
# Record one fixture through the stub, never against a bare `aws`.
#
#   plugins/aws/tests/stub/record.sh <skill>/<task> <fixture> <stub-scenario> <resource-path> [rwtask test options]
#
#   plugins/aws/tests/stub/record.sh aws-lambda-health/function-config healthy healthy \
#     aws/accounts/123456789012/regions/eu-west-1/lambda-functions/orders-api
#
# <fixture> is the file name under skills/<skill>/tests/<task>/ (it may differ from the stub scenario,
# which says what the fake CLI answers). The harness puts the stub first on PATH, names the scenario
# in AWS_STUB_SCENARIO (the runner strips every RW_* variable, so no RW_ name would arrive), drops
# the caller's own AWS credential variables so a real account is never reached, and refuses to
# start unless `aws` resolves to the stub. `--force` overwrites the fixture: re-add its `expect`.
# Replay needs none of this: `rwtask test <skill> --plugin-dir plugins/aws` uses the recorded calls.
set -eu

[ "$#" -ge 4 ] || { sed -n '2,13p' "$0" >&2; exit 2; }
task="$1"; fixture="$2"; scenario="$3"; resource="$4"; shift 4

here="$(cd "$(dirname "$0")" && pwd)"            # .../plugins/aws/tests/stub
plugin="$(cd "$here/../.." && pwd)"             # .../plugins/aws
[ -f "$here/scenarios/$scenario/calls.yaml" ] || { echo "no stub scenario '$scenario' under $here/scenarios" >&2; exit 2; }

PATH="$here/bin:$PATH"
export PATH
AWS_STUB_SCENARIO="$scenario"
export AWS_STUB_SCENARIO
found="$(command -v aws || true)"
[ "$found" = "$here/bin/aws" ] || { echo "aws resolves to '$found', not the stub; refusing to record" >&2; exit 1; }
# no ambient AWS credential, profile or endpoint may reach a recording
for name in $(env | sed -n 's/^\(AWS_[A-Z_]*\)=.*/\1/p'); do
  [ "$name" = "AWS_STUB_SCENARIO" ] || unset "$name"
done

exec "${RWTASK:-rwtask}" test "$task" --plugin-dir "$plugin" --record --force \
  --scenario "$fixture" --resource "$resource" "$@"
