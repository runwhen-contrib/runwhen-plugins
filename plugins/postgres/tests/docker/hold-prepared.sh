#!/usr/bin/env bash
# Hold a lock in a PREPARED TRANSACTION on the primary and queue a waiter behind it, for the
# pg-locks and pg-long-running-queries fixtures; and remove both. Needs up.sh first
# (max_prepared_transactions is 10 there).
#
#   hold-prepared.sh start    PREPARE TRANSACTION 'rw_fixture_prepared' holding ACCESS EXCLUSIVE on
#                             orders.lock_fixture (the session that prepared it is gone), and one
#                             waiter behind it, application_name rw-fixture-prepared
#   hold-prepared.sh stop     terminate the waiter and ROLLBACK PREPARED the transaction
#   hold-prepared.sh status   the prepared transaction and the waiter
#
# A prepared transaction outlives every connection and holds its locks until it is committed or
# rolled back (it also keeps vacuum from cleaning up), so ALWAYS stop:
#   trap 'hold-prepared.sh stop' EXIT; hold-prepared.sh start; <rwtask test --record ...>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"
APP="rw-fixture-prepared"
GID="rw_fixture_prepared"
PIDFILE="${TMPDIR:-/tmp}/rw-fixture-prepared.pids"

sql() { # <database> <sql>: as the superuser on the primary, one value
  docker exec -e PGPASSWORD="$SUPERUSER_PASSWORD" "$PRIMARY" \
    psql -X -At -q -h 127.0.0.1 -U "$SUPERUSER" -d "$1" -v ON_ERROR_STOP=1 -c "$2"
}

stop() {
  sql postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '${APP}'" >/dev/null 2>&1 || true
  if [ -f "$PIDFILE" ]; then
    # shellcheck disable=SC2046
    kill $(cat "$PIDFILE") 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  # the waiter must be gone before the lock is released, or it would take the lock and commit
  # shellcheck disable=SC2015
  until [ "$(sql postgres "SELECT count(*) FROM pg_stat_activity WHERE application_name = '${APP}'" 2>/dev/null || echo 0)" = 0 ]; do sleep 0.2; done
  if [ "$(sql postgres "SELECT count(*) FROM pg_prepared_xacts WHERE gid = '${GID}'" 2>/dev/null || echo 0)" != 0 ]; then
    sql "$APP_DB" "ROLLBACK PREPARED '${GID}'" >/dev/null
  fi
  log "rolled back ${GID}, released ${APP} backends"
}

case "${1:-}" in
  start)
    stop >/dev/null
    : > "$PIDFILE"
    sql "$APP_DB" "BEGIN; LOCK TABLE lock_fixture IN ACCESS EXCLUSIVE MODE; PREPARE TRANSACTION '${GID}'" >/dev/null
    docker exec -i -e PGPASSWORD="$SUPERUSER_PASSWORD" -e PGAPPNAME="$APP" "$PRIMARY" \
      psql -X -q -h 127.0.0.1 -U "$SUPERUSER" -d "$APP_DB" -v ON_ERROR_STOP=1 \
      -c "BEGIN; LOCK TABLE lock_fixture IN ACCESS EXCLUSIVE MODE; COMMIT" >/dev/null 2>&1 &
    echo $! >> "$PIDFILE"
    until [ "$(sql postgres "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '${APP}' AND NOT l.granted")" = 1 ]; do sleep 0.2; done
    sleep 2   # let the wait duration grow past a few hundred ms
    log "prepared transaction ${GID} holds lock_fixture; 1 waiter (${APP}) behind it"
    ;;
  stop) stop ;;
  status)
    sql postgres "SELECT gid, owner, database, prepared FROM pg_prepared_xacts"
    sql postgres "SELECT pid, state, wait_event_type, pg_blocking_pids(pid) AS blocked_by FROM pg_stat_activity WHERE application_name = '${APP}' ORDER BY pid" ;;
  *) echo "usage: $(basename "$0") start|stop|status" >&2; exit 2 ;;
esac
