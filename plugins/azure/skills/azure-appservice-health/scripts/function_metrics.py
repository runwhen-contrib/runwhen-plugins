from azure_appservice_health.common import read_settings
from azure_appservice_health.usage import FUNCTION, read_thresholds, web_metrics


def main(ctx, window="24h"):
    st, th = read_settings(ctx.settings), read_thresholds(ctx.settings)
    return {"functions": web_metrics(ctx, window, st, th, FUNCTION, functions_only=True)}
