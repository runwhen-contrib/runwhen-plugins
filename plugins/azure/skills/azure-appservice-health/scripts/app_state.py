from azure_appservice_health.common import read_settings
from azure_appservice_health.site import app_state, read_site
from azure_appservice_health.target import WEB_APP, target


def main(ctx):
    read_settings(ctx.settings)
    return {"appState": app_state(read_site(ctx, target(ctx.resource, (WEB_APP,))))}
