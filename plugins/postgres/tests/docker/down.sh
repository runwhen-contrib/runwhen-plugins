#!/usr/bin/env bash
# Remove the local postgres test containers, their volumes and the docker network.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"
docker rm -fv "$STANDBY" "$PRIMARY" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
log "removed ${PRIMARY}, ${STANDBY} and ${NET}"
