#!/usr/bin/env bash
# An inactive physical slot that retains a little WAL on the primary, for the pg-replication-lag
# fixtures; and its removal. Needs up.sh first.
#
#   idle-slot.sh create   create slot rw_fixture_idle (WAL reserved at once), then write ~1 MB of WAL
#                         in orders.lock_fixture, so the slot retains that much
#   idle-slot.sh drop     drop it (always do: an inactive slot keeps WAL on the disk until it is gone)
#
# Record in between:  trap 'idle-slot.sh drop' EXIT; idle-slot.sh create; <rwtask test --record ...>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"
SLOT_NAME="rw_fixture_idle"

sql() { # <database> <sql>
  docker exec -e PGPASSWORD="$SUPERUSER_PASSWORD" "$PRIMARY" \
    psql -X -At -q -h 127.0.0.1 -U "$SUPERUSER" -d "$1" -v ON_ERROR_STOP=1 -c "$2"
}

case "${1:-}" in
  create)
    sql postgres "SELECT pg_drop_replication_slot('${SLOT_NAME}') FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}' AND NOT active" >/dev/null
    sql postgres "SELECT slot_name FROM pg_create_physical_replication_slot('${SLOT_NAME}', true)" >/dev/null
    sql "$APP_DB" "INSERT INTO lock_fixture SELECT g, repeat('x', 400) FROM generate_series(1000, 3000) g" >/dev/null
    sql "$APP_DB" "DELETE FROM lock_fixture WHERE id >= 1000" >/dev/null
    log "slot ${SLOT_NAME}: $(sql postgres "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}'") bytes retained"
    ;;
  drop)
    sql postgres "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}'" >/dev/null
    log "slot ${SLOT_NAME} dropped"
    ;;
  *) echo "usage: $(basename "$0") create|drop" >&2; exit 2 ;;
esac
