from azure_appservice_health.common import read_settings
from azure_appservice_health.health import read
from azure_appservice_health.target import PLAN, WEB_APP, target


def main(ctx):
    read_settings(ctx.settings)
    return {"health": read(ctx, target(ctx.resource, (WEB_APP, PLAN)))}
