"""Fetching the target Prometheus and talking to its pods (the only I/O besides k8s.py)."""

from k8s_prometheus_health.common import MESSAGE_LIMIT
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.k8s import kubectl_json
from k8s_prometheus_health.proxy import api_json, pod_path, proxy_get

TYPE = "kubernetes/prometheus"
PLURAL = "prometheuses"
# Fully qualified so a CRD from another group with a `prometheuses` plural cannot answer.
PROMETHEUSES = "prometheuses.monitoring.coreos.com"
# The labels the operator puts on the pods of a Prometheus CR (also on its StatefulSet).
POD_SELECTOR = "app.kubernetes.io/name=prometheus,prometheus={name}"
DEFAULT_PORT_NAME = "web"
DEFAULT_PORT = 9090
# Replicas times shards: more pods than this are listed but not queried.
MAX_QUERIED_PODS = 10


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return "kubernetes/prometheus" if len(segments) >= 2 and segments[-2] == PLURAL else None


def load_prometheus(ctx):
    """The target Prometheus CR. A missing one raises (NotFound from kubectl)."""
    if target_type(ctx) != TYPE:
        raise RwError(
            "invalid_input", f"k8s-prometheus-health does not apply to {target_type(ctx)!r}"
        )
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", PROMETHEUSES, ctx.resource.name)


def prometheus_pods(ctx, prom):
    """The pods of the CR, sorted by name (a list that is empty is a fact the caller reports)."""
    name = prom["metadata"]["name"]
    doc = kubectl_json(
        ctx,
        "-n",
        prom["metadata"]["namespace"],
        "get",
        "pods",
        "-l",
        POD_SELECTOR.format(name=name),
    )
    return sorted(doc["items"], key=lambda p: p["metadata"]["name"])


def endpoint(prom, pod):
    """(scheme, port, routePrefix) the CR's server listens on, read like the operator reads it:
    the container port named `spec.portName` (default `web`), https when `spec.web.tlsConfig` is
    set, and `spec.routePrefix`."""
    spec = prom.get("spec") or {}
    port_name = spec.get("portName") or DEFAULT_PORT_NAME
    scheme = "https" if (spec.get("web") or {}).get("tlsConfig") is not None else "http"
    prefix = (spec.get("routePrefix") or "").strip("/")
    port = DEFAULT_PORT
    containers = (pod.get("spec") or {}).get("containers") or []
    for container in sorted(containers, key=lambda c: c.get("name") != "prometheus"):
        for entry in container.get("ports") or []:
            if entry.get("name") == port_name:
                return (
                    scheme,
                    entry.get("containerPort", DEFAULT_PORT),
                    f"/{prefix}" if prefix else "",
                )
    return scheme, port, f"/{prefix}" if prefix else ""


def running(pod):
    """Whether the pod can be proxied to: Running and not being deleted."""
    return (pod.get("status") or {}).get("phase") == "Running" and not (
        pod["metadata"].get("deletionTimestamp")
    )


def fetch_one(ctx, prom, pod, path, parse, what):
    """GET `path` from one pod and parse the reply (RwError on any failure)."""
    scheme, port, prefix = endpoint(prom, pod)
    reply = proxy_get(
        ctx,
        pod_path(
            prom["metadata"]["namespace"], pod["metadata"]["name"], scheme, port, prefix + path
        ),
    )
    return parse(reply, f"{what} on pod {pod['metadata']['name']}")


def query_pods(ctx, prom, pods, path, what, parse):
    """GET `path` from every running pod (up to MAX_QUERIED_PODS) and parse each reply.

    Returns [(pod name, parse(reply))] for the pods that answered. A failed pod is reported with
    `ctx.error` and skipped, unless no pod answered, which raises the first failure: a task with
    nothing to report never returns an empty result. A forbidden proxy raises at once (every
    pod and path is forbidden alike). No running pod raises `unreachable`.
    """
    candidates = [p for p in pods if running(p)][:MAX_QUERIED_PODS]
    if not candidates:
        raise RwError(
            "unreachable",
            f"no running pod for Prometheus {prom['metadata']['name']}: nothing to query",
        )
    answers, failures = [], []
    for pod in candidates:
        name = pod["metadata"]["name"]
        try:
            answers.append((name, fetch_one(ctx, prom, pod, path, parse, what)))
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failures.append((name, exc))
    if not answers:
        name, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"pod/{name}") from exc
    for name, exc in failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"pod/{name}")
    return answers


def query_json(ctx, prom, pods, path, what):
    """`query_pods` for a Prometheus API path: the `data` of each answering pod."""
    return query_pods(ctx, prom, pods, path, what, api_json)
