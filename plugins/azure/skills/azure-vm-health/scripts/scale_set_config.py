from azure_vm_health.common import read_settings
from azure_vm_health.scale_set import read_config


def main(ctx):
    read_settings(ctx.settings)
    return {"scaleSet": read_config(ctx)}
