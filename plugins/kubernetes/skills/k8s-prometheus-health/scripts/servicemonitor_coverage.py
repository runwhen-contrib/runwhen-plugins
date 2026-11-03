import json

from k8s_prometheus_health import coverage, targets
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.fetch import load_prometheus, prometheus_pods, query_pods
from k8s_prometheus_health.k8s import kubectl_json, kubectl_json_scoped, run_checked

REVIEWS_FILE = "sar-0.json"


def list_servicemonitors(ctx, prom):
    """(ServiceMonitors, namespace labels or None, complete). A cluster-wide list is needed unless the
    CR watches only its own namespace; when it is forbidden the CR's own namespace is listed and
    the result says it is not complete."""
    ns = prom["metadata"]["namespace"]
    scope = coverage.selector_scope(prom)
    complete = True
    if scope == "own":
        return kubectl_json(ctx, "-n", ns, "get", coverage.SERVICEMONITORS)["items"], None, True
    doc = kubectl_json_scoped(
        ctx,
        "servicemonitors",
        "get",
        coverage.SERVICEMONITORS,
        "-A",
        hint="grant list on servicemonitors.monitoring.coreos.com cluster-wide (a ClusterRole)",
    )
    if doc is None:
        complete = False
        doc = kubectl_json(ctx, "-n", ns, "get", coverage.SERVICEMONITORS)
    labels = None
    if scope == "selector":
        spaces = kubectl_json_scoped(
            ctx,
            "namespaces",
            "get",
            "namespaces",
            hint="grant list on namespaces (a ClusterRole): the Prometheus selects namespaces by label",
        )
        if spaces is None:
            complete = False
        else:
            labels = {
                n["metadata"]["name"]: n["metadata"].get("labels") or {} for n in spaces["items"]
            }
    return doc["items"], labels, complete


def review_access(ctx, prom, sms, selections):
    """The ServiceAccount access view, or None when creating SubjectAccessReviews is forbidden
    (recorded with ctx.error)."""
    spaces, notes = coverage.target_namespaces(prom, sms, selections)
    if not spaces:
        return coverage.access_view(prom, spaces, notes, [], [])
    document, order = coverage.review_list(prom, spaces)
    with open(ctx.workdir / REVIEWS_FILE, "w") as handle:
        json.dump(document, handle)
    try:
        result = run_checked(ctx, ["kubectl", "create", "-f", REVIEWS_FILE, "-o", "json"])
    except RwError as exc:
        if exc.code != "permission_denied":
            raise
        ctx.error(
            "permission_denied",
            f"not allowed to create SubjectAccessReviews: {exc.message}"[:1024],
            scope="subjectaccessreviews",
            hint="grant create on subjectaccessreviews.authorization.k8s.io (a ClusterRole)",
        )
        return None
    allowed = coverage.parse_reviews(result.stdout, len(order))
    return coverage.access_view(prom, spaces, notes, order, allowed)


def main(ctx):
    prom = load_prometheus(ctx)
    pods = prometheus_pods(ctx, prom)
    answers = query_pods(ctx, prom, pods, targets.PATH, "targets", targets.parse_reply)
    merged = targets.merge(answers, ctx.now(), ctx.settings.overdue_intervals)
    sms, ns_labels, complete = list_servicemonitors(ctx, prom)
    result = coverage.view(prom, sms, ns_labels, merged, ctx.settings.max_items, complete)
    selections = {
        (sm["metadata"]["namespace"], sm["metadata"]["name"]): coverage.selection(
            prom, sm, ns_labels
        )[0]
        is True
        for sm in sms
    }
    result["queriedPods"] = [pod for pod, _ in answers]
    return {
        "coverage": result,
        "serviceAccountAccess": review_access(ctx, prom, sms, selections),
    }
