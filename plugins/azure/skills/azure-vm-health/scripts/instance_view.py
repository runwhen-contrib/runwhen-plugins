from azure_vm_health.common import read_settings
from azure_vm_health.instance_view import read


def main(ctx):
    st = read_settings(ctx.settings)
    return {"instanceView": read(ctx, st.max_items)}
