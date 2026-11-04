from azure_vm_health.common import read_settings
from azure_vm_health.monitor import read
from azure_vm_health.target import read_thresholds


def main(ctx, window="6h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"metrics": read(ctx, window, st, th)}
