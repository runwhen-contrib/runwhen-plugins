from k8s_certmanager_health.cert import certificate_view, issuer_lookup, issuer_view
from k8s_certmanager_health.clock import reference_time
from k8s_certmanager_health.fetch import load_certificate
from k8s_certmanager_health.k8s import kubectl_json_scoped


def main(ctx):
    cert = load_certificate(ctx)
    view = certificate_view(cert, reference_time(ctx), ctx.settings.expiry_warning_days)

    # The issuer is read apart from the Certificate: a ClusterIssuer is cluster-scoped, so a
    # namespace-scoped identity is forbidden here and still gets the Certificate facts.
    plural, name = issuer_lookup(cert)
    obj, state = None, "external"
    if plural == "issuers":
        obj, state = kubectl_json_scoped(
            ctx,
            "issuers",
            "-n",
            ctx.resource.namespace,
            "get",
            "issuers.cert-manager.io",
            name,
            missing_ok=True,
        )
    elif plural == "clusterissuers":
        obj, state = kubectl_json_scoped(
            ctx, "clusterissuers", "get", "clusterissuers.cert-manager.io", name, missing_ok=True
        )
    return {"certificate": view, "issuer": issuer_view(cert, obj, state)}
