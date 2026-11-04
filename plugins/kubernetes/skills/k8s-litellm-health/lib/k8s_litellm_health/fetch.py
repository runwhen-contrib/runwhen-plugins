"""Fetching the target workload and choosing the pod that is asked (the only I/O besides k8s.py)."""

import time

from k8s_litellm_health.errors import RwError
from k8s_litellm_health.project import POD_FIELDS, WORKLOAD_FIELDS, kubectl_projected
from k8s_litellm_health.proxy import proxy_get

KINDS = {
    "kubernetes/statefulset": "statefulsets.apps",
    "kubernetes/deployment": "deployments.apps",
}
PLURAL_TYPES = {"statefulsets": "kubernetes/statefulset", "deployments": "kubernetes/deployment"}
DEFAULT_PORT = 4000
PORT_NAMES = ("http", "litellm", "http-litellm")
SCHEMES = ("http", "https")
# Inside the task's 120s timeout: after this nothing more is asked, and what was not asked is
# reported as a timeout.
# More pods than this are listed but not queried.
MAX_QUERIED_PODS = 10
TASK_DEADLINE = 100.0
MAX_CONSECUTIVE_TIMEOUTS = 3
REQUEST_SECONDS = 30
# Container names that are injected next to the application and are not LiteLLM.
SIDECAR_PREFIXES = (
    "istio-proxy",
    "linkerd-proxy",
    "envoy",
    "vault-agent",
    "oauth2-proxy",
    "cloud-sql-proxy",
    "config-reloader",
    "fluent",
    "filebeat",
    "kube-rbac-proxy",
    "otel-agent",
)


class Budget:
    """The time a task may spend asking: a deadline for the whole task, and a stop for the work
    on a service whose last MAX_CONSECUTIVE_TIMEOUTS asks all timed out."""

    def __init__(self, seconds=TASK_DEADLINE, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._end = clock() + seconds
        self._timeouts = 0

    def remaining(self):
        return self._end - self._clock()

    def reason(self):
        """Why nothing may be asked now, or None."""
        if self.remaining() < 1:
            return f"not asked: the task's {self.seconds:g}s deadline was reached"
        if self._timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            return f"not asked: {MAX_CONSECUTIVE_TIMEOUTS} consecutive asks timed out"
        return None

    def check(self):
        reason = self.reason()
        if reason:
            raise RwError("timeout", reason)

    def request_timeout(self):
        """Seconds for the next ask: never past the deadline."""
        return max(1, min(REQUEST_SECONDS, int(self.remaining())))

    def record(self, timed_out):
        self._timeouts = self._timeouts + 1 if timed_out else 0


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return PLURAL_TYPES.get(segments[-2]) if len(segments) >= 2 else None


def check_inputs(scheme, port):
    """Bad input is `invalid_input`, before any kubectl call."""
    if scheme not in SCHEMES:
        raise RwError("invalid_input", f"scheme must be one of {SCHEMES}, got {scheme!r}")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise RwError("invalid_input", f"port must be an integer from 1 to 65535, got {port!r}")


def load_workload(ctx):
    """The target StatefulSet or Deployment. A missing one raises (NotFound from kubectl)."""
    kind = KINDS.get(target_type(ctx))
    if kind is None:
        raise RwError("invalid_input", f"k8s-litellm-health does not apply to {target_type(ctx)!r}")
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
    """The workload's pods (its own selector), sorted by name."""
    selector = selector_string((workload.get("spec") or {}).get("selector") or {})
    doc = kubectl_projected(
        ctx,
        POD_FIELDS,
        "-n",
        workload["metadata"]["namespace"],
        "get",
        "pods",
        "-l",
        selector,
        many=True,
    )
    return sorted(doc["items"], key=lambda p: p["metadata"]["name"])


def is_litellm_image(image):
    """Whether a container image is a LiteLLM proxy's (ghcr.io/berriai/litellm...)."""
    return "litellm" in (image or "").lower()


def main_container(spec):
    """The container that runs LiteLLM in a pod spec or pod template spec: the one whose image is
    LiteLLM's, else the first that is not a known sidecar (a mesh proxy, a log shipper), else the
    first. None without containers."""
    containers = (spec or {}).get("containers") or []
    for container in containers:
        if is_litellm_image(container.get("image")):
            return container
    for container in containers:
        if not str(container.get("name") or "").lower().startswith(SIDECAR_PREFIXES):
            return container
    return containers[0] if containers else None


def serving_port(spec):
    """(port, declared) of the main container of a pod spec: its port named http, litellm or
    http-litellm, else 4000 (the proxy's default, declared False)."""
    for entry in (main_container(spec) or {}).get("ports") or []:
        if entry.get("name") in PORT_NAMES:
            return entry.get("containerPort", DEFAULT_PORT), True
    return DEFAULT_PORT, False


def pod_port(pod, port=None):
    """`port` if given (the override), else the main container's port named http, litellm or
    http-litellm, else 4000. Ports of the other containers are not looked at."""
    if port is not None:
        return port
    return serving_port(pod.get("spec"))[0]


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


def queried(pods):
    """The pods that are asked: Running ones, the first MAX_QUERIED_PODS."""
    return [p for p in pods if running(p)][:MAX_QUERIED_PODS]


def fetch_one(ctx, workload, pod, scheme, port, path, budget):
    """GET `path` from one pod: a Reply (200, 404 or 503) or RwError. An ask past the task's
    deadline, or after three timeouts in a row, raises `timeout` without asking."""
    where = (
        workload["metadata"]["namespace"],
        pod["metadata"]["name"],
        scheme,
        pod_port(pod, port),
        path,
    )
    budget.check()
    try:
        reply = proxy_get(ctx, *where, budget.request_timeout())
    except RwError as exc:
        budget.record(exc.code == "timeout")
        raise
    budget.record(False)
    return reply
