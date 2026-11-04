from azure_kv_health.availability import read
from azure_kv_health.common import read_settings
from azure_kv_health.vault import read_thresholds


def main(ctx, window="24h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"availability": read(ctx, window, st, th)}
