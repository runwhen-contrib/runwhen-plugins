from k8s_ingress_health.fetch import fetch_services, load_ingress
from k8s_ingress_health.ingress import (
    backend_refs,
    class_source,
    class_view,
    ingress_view,
    service_addresses,
    service_names,
)
from k8s_ingress_health.k8s import kubectl_json_scoped


def main(ctx):
    ingress = load_ingress(ctx)
    view = ingress_view(ingress)

    # Cluster-scoped reads: a namespace-scoped identity is forbidden here and
    # still gets the Ingress's address and its Services. A named class is read
    # by name; with none named the IngressClasses are listed for the default.
    source, name = class_source(ingress)
    ic, ic_state, classes = None, "ok", None
    if source == "spec":
        ic, ic_state = kubectl_json_scoped(
            ctx, "ingressclasses", "get", "ingressclasses.networking.k8s.io", name, missing_ok=True
        )
    elif source == "none":
        doc, state = kubectl_json_scoped(
            ctx, "ingressclasses", "get", "ingressclasses.networking.k8s.io"
        )
        classes = None if state == "denied" else doc["items"]

    services, services_truncated = fetch_services(ctx, service_names(backend_refs(ingress)))
    return {
        "ingress": view,
        "ingressClass": class_view(source, name, ic, ic_state, classes),
        "serviceAddresses": service_addresses(
            ingress, services, ctx.settings.max_items, servicesTruncated=services_truncated
        ),
    }
