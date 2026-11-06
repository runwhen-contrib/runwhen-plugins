from k8s_argocd_health.app import unhealthy_resources
from k8s_argocd_health.fetch import load_application


def main(ctx):
    app = load_application(ctx)
    return {"resources": unhealthy_resources(app, ctx.settings.max_items)}
