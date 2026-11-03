from k8s_namespace_resources.common import cap
from k8s_namespace_resources.fetch import (
    check_settings,
    list_items,
    list_optional,
    load_namespace,
)
from k8s_namespace_resources.k8s import RwError, stderr_of
from k8s_namespace_resources.usage import usage_entries


def main(ctx):
    check_settings(ctx, "max_items", "utilization_percent")
    ns = load_namespace(ctx, "resource-usage")
    # metrics.k8s.io is the point of this task: when it is missing or forbidden there is nothing to report.
    try:
        metrics = list_items(ctx, ns, "pods.metrics.k8s.io")
    except RwError as exc:
        if exc.code == "unreachable" and "unable to handle the request" in stderr_of(exc):
            # The API is registered but its backend (metrics-server) is not serving.
            raise RwError(
                "dependency", f"metrics.k8s.io is not serving: {exc.message}"[:1024]
            ) from None
        raise
    # Requests and limits come from the pods; without them the usage is still reported.
    pods = list_optional(ctx, ns, "pods", "pods")
    entries, counts = usage_entries(metrics, pods, ctx.settings.utilization_percent)
    return {
        "usage": cap(entries, ctx.settings.max_items),
        "inspected": {**counts, "entries": len(entries)},
    }
