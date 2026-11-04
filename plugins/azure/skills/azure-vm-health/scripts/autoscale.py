from azure_vm_health.common import read_settings
from azure_vm_health.scale_set import read_autoscale


def main(ctx):
    st = read_settings(ctx.settings)
    return {"autoscale": read_autoscale(ctx, st.max_items)}
