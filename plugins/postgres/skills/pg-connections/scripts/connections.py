from pg_connections.connections import collect
from pg_connections.settings import read_settings


def main(ctx):
    st = read_settings(ctx.settings)
    return {"connections": collect(ctx, st.max_items)}
