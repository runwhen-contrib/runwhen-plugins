#!/usr/bin/env bash
# Capture azure-aks-health's stub answers from the pinned az CLI and put them in the scenarios.
#
#   plugins/azure/tests/stub/capture/capture_aks.sh
#
# Runs capture_aks.py inside the azure@1 runtime image with no network (`docker build` it first as
# rwp-runtime-azure:dev, or name another image in AZURE_RUNTIME_IMAGE): the real az, with its sign-in
# and its HTTP replaced by fake_az.py, against the ARM bodies of aks_world.py. Then splice_aks.py
# copies the stdout files into tests/stub/scenarios/<scenario>/ and the calls into each calls.yaml.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${AZURE_RUNTIME_IMAGE:-rwp-runtime-azure:dev}"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
docker run --rm --network none -v "$here:/capture:ro" -v "$out:/out" \
  --entrypoint /opt/az/bin/python "$image" -I /capture/capture_aks.py /out
python3 "$here/splice_aks.py" "$out" "$here/../scenarios"
