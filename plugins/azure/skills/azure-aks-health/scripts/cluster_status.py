from azure_aks_health.cluster_status import read
from azure_aks_health.common import read_settings


def main(ctx):
    read_settings(ctx.settings)
    return {"cluster": read(ctx)}
