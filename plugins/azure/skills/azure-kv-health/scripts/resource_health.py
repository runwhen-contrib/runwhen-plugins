from azure_kv_health.resource_health import read


def main(ctx):
    return {"health": read(ctx)}
