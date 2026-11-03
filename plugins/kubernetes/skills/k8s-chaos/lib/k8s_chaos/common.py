"""Pure functions behind the k8s-chaos tasks.

Everything here takes already-parsed Kubernetes JSON (or plain inputs) and returns
plain facts or a plan: no kubectl, no clock, no environment. The actions in
actions.py fetch, call these, run the mutating command and observe.
"""

import hashlib
import math
import re
from decimal import Decimal, InvalidOperation

from k8s_chaos.errors import RwError

TEXT_LIMIT = 1024
NESTED_LIMIT = 50
MIB = 1024 * 1024

NAMESPACE = "Namespace"
DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"

# ctx.resource.type -> Kind, and the plural path segment -> Kind
_BY_TYPE = {
    "kubernetes/namespace": NAMESPACE,
    "kubernetes/deployment": DEPLOYMENT,
    "kubernetes/statefulset": STATEFULSET,
}
_BY_PLURAL = {"namespaces": NAMESPACE, "deployments": DEPLOYMENT, "statefulsets": STATEFULSET}
RESOURCE = {DEPLOYMENT: "deployments.apps", STATEFULSET: "statefulsets.apps"}


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


# -------------------------------------------------------------- target ----


def resolve_target(resource_type, resource_path, namespace, name):
    """{scope, kind, namespace, name} of the target.

    `scope` is 'namespace' (act on pods anywhere in the namespace) or 'workload'
    (act on that workload's pods only). Kind comes from `ctx.resource.type`, else
    the plural segment of `ctx.resource.path`. Any other type raises
    RwError('invalid_input').
    """
    kind = _BY_TYPE.get(resource_type)
    if kind is None and not resource_type and resource_path:
        for segment in reversed(str(resource_path).strip("/").split("/")):
            if segment in _BY_PLURAL:
                kind = _BY_PLURAL[segment]
                break
    if kind is None:
        raise RwError(
            "invalid_input",
            f"k8s-chaos applies to a namespace, deployment or statefulset, not {resource_type or resource_path!r}",
        )
    if kind == NAMESPACE:
        return {"scope": "namespace", "kind": kind, "namespace": name, "name": name}
    return {"scope": "workload", "kind": kind, "namespace": namespace, "name": name}


_GITOPS_PREFIXES = (
    ("flux", "kustomize.toolkit.fluxcd.io/"),
    ("flux", "helm.toolkit.fluxcd.io/"),
    ("argocd", "argocd.argoproj.io/"),
)
_GITOPS_MANAGED_BY = {"flux": "flux", "fluxcd": "flux", "argocd": "argocd", "argo-cd": "argocd"}


def gitops_tool(obj):
    """'flux', 'argocd' or None: whether Flux or Argo CD labels or annotations are on
    the object. A fact: a GitOps controller heals most chaos, and that is the approver's call."""
    meta = obj.get("metadata", {})
    for mapping in (meta.get("labels"), meta.get("annotations")):
        for key, value in sorted((mapping or {}).items()):
            found = next((t for t, prefix in _GITOPS_PREFIXES if key.startswith(prefix)), None)
            if key == "app.kubernetes.io/managed-by":
                found = _GITOPS_MANAGED_BY.get(str(value).lower())
            if found:
                return found
    return None


def target_view(target, workload):
    """The `target` output every task returns."""
    base = {
        "scope": target["scope"],
        "kind": target["kind"],
        "name": target["name"],
        "namespace": target["namespace"],
        "desiredReplicas": None,
        "readyReplicas": None,
        "gitopsTool": None,
    }
    if workload is not None:
        status = workload.get("status", {})
        base["desiredReplicas"] = workload.get("spec", {}).get("replicas")
        base["readyReplicas"] = status.get("readyReplicas") or 0
        base["gitopsTool"] = gitops_tool(workload)
    return base


# ------------------------------------------------------------ selectors ----


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
        # the classic tasks fell through to every pod of the namespace here
        raise RwError(
            "invalid_input", "the workload has an empty selector; refusing to act on pods"
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


def _controller_uids(obj):
    return {
        ref.get("uid")
        for ref in (obj.get("metadata", {}).get("ownerReferences") or [])
        if ref.get("controller")
    }


def owned_pods(kind, workload, replicasets, pods):
    """Pods that match the workload's selector AND hang off it through the
    ownerReferences chain (Deployment -> ReplicaSet -> Pod; StatefulSet -> Pod)."""
    selector = workload["spec"]["selector"]
    if kind == DEPLOYMENT:
        uid = workload["metadata"]["uid"]
        owners = {r["metadata"]["uid"] for r in replicasets if uid in _controller_uids(r)}
    else:
        owners = {workload["metadata"]["uid"]}
    return [
        p
        for p in pods
        if matches_selector(p["metadata"].get("labels"), selector) and _controller_uids(p) & owners
    ]


# ----------------------------------------------------------------- pods ----


def is_running(pod):
    """Running and not already terminating: a pod that can be acted on."""
    return (
        pod.get("status", {}).get("phase") == "Running"
        and pod["metadata"].get("deletionTimestamp") is None
    )


def ready_condition(pod):
    cond = next(
        (c for c in pod.get("status", {}).get("conditions") or [] if c.get("type") == "Ready"),
        None,
    )
    return cond is not None and cond.get("status") == "True"


def pod_view(pod):
    status = pod.get("status", {})
    return {
        "name": pod["metadata"]["name"],
        "phase": status.get("phase"),
        "ready": ready_condition(pod),
        "node": pod.get("spec", {}).get("nodeName"),
    }


def container_status(pod, container):
    """The status entry of a regular container, or None."""
    for s in pod.get("status", {}).get("containerStatuses") or []:
        if s.get("name") == container:
            return s
    return None


def restart_count(pod, container):
    status = container_status(pod, container)
    return int(status.get("restartCount") or 0) if status else 0


# ------------------------------------------------------------ selection ----


def seed_of(seed_input, now):
    """(seed, source): the caller's `seed` input, else the run's reference time
    (ctx.now()), as text. The same seed picks the same pods from the same candidates."""
    if seed_input is not None and str(seed_input).strip() != "":
        return str(seed_input).strip(), "input"
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "now"


def pick(names, count, seed):
    """`count` of `names`, chosen by a hash of (seed, name): random-looking, stable
    for one seed and one candidate set, independent of the Python version."""
    ranked = sorted(
        set(names), key=lambda n: (hashlib.sha256(f"{seed}\0{n}".encode()).hexdigest(), n)
    )
    return ranked[:count]


# -------------------------------------------------------------- inputs ----

_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text, name, minimum=0, maximum=None):
    """'90s', '2m', '1m30s', '500ms' -> seconds (float), within [minimum, maximum].
    Bad input raises RwError('invalid_input')."""
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s:
        raise RwError("invalid_input", f"{name} must be a duration such as 90s or 2m, got {text!r}")
    seconds = sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)
    if seconds < minimum or (maximum is not None and seconds > maximum):
        raise RwError(
            "invalid_input",
            f"{name} must be between {minimum:g}s and {maximum:g}s, got {text!r}"
            if maximum is not None
            else f"{name} must be at least {minimum:g}s, got {text!r}",
        )
    return seconds


def check_int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RwError(
            "invalid_input", f"{name} must be a whole number from {low} to {high}, got {value!r}"
        )
    return value


def check_container_name(container):
    if container is None:
        return None
    if not isinstance(container, str) or not re.fullmatch(
        r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", container
    ):
        raise RwError("invalid_input", f"container must be a container name, got {container!r}")
    return container


# ----------------------------------------------------------- quantities ----

_QUANTITY = re.compile(
    r"^\+?(\d+(?:\.\d*)?|\.\d+)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E|[eE][+-]?\d+)?$"
)
_SCALE = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}


def parse_bytes(text):
    """A memory quantity ('256Mi', '1G', '129e6', '1048576') -> whole bytes (int).
    Anything else raises ValueError."""
    match = _QUANTITY.match(str(text).strip())
    if not match:
        raise ValueError(f"not a quantity: {text!r}")
    number, suffix = match.group(1), match.group(2) or ""
    if len(number) > 30:
        raise ValueError(f"quantity is too long: {text!r}")
    try:
        if suffix[:1] in ("e", "E") and len(suffix) > 1:
            exponent = int(suffix[1:])
            if abs(exponent) > 30:
                raise ValueError(f"exponent out of range in {text!r}")
            value = Decimal(number) * Decimal(10) ** exponent
        else:
            value = Decimal(number) * _SCALE[suffix]
        return int(value.to_integral_value(rounding="ROUND_CEILING"))
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"not a quantity: {text!r}") from None


def memory_limit(pod, container=None):
    """(container name, limit bytes) of the first regular container (or the one
    named) that has a memory limit; None when none has. An unparsable limit counts
    as no limit."""
    for c in pod.get("spec", {}).get("containers") or []:
        if container and c["name"] != container:
            continue
        raw = ((c.get("resources") or {}).get("limits") or {}).get("memory")
        if raw is None:
            continue
        try:
            value = parse_bytes(raw)
        except ValueError:
            continue
        if value > 0:
            return c["name"], value
    return None


def allocation_bytes(limit):
    """How much memory to allocate to go past `limit`: a quarter more, at least 64Mi more,
    whole MiB."""
    return math.ceil((limit + max(64 * MIB, limit // 4)) / MIB) * MIB


def first_container(pod, container=None):
    """The named regular container, else the first one; None when there is none."""
    for c in pod.get("spec", {}).get("containers") or []:
        if container is None or c["name"] == container:
            return c["name"]
    return None
