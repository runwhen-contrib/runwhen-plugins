"""exporter-queue: the collector pods' own metrics as exporter queue and drop-counter facts."""

from k8s_otelcollector_health.collector import merge, pod_entry, view
from k8s_otelcollector_health.common import cap
from k8s_otelcollector_health.errors import RwError
from k8s_otelcollector_health.k8s import MESSAGE_LIMIT, kubectl_json, proxy_get
from k8s_otelcollector_health.prom import parse

# ctx.resource.type -> kubectl resource
TARGETS = {
    "kubernetes/deployment": "deployments",
    "kubernetes/statefulset": "statefulsets",
    "kubernetes/daemonset": "daemonsets",
}
PLURALS = {plural: plural for plural in TARGETS.values()}

METRICS_HINT = (
    "the collector exposes its own metrics on service.telemetry.metrics (default port 8888, "
    "bound to localhost only by recent versions); set metricsPort or bind it to the pod IP"
)


def target_resource(ctx):
    """The kubectl resource of the target from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path`. Needs no cluster call. Anything else raises RwError('invalid_input')."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype in TARGETS:
        return TARGETS[rtype]
    if not rtype:
        parts = str(getattr(ctx.resource, "path", None) or "").strip("/").split("/")
        if len(parts) >= 2 and parts[-2] in PLURALS:
            return parts[-2]
    raise RwError(
        "invalid_input",
        f"k8s-otelcollector-health does not apply to {rtype or getattr(ctx.resource, 'path', None)!r}",
    )


def selector_string(selector):
    """Label selector (matchLabels + matchExpressions) as a kubectl -l string. An empty selector would
    select every pod of the namespace, so it raises."""
    selector = selector or {}
    terms = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], ",".join(expr.get("values") or [])
        terms.append(
            {
                "In": f"{key} in ({values})",
                "NotIn": f"{key} notin ({values})",
                "Exists": key,
                "DoesNotExist": f"!{key}",
            }.get(op)
            or _unknown(op)
        )
    if not terms:
        raise RwError(
            "invalid_input", "workload has an empty selector; refusing to select every pod"
        )
    return ",".join(terms)


def _unknown(op):
    raise RwError("invalid_input", f"unknown selector operator {op!r}")


def queryable(pod):
    status = pod.get("status") or {}
    return (
        status.get("phase") == "Running"
        and bool(status.get("podIP"))
        and not (pod.get("metadata") or {}).get("deletionTimestamp")
    )


def not_queried_reason(pod):
    status = pod.get("status") or {}
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return "pod is being deleted"
    if status.get("phase") == "Running":
        return "pod has no IP yet"
    return f"pod is {status.get('phase') or 'in an unknown phase'}"


def run(ctx):
    plural = target_resource(ctx)
    ns = ctx.resource.namespace
    workload = kubectl_json(ctx, "-n", ns, "get", plural, ctx.resource.name)
    selector = selector_string((workload.get("spec") or {}).get("selector"))
    pods = sorted(
        kubectl_json(ctx, "-n", ns, "get", "pods", "-l", selector)["items"],
        key=lambda p: p["metadata"]["name"],
    )
    if not pods:
        raise RwError(
            "unreachable",
            f"{plural[:-1]} {ctx.resource.name!r} has no pods, so there is no collector to ask",
        )
    max_items = ctx.settings.max_items
    running = [p for p in pods if queryable(p)]
    asked = running[: ctx.settings.max_pods]
    entries = [
        pod_entry(p["metadata"]["name"], reason=not_queried_reason(p))
        for p in pods
        if not queryable(p)
    ]
    per_pod, failures = [], []
    for pod in asked:
        name = pod["metadata"]["name"]
        try:
            body = proxy_get(
                ctx, ns, name, ctx.settings.metrics_port, "metrics", ctx.settings.scheme
            )
            queues, counters, facts = view(
                parse(body), name, ctx.settings.queue_utilization_percent
            )
        except RwError as exc:
            if exc.code == "invalid_input":
                raise
            failures.append((name, exc))
            entries.append(pod_entry(name, reason=f"{exc.code}: {exc.message}"[:MESSAGE_LIMIT]))
            continue
        per_pod.append((queues, counters))
        entries.append(pod_entry(name, facts))
    if not per_pod:
        # nothing was read: a result with no queue in it would look like a collector with none
        if failures:
            name, exc = failures[0]  # the same hint as the partial path gives
            raise RwError(
                exc.code,
                f"{name}: {exc.message}"[:MESSAGE_LIMIT],
                scope=f"pod/{name}",
                hint=getattr(exc, "hint", None) or METRICS_HINT,
            )
        raise RwError(
            "unreachable",
            "no Running pod to ask: "
            + ", ".join(f"{e['pod']} ({e['notQueriedReason']})" for e in entries)[:MESSAGE_LIMIT],
        )
    for name, exc in failures:
        ctx.error(
            exc.code,
            f"{name}: {exc.message}"[:MESSAGE_LIMIT],
            scope=f"pod/{name}",
            hint=getattr(exc, "hint", None) or METRICS_HINT,
        )
    queues, counters = merge(per_pod)
    entries.sort(key=lambda e: e["pod"])
    return {
        "pods": {
            "count": len(pods),
            "queriedCount": len(per_pod),
            "truncated": len(running) > len(asked),
            "items": cap(entries, max_items),
        },
        "queues": {
            "thresholdPercent": ctx.settings.queue_utilization_percent,
            "count": len(queues),
            "noQueuedExporters": not queues,
            "truncated": len(queues) > max_items,
            "items": cap(queues, max_items),
        },
        "counters": {
            "count": len(counters),
            "nonZeroCount": sum(1 for c in counters if c["value"]),
            "truncated": len(counters) > max_items,
            "items": cap(counters, max_items),
        },
    }
