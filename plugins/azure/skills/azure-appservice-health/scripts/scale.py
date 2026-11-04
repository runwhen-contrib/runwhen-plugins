from azure_appservice_health.common import read_settings
from azure_appservice_health.ops import scale as run


def main(ctx, instances=None, sku=None, override_autoscale=False):
    read_settings(ctx.settings)
    return {"scale": run(ctx, instances, sku, ctx.settings, override_autoscale)}
