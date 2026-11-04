from azure_vm_health.common import read_settings
from azure_vm_health.scale_set import read_instances


def main(ctx):
    st = read_settings(ctx.settings)
    return {"instances": read_instances(ctx, st.max_items)}
