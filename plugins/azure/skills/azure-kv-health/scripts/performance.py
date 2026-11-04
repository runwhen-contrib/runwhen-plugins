from azure_kv_health.common import read_settings
from azure_kv_health.performance import read


def main(ctx, window="24h"):
    return {"performance": read(ctx, window, read_settings(ctx.settings))}
