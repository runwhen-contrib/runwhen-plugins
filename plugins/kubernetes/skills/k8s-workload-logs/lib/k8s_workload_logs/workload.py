"""Workload ownership and selection (copied from k8s-workload-health: skills do not share lib)."""

import math
import re
from datetime import timedelta

from k8s_workload_logs.errors import RwError

DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"
DAEMONSET = "DaemonSet"

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises RwError invalid_input."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError(
            "invalid_input",
            f"not a duration: {str(text)[:64]!r} (use ms, s, m, h, d, w: 90s, 1h30m)",
        )
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))


def since_seconds(text):
    """The `--since` value for kubectl logs (Go durations have no 'd'/'w'): whole seconds, rounded up, at least 1."""
    return max(1, math.ceil(parse_duration(text).total_seconds()))


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


def selector_string(selector):
    """Label selector (matchLabels + matchExpressions) -> kubectl -l string.

    An empty selector would select every pod in the namespace, so it raises.
    """
    selector = selector or {}
    terms = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], ",".join(expr.get("values") or [])
        if op == "In":
            terms.append(f"{key} in ({values})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({values})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            raise RwError("invalid_input", f"unknown selector operator {op!r}")
    if not terms:
        raise RwError("invalid_input", "workload has an empty selector")
    return ",".join(terms)


def matches_selector(labels, selector):
    labels = labels or {}
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _controller_uids(obj):
    return {
        ref.get("uid")
        for ref in (obj.get("metadata", {}).get("ownerReferences") or [])
        if ref.get("controller")
    }


def owned_revisions(workload, replicasets):
    """ReplicaSets whose controller is this Deployment."""
    uid = workload["metadata"]["uid"]
    return [i for i in replicasets if uid in _controller_uids(i)]


def owned_pods(kind, workload, replicasets, pods):
    """Pods that match the workload's selector AND hang off it through the
    ownerReferences chain (Deployment -> ReplicaSet -> Pod; StatefulSet or
    DaemonSet -> Pod). A pod with the right labels but another controller is
    not this workload's pod."""
    selector = workload["spec"]["selector"]
    if kind == DEPLOYMENT:
        owners = {r["metadata"]["uid"] for r in owned_revisions(workload, replicasets)}
    else:
        owners = {workload["metadata"]["uid"]}
    return [
        p
        for p in pods
        if matches_selector(p["metadata"].get("labels"), selector) and _controller_uids(p) & owners
    ]
