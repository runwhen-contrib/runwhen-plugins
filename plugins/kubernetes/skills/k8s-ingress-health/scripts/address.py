from k8s_ingress_health.fetch import fetch_services, load_ingress
from k8s_ingress_health.ingress import (
    backend_refs,
    class_view,
    ingress_view,
    service_addresses,
    service_names,
)
from k8s_ingress_health.k8s import kubectl_json_scoped


def main(ctx):
    ingress = load_ingress(ctx)
    view = ingress_view(ingress)

    # Cluster-scoped read: a namespace-scoped identity is forbidden here and
    # still gets the Ingress's address and its Services.
    ic, ic_state = None, "ok"
    if view["ingressClassName"]:
        ic, ic_state = kubectl_json_scoped(
            ctx,
            "ingressclasses",
            "get",
            "ingressclasses.networking.k8s.io",
            view["ingressClassName"],
            missing_ok=True,
        )

    services = fetch_services(ctx, service_names(backend_refs(ingress)))
    return {
        "ingress": view,
        "ingressClass": class_view(view["ingressClassName"], ic, ic_state != "denied"),
        "serviceAddresses": service_addresses(ingress, services, ctx.settings.max_items),
    }
