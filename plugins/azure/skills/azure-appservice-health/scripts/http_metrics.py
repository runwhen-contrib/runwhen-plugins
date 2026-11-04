from azure_appservice_health.common import read_settings
from azure_appservice_health.usage import HTTP, read_thresholds, web_metrics


def main(ctx, window="24h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"http": web_metrics(ctx, window, st, th, HTTP)}
