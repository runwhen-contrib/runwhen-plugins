"""Pure functions on the target workload and its pods: which Kind it is, which pods it owns,
which are ready and which container is Redis. No kubectl, no clock."""

from k8s_redis_health.errors import RwError

DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"

TEXT_LIMIT = 1024  # longest free-text field returned
NESTED_MAX = 50  # longest nested list returned inside an entry

# ctx.resource.type -> (Kind, kubectl resource)
_BY_TYPE = {
    "kubernetes/deployment": (DEPLOYMENT, "deployments.apps"),
    "kubernetes/statefulset": (STATEFULSET, "statefulsets.apps"),
}
# the plural segment of ctx.resource.path -> same
_BY_PLURAL = {resource.split(".")[0]: (kind, resource) for kind, resource in _BY_TYPE.values()}


def resolve_target(resource_type, resource_path=None):
    """(Kind, kubectl resource) from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path` (`.../statefulsets/redis`). Anything else is invalid_input."""
    if resource_type in _BY_TYPE:
        return _BY_TYPE[resource_type]
    if not resource_type and resource_path:
        for segment in reversed(str(resource_path).strip("/").split("/")):
            if segment in _BY_PLURAL:
                return _BY_PLURAL[segment]
    raise RwError(
        "invalid_input",
        f"k8s-redis-health applies to a Deployment or StatefulSet, not {resource_type or resource_path!r}",
    )


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


def owned_replicasets(workload, items):
    """The ReplicaSets whose controller is this Deployment."""
    uid = workload["metadata"]["uid"]
    return [i for i in items if uid in _controller_uids(i)]


def owned_pods(kind, workload, replicasets, pods):
    """Pods that match the workload's selector AND hang off it through the ownerReferences chain
    (Deployment -> ReplicaSet -> Pod; StatefulSet -> Pod). `replicasets` matters only for a
    Deployment. Sorted by name."""
    selector = workload["spec"]["selector"]
    if kind == DEPLOYMENT:
        owners = {r["metadata"]["uid"] for r in owned_replicasets(workload, replicasets)}
    else:
        owners = {workload["metadata"]["uid"]}
    found = [
        p
        for p in pods
        if matches_selector(p["metadata"].get("labels"), selector) and _controller_uids(p) & owners
    ]
    return sorted(found, key=lambda p: p["metadata"]["name"])


# ---------------------------------------------------------------- pods ----


def pod_ready(pod):
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def _image_name(image):
    """'registry/redis:7-alpine@sha256:..' -> 'redis'."""
    return (image or "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]


SERVER_NAMES = ("redis", "valkey")


def choose_container(pod, requested=None):
    """The name of the container to run redis-cli in: `requested` (must exist), else the one named
    redis or valkey, else the first whose image name starts with redis or valkey (not an exporter
    or sentinel), else the first container."""
    containers = pod.get("spec", {}).get("containers") or []
    names = [c["name"] for c in containers]
    if requested:
        if requested not in names:
            raise RwError(
                "invalid_input",
                f"pod {pod['metadata']['name']} has no container {requested!r}; it has {', '.join(names)}",
            )
        return requested
    for name in SERVER_NAMES:
        if name in names:
            return name
    for c in containers:
        image = _image_name(c.get("image"))
        if image.startswith(SERVER_NAMES) and "exporter" not in image and "sentinel" not in image:
            return c["name"]
    if not names:
        raise RwError("not_found", f"pod {pod['metadata']['name']} has no containers")
    return names[0]
