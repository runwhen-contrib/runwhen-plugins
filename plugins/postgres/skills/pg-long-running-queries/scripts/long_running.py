from pg_long_running_queries.long_running import collect
from pg_long_running_queries.settings import read_settings


def main(ctx, min_duration="5m"):
    st = read_settings(ctx.settings)
    return {"longRunning": collect(ctx, min_duration, st.max_items, st.include_query_text)}
