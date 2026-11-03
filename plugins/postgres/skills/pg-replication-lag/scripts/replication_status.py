from pg_replication_lag.pg import psql_json
from pg_replication_lag.replication import (
    PRIMARY_PARTIAL,
    PRIMARY_SQL,
    ROLE_SQL,
    STANDBY_PARTIAL,
    STANDBY_SQL,
    build_primary,
    build_standby,
    read_role,
)
from pg_replication_lag.settings import read_settings


def main(ctx):
    st = read_settings(ctx.settings)
    role = read_role(psql_json(ctx, ROLE_SQL))
    if role["inRecovery"]:
        replication = build_standby(role, psql_json(ctx, STANDBY_SQL))
        scope, message = "pg_stat_wal_receiver", STANDBY_PARTIAL
    else:
        replication = build_primary(role, psql_json(ctx, PRIMARY_SQL), st.max_items)
        scope, message = "pg_stat_replication", PRIMARY_PARTIAL
    if replication["visibility"] == "partial":
        ctx.error("permission_denied", message, scope=scope)
    return {"replication": replication}
