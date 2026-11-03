from k8s_certmanager_health.cert import certificate_requests, challenges_for, orders_for
from k8s_certmanager_health.fetch import load_certificate
from k8s_certmanager_health.k8s import kubectl_json, kubectl_json_scoped

NOT_APPLICABLE = "the cluster has no acme.cert-manager.io {} (the ACME CRDs are not installed)"


def not_applicable(kind):
    return {"applicable": False, "reason": NOT_APPLICABLE.format(kind), "items": []}


def main(ctx):
    ns = ctx.resource.namespace
    cert = load_certificate(ctx)
    items = kubectl_json(ctx, "-n", ns, "get", "certificaterequests.cert-manager.io")["items"]
    rows, owners = certificate_requests(items, cert, ctx.settings.max_items)

    # Orders and Challenges exist only where the ACME CRDs are installed: an absent resource type
    # is a fact (applicable: false), a forbidden list is unknown (null) with an error.
    doc, state = kubectl_json_scoped(
        ctx, "orders", "-n", ns, "get", "orders.acme.cert-manager.io", absent_ok=True
    )
    order_uids = {}
    if state == "absent":
        orders = not_applicable("Orders")
    elif state == "denied":
        orders = None
    else:
        order_rows, order_uids = orders_for(doc["items"], owners, ctx.settings.max_items)
        orders = {"applicable": True, "reason": None, "items": order_rows}

    # A Challenge is matched to its Order by ownerReference, so without the Orders there is
    # nothing to match: the list is not read and challenges is null.
    if orders is None:
        challenges = None
    else:
        doc, state = kubectl_json_scoped(
            ctx, "challenges", "-n", ns, "get", "challenges.acme.cert-manager.io", absent_ok=True
        )
        if state == "absent":
            challenges = not_applicable("Challenges")
        elif state == "denied":
            challenges = None
        else:
            challenge_rows = challenges_for(doc["items"], order_uids, ctx.settings.max_items)
            challenges = {"applicable": True, "reason": None, "items": challenge_rows}
    return {"requests": rows, "orders": orders, "challenges": challenges}
