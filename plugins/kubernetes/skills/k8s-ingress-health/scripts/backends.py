from k8s_ingress_health.fetch import fetch_endpoint_slices, fetch_services, load_ingress
from k8s_ingress_health.ingress import (
    backend_entries,
    backend_refs,
    gke_backends,
    gke_load_balancer,
    service_names,
    tls_view,
    wrap,
)


def main(ctx):
    ingress = load_ingress(ctx)
    max_items = ctx.settings.max_items
    services, services_truncated = fetch_services(ctx, service_names(backend_refs(ingress)))
    slices = fetch_endpoint_slices(ctx, services)
    gke, problem = gke_backends(ingress, max_items)
    if problem:
        ctx.error("internal", problem, scope="annotations")
    return {
        "backends": wrap(
            backend_entries(ingress, services, slices),
            max_items,
            servicesTruncated=services_truncated,
        ),
        "tls": tls_view(ingress, max_items),
        "gkeBackends": gke,
        "gkeLoadBalancer": gke_load_balancer(ingress),
    }
