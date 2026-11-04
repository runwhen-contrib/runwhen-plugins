from azure_kv_health.audit import read
from azure_kv_health.common import read_settings


def main(ctx, window="24h"):
    st = read_settings(ctx.settings)
    return {"audit": read(ctx, window, st.max_items)}
