#!/bin/sh
# Rewrites the Cloud Functions scenarios (functions-*) and adds the `functions list` call to the
# bucket pilot's healthy, broken and project-denied scenarios, from the real gcloud of the runtime
# image, against a local fake of the Google APIs, with no network.
# Needs the image: docker build -t rwp-runtime-gcp:dev runtimes/gcp
# Usage (from anywhere): plugins/gcp/tests/stub/capture/capture_functions.sh
# CAPTURE_ONLY=functions-split,healthy rewrites just those scenarios (a full run takes minutes).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
stub=$(dirname "$here")
lib=$(dirname "$stub")/../skills/gcp-functions-health/lib
lib=$(cd "$lib" && pwd)
exec docker run --rm --network none -e CAPTURE_ONLY="${CAPTURE_ONLY:-}" \
  -v "$stub":/stub -v "$lib":/plugin-lib:ro \
  --entrypoint sh rwp-runtime-gcp:dev -c 'python3 /stub/capture/capture_functions.py'
