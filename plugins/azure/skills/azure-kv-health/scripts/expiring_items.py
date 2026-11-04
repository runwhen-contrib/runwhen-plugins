from azure_kv_health.common import read_settings
from azure_kv_health.expiry import read


def main(ctx, threshold_days=31):
    st = read_settings(ctx.settings)
    return {"expiry": read(ctx, threshold_days, st.max_items)}
