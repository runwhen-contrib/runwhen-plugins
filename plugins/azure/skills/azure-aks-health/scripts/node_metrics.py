from azure_aks_health.cluster import read_thresholds
from azure_aks_health.common import read_settings
from azure_aks_health.node_metrics import read


def main(ctx, window="24h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"metrics": read(ctx, window, st, th)}
