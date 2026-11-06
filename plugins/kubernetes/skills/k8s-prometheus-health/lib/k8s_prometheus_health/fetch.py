"""Fetching the target Prometheus and talking to its pods (the only I/O besides k8s.py)."""

import time

from k8s_prometheus_health.common import MESSAGE_LIMIT
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.k8s import kubectl_json
from k8s_prometheus_health.proxy import api_json, proxy_get

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
# The label the operator puts on each pod of a sharded Prometheus.
SHARD_LABEL = "operator.prometheus.io/shard"
# Inside the tasks' 120s timeout: after this nothing more is asked, and what was not asked is
# reported as a timeout.
TASK_DEADLINE = 100.0
MAX_CONSECUTIVE_TIMEOUTS = 3
REQUEST_SECONDS = 30


class Budget:
    """The time a task may spend asking pods: a deadline for the whole task, and a stop for a
    path whose last MAX_CONSECUTIVE_TIMEOUTS asks (of consecutive pods) all timed out."""

    def __init__(self, seconds=TASK_DEADLINE, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._end = clock() + seconds
        self._timeouts = {}

    def remaining(self):
        return self._end - self._clock()

    def reason(self, path):
        """Why `path` may not be asked now, or None."""
        if self.remaining() < 1:
            return f"not asked: the task's {self.seconds:g}s deadline was reached"
        if self._timeouts.get(path, 0) >= MAX_CONSECUTIVE_TIMEOUTS:
            return f"not asked: {MAX_CONSECUTIVE_TIMEOUTS} consecutive pods timed out on {path}"
        return None

    def check(self, path):
        reason = self.reason(path)
        if reason:
            raise RwError("timeout", reason)

    def request_timeout(self):
        """Seconds for the next ask: never past the deadline."""
        return max(1, min(REQUEST_SECONDS, int(self.remaining())))

    def record(self, path, timed_out):
        self._timeouts[path] = self._timeouts.get(path, 0) + 1 if timed_out else 0


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


def select_pods(pods):
    """(the running pods to ask, in name order; how many running pods are not asked).

    At most MAX_QUERIED_PODS: the first pod of every shard (operator.prometheus.io/shard) first,
    since shards scrape different targets and replicas the same ones, then more replicas."""
    live = [p for p in pods if running(p)]
    chosen, shards = [], set()
    for pod in live:
        shard = (pod["metadata"].get("labels") or {}).get(SHARD_LABEL)
        if shard not in shards:
            shards.add(shard)
            chosen.append(pod)
    chosen = chosen[:MAX_QUERIED_PODS]
    for pod in live:
        if len(chosen) >= MAX_QUERIED_PODS:
            break
        if pod not in chosen:
            chosen.append(pod)
    names = {p["metadata"]["name"] for p in chosen}
    asked = [p for p in live if p["metadata"]["name"] in names]
    return asked, len(live) - len(asked)


def pod_stats(pods):
    """`podCount` and `podsSkipped` of a task output."""
    return {"podCount": len(pods), "podsSkipped": select_pods(pods)[1]}


def fetch_one(ctx, prom, pod, path, parse, what, budget=None):
    """GET `path` from one pod and parse the reply (RwError on any failure). With a `budget`, a
    ask that is past the deadline, or on a path that timed out for the last pods, raises
    `timeout` without asking."""
    scheme, port, prefix = endpoint(prom, pod)
    where = (prom["metadata"]["namespace"], pod["metadata"]["name"], scheme, port, prefix + path)
    if budget is None:
        reply = proxy_get(ctx, *where)
    else:
        budget.check(path)
        try:
            reply = proxy_get(ctx, *where, budget.request_timeout())
        except RwError as exc:
            budget.record(path, exc.code == "timeout")
            raise
        budget.record(path, False)
    return parse(reply, f"{what} on pod {pod['metadata']['name']}")


def query_pods(ctx, prom, pods, path, what, parse, budget=None):
    """GET `path` from the running pods `select_pods` picks and parse each reply.

    Returns [(pod name, parse(reply))] for the pods that answered. A failed pod is reported with
    `ctx.error` and skipped, unless no pod answered, which raises the first failure: a task with
    nothing to report never returns an empty result. A forbidden proxy raises at once (every
    pod and path is forbidden alike). No running pod raises `unreachable`.
    """
    candidates = select_pods(pods)[0]
    budget = budget or Budget()
    if not candidates:
        raise RwError(
            "unreachable",
            f"no running pod for Prometheus {prom['metadata']['name']}: nothing to query",
        )
    answers, failures = [], []
    for pod in candidates:
        name = pod["metadata"]["name"]
        try:
            answers.append((name, fetch_one(ctx, prom, pod, path, parse, what, budget)))
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


def query_json(ctx, prom, pods, path, what, budget=None):
    """`query_pods` for a Prometheus API path: the `data` of each answering pod."""
    return query_pods(ctx, prom, pods, path, what, api_json, budget)
