#!/bin/sh
# Rewrites tests/stub/scenarios/* from the real gcloud of the runtime image, against a local fake
# of the Google APIs, with no network. Needs the image: docker build -t rwp-runtime-gcp:dev runtimes/gcp
# Usage (from anywhere): plugins/gcp/tests/stub/capture/capture.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
stub=$(dirname "$here")
lib=$(dirname "$stub")/../skills/gcp-bucket-health/lib
lib=$(cd "$lib" && pwd)
exec docker run --rm --network none \
  -v "$stub":/stub -v "$lib":/plugin-lib:ro \
  --entrypoint sh rwp-runtime-gcp:dev -c 'python3 /stub/capture/capture.py'
