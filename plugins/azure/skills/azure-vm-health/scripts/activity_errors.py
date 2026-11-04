from azure_vm_health.activity import read
from azure_vm_health.common import number, read_settings


def main(ctx, window="24h"):
    st = read_settings(ctx.settings)
    scan_limit = number(
        ctx.settings, "activityScanLimit", "activity_scan_limit", 1, 5000, integer=True
    )
    return {"activity": read(ctx, window, st.max_items, scan_limit)}
