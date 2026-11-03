from k8s_certmanager_health.cert import certificate_requests, orders_for
from k8s_certmanager_health.fetch import load_certificate
from k8s_certmanager_health.k8s import kubectl_json, kubectl_json_scoped

NOT_APPLICABLE = "the cluster has no acme.cert-manager.io Orders (the ACME CRDs are not installed)"


def main(ctx):
    ns = ctx.resource.namespace
    cert = load_certificate(ctx)
    items = kubectl_json(ctx, "-n", ns, "get", "certificaterequests.cert-manager.io")["items"]
    rows, uids = certificate_requests(items, cert, ctx.settings.max_items)

    # Orders exist only where the ACME CRDs are installed: an absent resource type is a fact
    # (applicable: false), a forbidden list is unknown (null) with an error.
    doc, state = kubectl_json_scoped(
        ctx, "orders", "-n", ns, "get", "orders.acme.cert-manager.io", absent_ok=True
    )
    if state == "absent":
        orders = {"applicable": False, "reason": NOT_APPLICABLE, "items": []}
    elif state == "denied":
        orders = None
    else:
        orders = {
            "applicable": True,
            "reason": None,
            "items": orders_for(doc["items"], uids, ctx.settings.max_items),
        }
    return {"requests": rows, "orders": orders}
