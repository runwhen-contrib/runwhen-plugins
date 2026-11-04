from azure_appservice_health.common import read_settings
from azure_appservice_health.ops import power


def main(ctx):
    read_settings(ctx.settings)
    return {"power": power(ctx, "start", ctx.settings)}
