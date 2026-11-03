from pg_locks.pg import psql_json
from pg_locks.settings import read_settings
from pg_locks.summary import SUMMARY_SQL, build_summary


def main(ctx):
    st = read_settings(ctx.settings)
    return build_summary(psql_json(ctx, SUMMARY_SQL), st.max_items)
