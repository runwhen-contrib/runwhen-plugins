#!/usr/bin/env bash
# Workload for the pg-connections and pg-long-running-queries fixtures: sessions with
# application_name rw-fixture-sessions on the primary, started as detached `docker exec psql`
# processes. They never run DDL or write.
#
#   sessions.sh start    3 idle sessions (orders, postgres), 1 idle in transaction, 1 pg_sleep(600)
#   sessions.sh stop     terminate exactly the backends named rw-fixture-sessions
#   sessions.sh status   list them
#
# Other fixtures share this server (rw-fixture-locks, ...): this script touches only its own
# application_name, and never restarts or removes a container.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"
APP="rw-fixture-sessions"
HOLD=600   # seconds an idle session stays open on its own, whatever happens to this script

su_psql() {
  docker exec -i -e PGPASSWORD="$SUPERUSER_PASSWORD" "$PRIMARY" \
    psql -X -q -At -h 127.0.0.1 -U "$SUPERUSER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

# in_container <database> <sh pipeline reading psql's stdin or using -c>
detached() { # <database> <stdin sql> <hold seconds, 0 = none> [psql args...]
  local db=$1 sql=$2 hold=$3; shift 3
  docker exec -d -e PGAPPNAME="$APP" -e PGPASSWORD="$MONITOR_PASSWORD" "$PRIMARY" \
    sh -c "(echo \"$sql\"; sleep $hold) | psql -X -q -h 127.0.0.1 -U $MONITOR -d $db >/dev/null"
}

count() { su_psql -c "SELECT count(*) FROM pg_stat_activity WHERE application_name = '${APP}'"; }

case "${1:-}" in
  start)
    [[ "$(count)" == "0" ]] || { log "already running ($(count) sessions)"; exit 0; }
    detached orders   "select 1;" "$HOLD"
    detached orders   "select 1;" "$HOLD"
    detached postgres "select 1;" "$HOLD"
    detached orders   "begin; select 1;" "$HOLD"          # idle in transaction
    docker exec -d -e PGAPPNAME="$APP" -e PGPASSWORD="$MONITOR_PASSWORD" "$PRIMARY" \
      psql -X -q -h 127.0.0.1 -U "$MONITOR" -d orders -c "select pg_sleep(600), 'rw-fixture' as marker, 42 as n"
    end=$((SECONDS + 20))
    until [[ "$(count)" == "5" ]]; do
      (( SECONDS < end )) || { log "TIMEOUT: expected 5 sessions, have $(count)"; exit 1; }
      sleep 1
    done
    log "started 5 sessions (${APP})" ;;
  stop)
    su_psql -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = '${APP}' AND pid <> pg_backend_pid()" >/dev/null
    log "terminated ${APP} sessions" ;;
  status)
    su_psql -F ' ' -c "SELECT pid, datname, usename, state, left(query, 40) FROM pg_stat_activity WHERE application_name = '${APP}'" ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
