from k8s_ingress_health.fetch import fetch_endpoints, fetch_services, load_ingress
from k8s_ingress_health.ingress import (
    backend_entries,
    backend_refs,
    cap,
    gke_backends,
    service_names,
    tls_view,
)


def main(ctx):
    ingress = load_ingress(ctx)
    max_items = ctx.settings.max_items
    services = fetch_services(ctx, service_names(backend_refs(ingress)))
    endpoints = fetch_endpoints(ctx, services)
    gke, problem = gke_backends(ingress, max_items)
    if problem:
        ctx.error("internal", problem[:1024], scope="annotations")
    return {
        "backends": cap(backend_entries(ingress, services, endpoints), max_items),
        "tls": tls_view(ingress, max_items),
        "gkeBackends": gke,
    }
