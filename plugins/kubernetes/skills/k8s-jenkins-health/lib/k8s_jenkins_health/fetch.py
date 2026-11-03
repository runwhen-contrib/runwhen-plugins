"""Fetching the target workload and its pods, and asking them (the only I/O besides k8s.py)."""

import re
import time
from datetime import timedelta

from k8s_jenkins_health.common import MESSAGE_LIMIT
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.project import (
    POD_FIELDS,
    WORKLOAD_FIELDS,
    kubectl_projected,
)
from k8s_jenkins_health.proxy import pod_path, proxy_get

KINDS = {
    "kubernetes/statefulset": "statefulsets.apps",
    "kubernetes/deployment": "deployments.apps",
}
PLURAL_TYPES = {"statefulsets": "kubernetes/statefulset", "deployments": "kubernetes/deployment"}
DEFAULT_PORT = 8080
# the jenkinsci chart names the controller's container port `http`
PORT_NAMES = ("http", "jenkins", "web")
SCHEMES = ("http", "https")
# More pods than this are listed but not asked.
MAX_QUERIED = 10
# Inside the tasks' 120s timeout: after this nothing more is asked, and what was not asked is
# reported as a timeout.
TASK_DEADLINE = 100.0
MAX_CONSECUTIVE_TIMEOUTS = 3
REQUEST_SECONDS = 30
_PREFIX = re.compile(r"(?:/[A-Za-z0-9_.-]+)*")
_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_DURATION_FULL = re.compile(r"(?:\d+(?:ms|s|m|h|d))+")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


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


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else is invalid_input."""
    if isinstance(text, timedelta):
        return text
    value = str(text)
    if not _DURATION_FULL.fullmatch(value):
        raise RwError("invalid_input", f"not a duration: {text!r} (use e.g. 90s, 30m, 1h, 2d)")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION.findall(value)))


def target_type(ctx):
    """ctx.resource.type, else the type named by the plural segment of ctx.resource.path."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    segments = (getattr(ctx.resource, "path", None) or "").split("/")
    return PLURAL_TYPES.get(segments[-2]) if len(segments) >= 2 else None


def check_inputs(scheme, port, prefix=""):
    """Bad input is `invalid_input`, before any kubectl call (a proxy path is never built from
    anything but plain path segments)."""
    if (
        not isinstance(prefix, str)
        or not _PREFIX.fullmatch(prefix)
        or any(seg in (".", "..") for seg in prefix.split("/"))
    ):
        raise RwError(
            "invalid_input",
            f"urlPrefix must be empty or plain path segments like /jenkins: {prefix!r}",
        )
    if scheme not in SCHEMES:
        raise RwError("invalid_input", f"scheme must be one of {SCHEMES}, got {scheme!r}")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise RwError("invalid_input", f"port must be an integer from 1 to 65535, got {port!r}")


def load_workload(ctx):
    """The target StatefulSet or Deployment (projected: no pod template). A missing one raises
    (NotFound from kubectl)."""
    kind = KINDS.get(target_type(ctx))
    if kind is None:
        raise RwError("invalid_input", f"k8s-jenkins-health does not apply to {target_type(ctx)!r}")
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
        ctx,
        POD_FIELDS,
        "-n",
        workload["metadata"]["namespace"],
        "get",
        "pods",
        "-l",
        selector,
        many=True,
    )["items"]
    return sorted(items, key=lambda p: p["metadata"]["name"])


def pod_port(pod, port=None):
    """`port` if given, else the pod's container port named http (the chart's), jenkins or web,
    else 8080."""
    if port is not None:
        return port
    for container in (pod.get("spec") or {}).get("containers") or []:
        for entry in container.get("ports") or []:
            if entry.get("name") in PORT_NAMES and "containerPort" in entry:
                return entry["containerPort"]
    return DEFAULT_PORT


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


def ask(ctx, workload, pod, scheme, port, path, budget, headers=False):
    """GET `path` from one pod within the budget: a Reply or RwError. A call past the deadline, or
    on a path that timed out for the last pods, raises `timeout` without asking."""
    budget.check(path)
    api_path = pod_path(
        workload["metadata"]["namespace"], pod["metadata"]["name"], scheme, port, path
    )
    try:
        reply = proxy_get(ctx, api_path, budget.request_timeout(), headers)
    except RwError as exc:
        budget.record(path, exc.code == "timeout")
        raise
    budget.record(path, False)
    return reply


def short(exc):
    return exc.message[:MESSAGE_LIMIT]
