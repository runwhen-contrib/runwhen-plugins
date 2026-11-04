from azure_aks_health.activity import read
from azure_aks_health.cluster import read_scan_limit
from azure_aks_health.common import read_settings


def main(ctx, window="24h"):
    st = read_settings(ctx.settings)
    return {"activity": read(ctx, window, st.max_items, read_scan_limit(ctx.settings))}
