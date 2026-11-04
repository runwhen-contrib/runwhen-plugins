from azure_appservice_health.arm import get
from azure_appservice_health.common import read_settings
from azure_appservice_health.errors import RwError
from azure_appservice_health.site import configuration, read_site
from azure_appservice_health.target import WEB_APP, site_id, target


def main(ctx):
    read_settings(ctx.settings)
    t = target(ctx.resource, (WEB_APP,))
    site = read_site(ctx, t)
    config = None
    try:
        config = get(ctx, site_id(t.subscription, t.group, t.name) + "/config/web")
    except RwError as exc:
        # the site facts stand; the configuration facts are null and say why
        ctx.error(exc.code, exc.message, scope="config")
    return {"configuration": configuration(site, config)}
