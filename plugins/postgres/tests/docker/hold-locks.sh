#!/usr/bin/env bash
# Hold a lock chain on the primary for the pg-locks fixtures, and release it. Needs up.sh first.
#
#   hold-locks.sh start   a root blocker (idle in transaction, ACCESS EXCLUSIVE on orders.lock_fixture)
#                         and two waiters queued behind it, all with application_name rw-fixture-locks
#   hold-locks.sh stop    terminate every rw-fixture-locks backend and its docker client
#   hold-locks.sh status  list the rw-fixture-locks backends
#
# Record while the chain is up:  hold-locks.sh start; <rwtask test --record ...>; hold-locks.sh stop
# (use `trap 'hold-locks.sh stop' EXIT` so it is released even when the recording fails).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"
APP="rw-fixture-locks"
PIDFILE="${TMPDIR:-/tmp}/rw-fixture-locks.pids"

sql() { # <sql>: as the superuser on the primary, one value
  docker exec -e PGPASSWORD="$SUPERUSER_PASSWORD" "$PRIMARY" \
    psql -X -At -q -h 127.0.0.1 -U "$SUPERUSER" -d postgres -v ON_ERROR_STOP=1 -c "$1"
}

client() { # <stdin-script-or-empty> <-c sql>...: a backgrounded docker exec psql named $APP
  docker exec -i -e PGPASSWORD="$SUPERUSER_PASSWORD" -e PGAPPNAME="$APP" "$PRIMARY" \
    psql -X -q -h 127.0.0.1 -U "$SUPERUSER" -d "$APP_DB" -v ON_ERROR_STOP=1 "$@"
}

backends() { sql "SELECT count(*) FROM pg_stat_activity WHERE application_name = '${APP}'"; }

stop() {
  sql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '${APP}'" >/dev/null 2>&1 || true
  if [ -f "$PIDFILE" ]; then
    # shellcheck disable=SC2046
    kill $(cat "$PIDFILE") 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  log "released ${APP} backends"
}

case "${1:-}" in
  start)
    stop >/dev/null
    : > "$PIDFILE"
    # the root blocker: stdin stays open, so the session sits idle in transaction holding the lock
    { echo "BEGIN; LOCK TABLE lock_fixture IN ACCESS EXCLUSIVE MODE;"; sleep 300; } | client >/dev/null 2>&1 &
    echo $! >> "$PIDFILE"
    until [ "$(sql "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '${APP}' AND l.granted AND l.mode = 'AccessExclusiveLock'")" = 1 ]; do sleep 0.2; done
    for i in 1 2; do # two waiters; the second queues behind the first as well
      client -c "BEGIN; LOCK TABLE lock_fixture IN ACCESS EXCLUSIVE MODE; COMMIT" >/dev/null 2>&1 &
      echo $! >> "$PIDFILE"
      until [ "$(sql "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) WHERE a.application_name = '${APP}' AND NOT l.granted")" = "$i" ]; do sleep 0.2; done
    done
    sleep 2   # let the wait durations grow past a few hundred ms
    log "lock chain up: $(backends) ${APP} backends (1 root blocker, 2 waiters)"
    ;;
  stop) stop ;;
  status) sql "SELECT pid, state, wait_event_type, pg_blocking_pids(pid) AS blocked_by FROM pg_stat_activity WHERE application_name = '${APP}' ORDER BY pid" ;;
  *) echo "usage: $(basename "$0") start|stop|status" >&2; exit 2 ;;
esac
