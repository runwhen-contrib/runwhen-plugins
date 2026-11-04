#!/bin/sh
# Check the canned CLI output of every stub scenario against the pinned aws CLI in the runtime
# image, served by a fake HTTP endpoint (plan C7). Needs docker and the image:
#   docker build -t rwp-runtime-aws:dev runtimes/aws
#   plugins/aws/tests/stub/verify/verify.sh [--write] [scenario ...]
# Runs as the runner does (uid 65532, read-only root), no network but loopback.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
image="${AWS_RUNTIME_IMAGE:-rwp-runtime-aws:dev}"
if [ "${1:-}" = "--write" ]; then mode=rw; else mode=ro; fi
exec docker run --rm ${VERIFY_DEBUG:+-e VERIFY_DEBUG=1} --network none --user 65532 --read-only --tmpfs /work:rw,mode=1777 -e TMPDIR=/work \
  -v "$here:/stub:$mode" --entrypoint python3 "$image" /stub/verify/verify.py "$@"
