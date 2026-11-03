from k8s_flux_health.fetch import load_dependencies, load_object, target_kind
from k8s_flux_health.flux import cap, revisions_view, state_view


def main(ctx):
    kind = target_kind(ctx)
    obj = load_object(ctx, kind)
    dependencies, total = load_dependencies(ctx, kind, obj)
    return {
        "state": state_view(kind, obj),
        "revisions": revisions_view(kind, obj),
        "dependencies": {
            "count": total,
            "truncated": total > len(dependencies),
            "items": cap(dependencies, ctx.settings.max_items),
        },
    }
