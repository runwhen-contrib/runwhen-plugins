from azure_kv_health.common import read_settings
from azure_kv_health.configuration import read


def main(ctx):
    read_settings(ctx.settings)
    return {"configuration": read(ctx)}
