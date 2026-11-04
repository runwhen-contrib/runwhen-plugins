from azure_appservice_health.common import read_settings
from azure_appservice_health.usage import plan_metrics, read_thresholds


def main(ctx, window="24h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"planMetrics": plan_metrics(ctx, window, st, th)}
