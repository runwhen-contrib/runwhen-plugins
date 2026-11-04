from azure_appservice_health.common import read_settings
from azure_appservice_health.ops import swap


def main(ctx, source_slot, target_slot="production"):
    read_settings(ctx.settings)
    return {"swap": swap(ctx, source_slot, target_slot, ctx.settings)}
