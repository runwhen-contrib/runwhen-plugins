from pg_locks.blocking import BLOCKING_SQL, VISIBILITY_MESSAGE, build_blocking
from pg_locks.pg import psql_json
from pg_locks.settings import read_settings


def main(ctx):
    st = read_settings(ctx.settings)
    blocking = build_blocking(psql_json(ctx, BLOCKING_SQL), st.max_items)
    if blocking["visibility"] == "partial":
        ctx.error("permission_denied", VISIBILITY_MESSAGE, scope="pg_stat_activity")
    return {"blocking": blocking}
