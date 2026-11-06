# Source this (do not run it) to point psql and rwtask at the local test containers:
#
#   source plugins/postgres/tests/docker/env.sh
#   rw_pg_use rw_monitor                # primary, the healthy identity
#   rw_pg_use rw_noperm                 # primary, login only
#   rw_pg_use rw_monitor standby        # the streaming standby on 55433
#   rw_pg_use rw_monitor primary orders # another database
#   rw_pg_use rw_monitor closed         # a port nothing listens on (unreachable)
#
# It exports the libpq variables the postgres/libpq credential would: PGHOST, PGPORT, PGUSER,
# PGPASSWORD, PGDATABASE, PGSSLMODE. Test values only.
_RW_PG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=common.sh
source "${_RW_PG_DIR}/common.sh"

# macOS: Homebrew's libpq is keg-only, so psql is not on PATH by default.
if ! command -v psql >/dev/null 2>&1; then
  for d in /opt/homebrew/opt/libpq/bin /usr/local/opt/libpq/bin; do
    [ -x "$d/psql" ] && PATH="$d:$PATH" && export PATH && break
  done
fi

rw_pg_use() { # <role> [primary|standby|closed] [database]
  local role=${1:?role}; local where=${2:-primary}; local db=${3:-postgres}
  case "$role" in
    "$MONITOR") export PGPASSWORD="$MONITOR_PASSWORD" ;;
    "$NOPERM") export PGPASSWORD="$NOPERM_PASSWORD" ;;
    "$SUPERUSER") export PGPASSWORD="$SUPERUSER_PASSWORD" ;;
    *) echo "unknown role $role" >&2; return 1 ;;
  esac
  case "$where" in
    primary) export PGPORT=$PRIMARY_PORT ;;
    standby) export PGPORT=$STANDBY_PORT ;;
    closed) export PGPORT=55439 ;;
    *) echo "unknown target $where" >&2; return 1 ;;
  esac
  # the local servers have no TLS, and the integration defaults to sslmode=require: disable it here only
  export PGHOST=127.0.0.1 PGUSER="$role" PGDATABASE="$db" PGSSLMODE=disable
}
