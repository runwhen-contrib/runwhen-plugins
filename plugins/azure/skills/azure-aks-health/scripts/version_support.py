from azure_aks_health.common import read_settings
from azure_aks_health.version_support import read


def main(ctx):
    read_settings(ctx.settings)
    return {"version": read(ctx)}
