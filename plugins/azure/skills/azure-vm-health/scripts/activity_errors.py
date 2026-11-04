from azure_vm_health.activity import read
from azure_vm_health.common import read_settings


def main(ctx, window="24h"):
    st = read_settings(ctx.settings)
    return {"activity": read(ctx, window, st.max_items)}
