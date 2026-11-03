from k8s_argocd_health.app import (
    application_view,
    cap,
    conditions_view,
    health_view,
    nested,
    operation_view,
    source_views,
    sync_view,
)
from k8s_argocd_health.fetch import load_application


def main(ctx):
    app = load_application(ctx)
    return {
        "application": application_view(app),
        "sync": sync_view(app),
        "health": health_view(app),
        "conditions": conditions_view(app, ctx.settings.max_items),
        "operation": operation_view(app),
        "sources": cap(nested(source_views(app)), ctx.settings.max_items),
    }
