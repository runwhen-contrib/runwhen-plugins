"""Pure functions on the target and the workloads and pods under it: which kind of target it is,
which pods a workload owns, readiness, bounds, time. No kubectl, no clock."""

import re
from datetime import UTC, datetime, timedelta

from k8s_vault_health.errors import RwError

STATEFULSET = "StatefulSet"
DAEMONSET = "DaemonSet"
NAMESPACE = "Namespace"

TEXT_LIMIT = 1024  # longest free-text field returned
NESTED_MAX = 50  # longest nested list returned inside an entry

# What the Vault Helm chart puts on its objects; a namespace target is searched for these.
VAULT_SERVER_LABEL = "app.kubernetes.io/name=vault"
VAULT_CSI_LABEL = "app.kubernetes.io/name=vault-csi-provider"

# ctx.resource.type -> (Kind, kubectl resource or None)
_BY_TYPE = {
    "kubernetes/statefulset": (STATEFULSET, "statefulsets.apps"),
    "kubernetes/daemonset": (DAEMONSET, "daemonsets.apps"),
    "kubernetes/namespace": (NAMESPACE, None),
}
# the plural segment of ctx.resource.path -> same
_BY_PLURAL = {
    "statefulsets": _BY_TYPE["kubernetes/statefulset"],
    "daemonsets": _BY_TYPE["kubernetes/daemonset"],
    "namespaces": _BY_TYPE["kubernetes/namespace"],
}

_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_DURATION_FULL = re.compile(r"(?:\d+(?:ms|s|m|h|d))+")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def resolve_target(resource_type, resource_path=None):
    """(Kind, kubectl resource) from `ctx.resource.type`, else from the plural segment before the
    name at the end of `ctx.resource.path`. Anything else is invalid_input."""
    if resource_type in _BY_TYPE:
        return _BY_TYPE[resource_type]
    if not resource_type and resource_path:
        parts = str(resource_path).strip("/").split("/")
        if len(parts) >= 2 and parts[-2] in _BY_PLURAL:
            return _BY_PLURAL[parts[-2]]
    raise RwError(
        "invalid_input",
        "k8s-vault-health applies to a StatefulSet, a DaemonSet or a namespace, "
        f"not {resource_type or resource_path!r}",
    )


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta; anything else is invalid_input."""
    if isinstance(text, timedelta):
        return text
    s = str(text)
    if not _DURATION_FULL.fullmatch(s):
        raise RwError("invalid_input", f"not a duration: {text!r} (use e.g. 90s, 30m, 1h, 2d)")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION.findall(s)))


def parse_time(value):
    """RFC 3339 string (as the Kubernetes API writes it) -> aware datetime, or None."""
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fmt_time(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


# ----------------------------------------------------------- selectors ----


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
        raise RwError(
            "invalid_input", "workload has an empty selector; refusing to select every pod"
        )
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


# ------------------------------------------------------------ ownership ----


def _controller_uids(obj):
    return {
        ref.get("uid")
        for ref in (obj.get("metadata", {}).get("ownerReferences") or [])
        if ref.get("controller")
    }


def owned_pods(workload, pods):
    """Pods that match the workload's selector AND are controlled by it (StatefulSet or DaemonSet
    -> Pod through ownerReferences). A pod with the right labels but another controller is not this
    workload's pod. Sorted by name."""
    selector = workload["spec"]["selector"]
    uid = workload["metadata"]["uid"]
    found = [
        p
        for p in pods
        if matches_selector(p["metadata"].get("labels"), selector) and uid in _controller_uids(p)
    ]
    return sorted(found, key=lambda p: p["metadata"]["name"])


# ---------------------------------------------------------------- pods ----


def pod_ready(pod):
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def container_statuses(pod):
    """(is_init, status) for every init container (native sidecars included) and container."""
    status = pod.get("status", {})
    for s in status.get("initContainerStatuses") or []:
        yield True, s
    for s in status.get("containerStatuses") or []:
        yield False, s


def restart_total(pod):
    return sum(s.get("restartCount", 0) for _init, s in container_statuses(pod))
