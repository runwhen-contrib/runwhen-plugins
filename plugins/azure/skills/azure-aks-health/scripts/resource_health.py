from azure_aks_health.resource_health import read


def main(ctx):
    return {"health": read(ctx)}
