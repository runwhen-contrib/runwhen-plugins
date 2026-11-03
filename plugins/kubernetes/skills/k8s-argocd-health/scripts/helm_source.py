from k8s_argocd_health.app import helm_view, last_deployed
from k8s_argocd_health.fetch import load_application


def main(ctx):
    app = load_application(ctx)
    return {"helm": helm_view(app), "lastDeployed": last_deployed(app)}
