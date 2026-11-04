from azure_kv_health.availability import read
from azure_kv_health.common import read_settings


def main(ctx, window="24h"):
    return {"availability": read(ctx, window, read_settings(ctx.settings))}
