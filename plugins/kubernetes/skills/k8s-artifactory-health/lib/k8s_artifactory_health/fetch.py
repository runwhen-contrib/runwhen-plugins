"""Fetching the target workload, its pods and Services, and asking them (the only I/O besides
k8s.py)."""

import time

from k8s_artifactory_health.common import MESSAGE_LIMIT
from k8s_artifactory_health.errors import RwError
from k8s_artifactory_health.project import (
    POD_FIELDS,
    SERVICE_FIELDS,
    WORKLOAD_FIELDS,
    kubectl_json_scoped_projected,
    kubectl_projected,
)
from k8s_artifactory_health.proxy import pod_path, proxy_get, service_path

KINDS = {
    "kubernetes/statefulset": "statefulsets.apps",
    "kubernetes/deployment": "deployments.apps",
}
PLURAL_TYPES = {"statefulsets": "kubernetes/statefulset", "deployments": "kubernetes/deployment"}
# The official JFrog chart: `http-internal` 8081 is Artifactory, `http` 8082 is the router.
DEFAULTS = {"artifactory": 8081, "router": 8082}
PORT_NAMES = {
    "artifactory": ("http-internal", "http-artifactory", "artifactory"),
    "router": ("http", "http-router", "router"),
}
SCHEMES = ("http", "https")
# More pods or Services than this are listed but not asked.
MAX_QUERIED = 10
# Inside the task's 120s timeout: after this nothing more is asked, and what was not asked is
# reported as a timeout.
TASK_DEADLINE = 100.0
MAX_CONSECUTIVE_TIMEOUTS = 3
REQUEST_SECONDS = 30


class Budget:
    """The time a task may spend asking: a deadline for the whole task, and a stop for a path
    whose last MAX_CONSECUTIVE_TIMEOUTS asks all timed out."""

    def __init__(self, seconds=TASK_DEADLINE, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._end = clock() + seconds
        self._timeouts = {}

    def remaining(self):
        return self._end - self._clock()

    def check(self, path):
        if self.remaining() < 1:
            raise RwError(
                "timeout", f"not asked: the task's {self.seconds:g}s deadline was reached"
            )
        if self._timeouts.get(path, 0) >= MAX_CONSECUTIVE_TIMEOUTS:
            raise RwError(
                "timeout",
                f"not asked: {MAX_CONSECUTIVE_TIMEOUTS} asks in a row timed out on {path}",
            )

    def request_timeout(self):
        """The kubectl --request-timeout for the next ask: never past the deadline."""
        return f"{max(1, min(REQUEST_SECONDS, int(self.remaining())))}s"

    def record(self, path, timed_out):
        self._timeouts[path] = self._timeouts.get(path, 0) + 1 if timed_out else 0


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return PLURAL_TYPES.get(segments[-2]) if len(segments) >= 2 else None


def check_inputs(scheme, port, router_port):
    """Bad input is `invalid_input`, before any kubectl call."""
    if scheme not in SCHEMES:
        raise RwError("invalid_input", f"scheme must be one of {SCHEMES}, got {scheme!r}")
    for name, value in (("port", port), ("routerPort", router_port)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535
        ):
            raise RwError(
                "invalid_input", f"{name} must be an integer from 1 to 65535, got {value!r}"
            )


def load_workload(ctx):
    """The target StatefulSet or Deployment (projected: no pod template beyond its labels). A
    missing one raises (NotFound from kubectl)."""
    kind = KINDS.get(target_type(ctx))
    if kind is None:
        raise RwError(
            "invalid_input", f"k8s-artifactory-health does not apply to {target_type(ctx)!r}"
        )
    return kubectl_projected(
        ctx, WORKLOAD_FIELDS, "-n", ctx.resource.namespace, "get", kind, ctx.resource.name
    )


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
    items = kubectl_projected(
        ctx, POD_FIELDS, "-n", workload["metadata"]["namespace"], "get", "pods", "-l", selector,
        many=True,
    )["items"]  # fmt: skip
    return sorted(items, key=lambda p: p["metadata"]["name"])


def workload_services(ctx, workload):
    """The Services that select the workload's pods, sorted by name: (services, readable). A
    Service counts when its selector is non-empty and every pair of it is in the pod template's
    labels. A list the caller may not make is reported (`ctx.error`, scope `services`) and gives
    ([], False): the pod facts stand without it."""
    doc = kubectl_json_scoped_projected(
        ctx, "services", SERVICE_FIELDS, "-n", workload["metadata"]["namespace"], "get",
        "services", many=True,
    )  # fmt: skip
    if doc is None:
        return [], False
    labels = ((workload.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels")
    labels = labels or {}
    chosen = [
        s
        for s in doc["items"]
        if (s.get("spec") or {}).get("selector")
        and all(labels.get(k) == v for k, v in s["spec"]["selector"].items())
    ]
    return sorted(chosen, key=lambda s: s["metadata"]["name"]), True


def container_ports(pod):
    """[(name or None, number)] of every declared container port of the pod, init containers
    excluded (they are not served from)."""
    found = []
    for container in (pod.get("spec") or {}).get("containers") or []:
        for entry in container.get("ports") or []:
            if "containerPort" in entry:
                found.append((entry.get("name"), entry["containerPort"]))
    return found


def pod_port(pod, kind, override=None):
    """(number, name or None) of the `kind` ("artifactory" or "router") port: the input if given,
    else the pod's container port with one of the chart's names, else the chart's default."""
    if override is not None:
        return override, next((n for n, num in container_ports(pod) if num == override), None)
    names = PORT_NAMES[kind]
    for name, number in container_ports(pod):
        if name in names:
            return number, name
    return DEFAULTS[kind], None


def service_port_for(service, number, names=()):
    """(proxy port, port number) of the Service port whose targetPort is the container port
    `number` or one of its `names`: the proxy port is the Service port's name if it has one, else
    its number. None when the Service does not expose that container port."""
    for entry in (service.get("spec") or {}).get("ports") or []:
        target = entry.get("targetPort", entry.get("port"))
        if target == number or target in names:
            return entry.get("name") or entry.get("port"), entry.get("port")
    return None


def running(pod):
    """Whether the pod can be proxied to: Running and not being deleted."""
    return (pod.get("status") or {}).get("phase") == "Running" and not (
        pod["metadata"].get("deletionTimestamp")
    )


def pod_ready(pod):
    for cond in (pod.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return None


def queried(pods):
    """The pods that are asked: Running ones, the first MAX_QUERIED."""
    return [p for p in pods if running(p)][:MAX_QUERIED]


def ask(ctx, api_path, budget, key):
    """GET one URL through the proxy within the budget: a Reply or RwError. `key` names the
    endpoint for the budget's timeout streak."""
    budget.check(key)
    try:
        reply = proxy_get(ctx, api_path, budget.request_timeout())
    except RwError as exc:
        budget.record(key, exc.code == "timeout")
        raise
    budget.record(key, False)
    return reply


def pod_url(workload, pod, scheme, port, path):
    return pod_path(workload["metadata"]["namespace"], pod["metadata"]["name"], scheme, port, path)


def service_url(workload, service, scheme, port, path):
    return service_path(
        workload["metadata"]["namespace"], service["metadata"]["name"], scheme, port, path
    )


def short(exc):
    return exc.message[:MESSAGE_LIMIT]
