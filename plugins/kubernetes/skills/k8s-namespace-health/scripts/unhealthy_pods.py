from k8s_namespace_health.common import cap
from k8s_namespace_health.fetch import list_items, list_secondary, load_namespace
from k8s_namespace_health.pods import pod_counts, unhealthy_pods


def main(ctx):
    namespace = load_namespace(ctx)
    pods = list_items(ctx, namespace, "pods")
    replicasets = list_secondary(ctx, namespace, "replicasets.apps")
    jobs = list_secondary(ctx, namespace, "jobs.batch")
    unhealthy = unhealthy_pods(pods, replicasets, jobs, ctx.settings.restart_threshold)
    return {
        "pods": cap(unhealthy, ctx.settings.max_items),
        "counts": pod_counts(pods, len(unhealthy)),
    }
