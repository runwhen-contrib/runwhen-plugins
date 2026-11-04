from azure_vm_health.common import read_settings
from azure_vm_health.oscheck import run
from azure_vm_health.target import read_thresholds


def main(ctx, check):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"osCheck": run(ctx, check, th, st.max_items)}
