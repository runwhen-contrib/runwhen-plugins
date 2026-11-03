#!/usr/bin/env bash
# Start (or reuse) a local PostgreSQL 16 primary on 127.0.0.1:55432 and a streaming standby
# on 127.0.0.1:55433, create the test roles and the `orders` database, and apply
# scenarios/*.sql. Idempotent. Docker only: no shared cluster, nothing leaves 127.0.0.1.
#
#   rw_monitor   LOGIN, member of pg_monitor, CONNECT on orders      (the healthy identity)
#   rw_noperm    LOGIN only; CONNECT on orders revoked from PUBLIC   (the forbidden identity)
#   orders       the app database, owned by NOLOGIN role orders_owner
#
# max_prepared_transactions is 10 on BOTH (a standby needs at least the primary's value), so the
# prepared-transaction fixture (hold-prepared.sh) can PREPARE TRANSACTION.
#
# Use it:  source plugins/postgres/tests/docker/env.sh; rw_pg_use rw_monitor
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]; }
exists() { docker inspect "$1" >/dev/null 2>&1; }   # running or stopped: a stopped one keeps its data

# psql_in <container> <user> <password> [psql args...]: psql inside a container over TCP.
psql_in() {
  local c=$1 u=$2 p=$3; shift 3
  docker exec -i -e PGPASSWORD="$p" "$c" psql -X -q -h 127.0.0.1 -U "$u" -v ON_ERROR_STOP=1 "$@"
}

wait_ready() { # <container> <timeout-seconds>
  local end=$((SECONDS + $2))
  until docker exec "$1" pg_isready -q -h 127.0.0.1 -U "$SUPERUSER"; do
    (( SECONDS < end )) || { log "TIMEOUT waiting for $1"; docker logs --tail 20 "$1" >&2; return 1; }
    sleep 1
  done
}

docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null

# 1. primary
if running "$PRIMARY"; then
  log "primary already running"
elif exists "$PRIMARY"; then
  log "starting stopped primary"
  docker start "$PRIMARY" >/dev/null
else
  log "starting primary on 127.0.0.1:${PRIMARY_PORT}"
  docker run -d --name "$PRIMARY" --network "$NET" -p "127.0.0.1:${PRIMARY_PORT}:5432" \
    -e POSTGRES_PASSWORD="$SUPERUSER_PASSWORD" "$IMAGE" \
    -c wal_level=replica -c max_wal_senders=10 -c max_replication_slots=10 \
    -c hot_standby=on -c listen_addresses='*' \
    -c max_prepared_transactions=10 >/dev/null
fi
wait_ready "$PRIMARY" 90

# 2. roles, database, grants (idempotent)
psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d postgres <<SQL
SET client_min_messages = warning;
SELECT 'CREATE ROLE ${REPLICATOR} REPLICATION LOGIN PASSWORD ''${REPLICATOR_PASSWORD}'''
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${REPLICATOR}') \gexec
SELECT 'CREATE ROLE ${MONITOR} LOGIN PASSWORD ''${MONITOR_PASSWORD}'''
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${MONITOR}') \gexec
SELECT 'CREATE ROLE ${NOPERM} LOGIN PASSWORD ''${NOPERM_PASSWORD}'''
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${NOPERM}') \gexec
SELECT 'CREATE ROLE orders_owner NOLOGIN'
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orders_owner') \gexec
SELECT 'CREATE DATABASE ${APP_DB} OWNER orders_owner ENCODING ''UTF8'' TEMPLATE template0'
  WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${APP_DB}') \gexec
GRANT pg_monitor TO ${MONITOR};
REVOKE CONNECT ON DATABASE ${APP_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${APP_DB} TO ${MONITOR}, orders_owner;
SQL

# replication connections are not matched by the image's `host all all all` line
if ! docker exec "$PRIMARY" grep -q '^host replication all all' /var/lib/postgresql/data/pg_hba.conf; then
  docker exec "$PRIMARY" sh -c "echo 'host replication all all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf"
  psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d postgres -c 'SELECT pg_reload_conf()' >/dev/null
fi

# 3. a little data so orders has a size and an owner worth reporting
psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d "$APP_DB" <<SQL
SET client_min_messages = warning;
CREATE TABLE IF NOT EXISTS orders (id bigserial PRIMARY KEY, customer text NOT NULL, total numeric(12,2) NOT NULL);
INSERT INTO orders (customer, total) SELECT 'c' || g, g FROM generate_series(1, 1000) g
  WHERE NOT EXISTS (SELECT 1 FROM orders);
SQL

# 4. standby: pg_basebackup from the primary through a physical slot, then run as a hot standby
if running "$STANDBY"; then
  log "standby already running"
elif exists "$STANDBY"; then
  log "starting stopped standby"
  docker start "$STANDBY" >/dev/null
else
  psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d postgres \
    -c "SELECT pg_drop_replication_slot('${SLOT}') FROM pg_replication_slots WHERE slot_name = '${SLOT}' AND NOT active" >/dev/null
  log "starting standby on 127.0.0.1:${STANDBY_PORT} (pg_basebackup from ${PRIMARY})"
  docker run -d --name "$STANDBY" --network "$NET" -p "127.0.0.1:${STANDBY_PORT}:5432" \
    --user postgres -e PGDATA=/var/lib/postgresql/data/pgdata -e PGPASSWORD="$REPLICATOR_PASSWORD" \
    --entrypoint sh "$IMAGE" -c "
      set -e
      pg_basebackup -h ${PRIMARY} -U ${REPLICATOR} -D \"\$PGDATA\" -X stream -R -C -S ${SLOT} --checkpoint=fast
      exec postgres -c hot_standby=on -c listen_addresses='*' -c max_prepared_transactions=10
    " >/dev/null
fi
wait_ready "$STANDBY" 120

# 5. scenarios: every scenarios/*.sql, as the superuser on the primary (must be idempotent)
for f in "${HERE}"/scenarios/*.sql; do
  [ -e "$f" ] || continue
  log "applying $(basename "$f")"
  psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d postgres < "$f"
done

# 6. the standby is streaming
end=$((SECONDS + 60))
until [[ "$(psql_in "$PRIMARY" "$SUPERUSER" "$SUPERUSER_PASSWORD" -d postgres -At \
  -c "SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming'")" == "1" ]]; do
  (( SECONDS < end )) || { log "TIMEOUT: standby is not streaming"; exit 1; }
  sleep 1
done
log "primary 127.0.0.1:${PRIMARY_PORT}, standby 127.0.0.1:${STANDBY_PORT} (streaming)"
log "roles: ${MONITOR} (pg_monitor), ${NOPERM} (login only); database: ${APP_DB}"
log "next: source ${HERE}/env.sh && rw_pg_use ${MONITOR}"
