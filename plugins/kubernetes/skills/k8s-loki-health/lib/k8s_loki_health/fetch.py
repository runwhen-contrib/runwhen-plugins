"""Fetching the target workload and talking to its pods (the only I/O besides k8s.py)."""

import re

from k8s_loki_health.common import MESSAGE_LIMIT
from k8s_loki_health.errors import RwError
from k8s_loki_health.k8s import kubectl_json
from k8s_loki_health.proxy import pod_path, proxy_get

KINDS = {
    "kubernetes/statefulset": "statefulsets.apps",
    "kubernetes/deployment": "deployments.apps",
}
PLURAL_TYPES = {"statefulsets": "kubernetes/statefulset", "deployments": "kubernetes/deployment"}
DEFAULT_PORT = 3100
PORT_NAMES = ("http-metrics", "http", "http-metric")
SCHEMES = ("http", "https")
# More pods than this are listed but not queried.
MAX_QUERIED_PODS = 10
_PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return PLURAL_TYPES.get(segments[-2]) if len(segments) >= 2 else None


def check_inputs(scheme, port, paths=()):
    """Bad input is `invalid_input`, before any kubectl call (a proxy path is never built from
    anything but a plain absolute path)."""
    if scheme not in SCHEMES:
        raise RwError("invalid_input", f"scheme must be one of {SCHEMES}, got {scheme!r}")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise RwError("invalid_input", f"port must be an integer from 1 to 65535, got {port!r}")
    for path in paths:
        if not isinstance(path, str) or not _PATH.match(path) or ".." in path:
            raise RwError(
                "invalid_input", f"ringPaths entries must be plain absolute paths: {path!r}"
            )


def load_workload(ctx):
    """The target StatefulSet or Deployment. A missing one raises (NotFound from kubectl)."""
    kind = KINDS.get(target_type(ctx))
    if kind is None:
        raise RwError("invalid_input", f"k8s-loki-health does not apply to {target_type(ctx)!r}")
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", kind, ctx.resource.name)


def selector_string(selector):
    """The `kubectl -l` form of a metav1.LabelSelector (matchLabels and matchExpressions)."""
    terms = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), ",".join(expr.get("values") or [])
        if op == "In":
            terms.append(f"{key} in ({values})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({values})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            raise RwError("invalid_input", f"label selector operator {op!r} is not supported")
    if not terms:
        raise RwError("invalid_input", "the workload's selector is empty: no pods can be named")
    return ",".join(terms)


def workload_pods(ctx, workload):
    """The workload's pods (its own selector), sorted by name; empty is a fact for the caller."""
    selector = selector_string((workload.get("spec") or {}).get("selector") or {})
    doc = kubectl_json(ctx, "-n", workload["metadata"]["namespace"], "get", "pods", "-l", selector)
    return sorted(doc["items"], key=lambda p: p["metadata"]["name"])


def pod_port(pod, port=None):
    """`port` if given, else the container port named http-metrics (the chart's), http or
    http-metric (the `loki` container first), else 3100."""
    if port is not None:
        return port
    containers = (pod.get("spec") or {}).get("containers") or []
    for container in sorted(containers, key=lambda c: c.get("name") != "loki"):
        for entry in container.get("ports") or []:
            if entry.get("name") in PORT_NAMES:
                return entry.get("containerPort", DEFAULT_PORT)
    return DEFAULT_PORT


def running(pod):
    """Whether the pod can be proxied to: Running and not being deleted."""
    return (pod.get("status") or {}).get("phase") == "Running" and not (
        pod["metadata"].get("deletionTimestamp")
    )


def pod_condition(pod, kind):
    for cond in (pod.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status") == "True"
    return None


def fetch_one(ctx, workload, pod, scheme, port, path):
    """GET `path` from one pod: a Reply (200, 404 or 503) or RwError."""
    return proxy_get(
        ctx,
        pod_path(
            workload["metadata"]["namespace"],
            pod["metadata"]["name"],
            scheme,
            pod_port(pod, port),
            path,
        ),
    )


def queried(pods):
    """The pods that are asked: Running ones, the first MAX_QUERIED_PODS."""
    return [p for p in pods if running(p)][:MAX_QUERIED_PODS]


def no_pods(workload):
    return RwError(
        "unreachable",
        f"no running pod for {workload['kind']} {workload['metadata']['name']}: nothing to query",
    )


def short(exc):
    return exc.message[:MESSAGE_LIMIT]
