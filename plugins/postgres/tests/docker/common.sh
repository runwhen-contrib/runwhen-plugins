# Shared by up.sh, down.sh and env.sh. Local docker only: every container publishes on
# 127.0.0.1 and nothing here talks to a shared cluster. The passwords are throwaway test
# values for these containers; they protect nothing.
IMAGE="postgres:16.15-alpine"        # pinned tag; bump deliberately
NET="rw-pg-net"
PRIMARY="rw-pg-primary"
STANDBY="rw-pg-standby"
PRIMARY_PORT=55432
STANDBY_PORT=55433
SLOT="rw_standby"

SUPERUSER="postgres"
SUPERUSER_PASSWORD="rw-test-superuser"
REPLICATOR="rw_replicator"
REPLICATOR_PASSWORD="rw-test-replicator"
MONITOR="rw_monitor"                 # LOGIN, member of pg_monitor, CONNECT on orders
MONITOR_PASSWORD="rw-test-monitor"
NOPERM="rw_noperm"                   # LOGIN only: no pg_monitor, no CONNECT on orders
NOPERM_PASSWORD="rw-test-noperm"
APP_DB="orders"

log() { printf '[%s] %s\n' "$(basename "$0")" "$*"; }
