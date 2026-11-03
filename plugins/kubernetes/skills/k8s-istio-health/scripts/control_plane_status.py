from k8s_istio_health.common import bound, check_int_setting, selector_string, deployment_pods
from k8s_istio_health.control import (
    MUTATING_PREFIXES,
    VALIDATING_PREFIXES,
    deployment_view,
    matching_configurations,
    pod_view,
    ready_endpoints,
    sort_pods,
    versions,
    webhook_views,
)
from k8s_istio_health.fetch import list_scoped, load_deployment
from k8s_istio_health.k8s import kubectl_json_scoped

WEBHOOK_HINT = "grant list on {} through a ClusterRole to see the Istio webhook configurations"


def main(ctx, include_webhooks=True):
    max_items = check_int_setting("maxItems", ctx.settings.max_items, 1, 10000)
    dep = load_deployment(ctx)
    ns, name = ctx.resource.namespace, ctx.resource.name

    # The pods are read apart from the Deployment: without them the replica counts still stand.
    selector = selector_string(dep["spec"]["selector"])
    replicasets = list_scoped(ctx, "replicasets", "replicasets.apps", "-l", selector)
    listed = list_scoped(ctx, "pods", "pods", "-l", selector)
    pods = None if listed is None else deployment_pods(dep, replicasets, listed)

    webhooks, mutating_n, validating_n = None, None, None
    if include_webhooks:
        mutating, validating = (
            kubectl_json_scoped(
                ctx,
                plural,
                "get",
                f"{plural}.admissionregistration.k8s.io",
                hint=WEBHOOK_HINT.format(plural),
            )
            for plural in ("mutatingwebhookconfigurations", "validatingwebhookconfigurations")
        )
        slices = {}

        def endpoints_of(service_ns, service):
            # Only Services in the target's namespace are looked at.
            if service_ns != ns:
                return None
            if service not in slices:
                items = list_scoped(
                    ctx,
                    "endpointslices",
                    "endpointslices.discovery.k8s.io",
                    "-l",
                    f"kubernetes.io/service-name={service}",
                )
                ready, not_ready = (None, None) if items is None else ready_endpoints(items)
                slices[service] = None if items is None else {"ready": ready, "notReady": not_ready}
            return slices[service]

        rows = []
        if mutating is not None:
            configs = matching_configurations(mutating["items"], MUTATING_PREFIXES)
            mutating_n = len(mutating["items"])
            rows += webhook_views(configs, "mutating", ns, name, endpoints_of)
        if validating is not None:
            configs = matching_configurations(validating["items"], VALIDATING_PREFIXES)
            validating_n = len(validating["items"])
            rows += webhook_views(configs, "validating", ns, name, endpoints_of)
        webhooks = None if mutating is None and validating is None else rows

    pod_rows = None if pods is None else sort_pods([pod_view(p) for p in pods])
    pod_rows, pods_total, pods_omitted = (
        bound(pod_rows or [], max_items) if pods is not None else (None, None, None)
    )
    webhook_rows, webhooks_total, webhooks_omitted = (
        bound(webhooks, max_items) if webhooks is not None else (None, None, None)
    )
    return {
        "deployment": deployment_view(dep),
        "version": versions(dep, pods),
        "pods": pod_rows,
        "webhooks": webhook_rows,
        "inspected": {
            "pods": pods_total,
            "mutatingConfigurations": mutating_n,
            "validatingConfigurations": validating_n,
            "total": {"pods": pods_total, "webhooks": webhooks_total},
            "omitted": {"pods": pods_omitted, "webhooks": webhooks_omitted},
        },
    }
