from k8s_namespace_health.fetch import list_items, load_namespace
from k8s_namespace_health.quotas import quota_usage


def main(ctx):
    namespace = load_namespace(ctx)
    quotas = list_items(ctx, namespace, "resourcequotas")
    return {"quotas": quota_usage(quotas, ctx.settings.quota_ratio, ctx.settings.max_items)}
