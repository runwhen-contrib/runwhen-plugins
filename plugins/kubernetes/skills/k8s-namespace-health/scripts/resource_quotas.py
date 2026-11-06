from k8s_namespace_health.fetch import check_settings, list_items, load_namespace
from k8s_namespace_health.quotas import quota_usage


def main(ctx):
    check_settings(ctx, "max_items", "quota_ratio")
    namespace = load_namespace(ctx)
    quotas = list_items(ctx, namespace, "resourcequotas")
    return {"quotas": quota_usage(quotas, ctx.settings.quota_ratio, ctx.settings.max_items)}
