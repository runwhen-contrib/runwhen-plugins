from k8s_namespace_resources.common import cap
from k8s_namespace_resources.fetch import check_settings, list_items, load_namespace
from k8s_namespace_resources.images import image_entries


def main(ctx):
    check_settings(ctx, "max_items")
    ns = load_namespace(ctx, "images")
    pods = list_items(ctx, ns, "pods")
    entries, failures = image_entries(pods)
    return {
        "containers": cap(entries, ctx.settings.max_items),
        "pullFailures": cap(failures, ctx.settings.max_items),
        "inspected": {"pods": len(pods), "containers": len(entries), "pullFailures": len(failures)},
    }
