"""Which pods and workloads are one SeaweedFS release, and which role each plays (pure)."""

import shlex

from k8s_seaweedfs_health.common import NESTED_LIMIT
from k8s_seaweedfs_health.errors import RwError

NAME_LABEL = "app.kubernetes.io/name"
INSTANCE_LABEL = "app.kubernetes.io/instance"
COMPONENT_LABEL = "app.kubernetes.io/component"
NAME_VALUE = "seaweedfs"

NAMESPACE = "namespace"
STATEFULSET = "statefulset"
TYPES = {"kubernetes/namespace": NAMESPACE, "kubernetes/statefulset": STATEFULSET}
PLURALS = {"namespaces": NAMESPACE, "statefulsets": STATEFULSET}

ROLES = ("master", "volume", "filer", "s3")
# the chart's component label -> roles that pod serves; "all-in-one" (any label value
# containing it) is one process doing master, volume and filer, and S3 only with an s3 flag
COMPONENT_ROLES = {"master": ("master",), "volume": ("volume",), "filer": ("filer",), "s3": ("s3",)}
ALL_IN_ONE_ROLES = ("master", "volume", "filer")
_FALSE = ("false", "0", "no", "off", "f")


def resolve_target(resource_type, resource_path=None):
    """'namespace' or 'statefulset' from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path`. Anything else raises RwError('invalid_input')."""
    if resource_type in TYPES:
        return TYPES[resource_type]
    if not resource_type and resource_path:
        parts = str(resource_path).strip("/").split("/")
        if len(parts) >= 2 and parts[-2] in PLURALS:
            return PLURALS[parts[-2]]
    raise RwError(
        "invalid_input",
        f"k8s-seaweedfs-health does not apply to {resource_type or resource_path!r}",
    )


def selector(release):
    """Label selector of one release's pods and workloads (every release when `release` is empty)."""
    parts = [f"{NAME_LABEL}={NAME_VALUE}"]
    if release:
        parts.append(f"{INSTANCE_LABEL}={release}")
    return ",".join(parts)


def labels_of(obj):
    return (obj.get("metadata") or {}).get("labels") or {}


def release_of_statefulset(sts):
    """The release a StatefulSet belongs to: its app.kubernetes.io/instance label. A StatefulSet
    that is not SeaweedFS's, or has no instance label, is invalid input (the target is wrong)."""
    labels = labels_of(sts)
    name = sts["metadata"]["name"]
    if labels.get(NAME_LABEL) != NAME_VALUE:
        raise RwError(
            "invalid_input",
            f"StatefulSet {name!r} has no label {NAME_LABEL}={NAME_VALUE}: it is not a SeaweedFS workload",
        )
    if not labels.get(INSTANCE_LABEL):
        raise RwError(
            "invalid_input",
            f"StatefulSet {name!r} has no {INSTANCE_LABEL} label to tell which release it belongs to; "
            "run the task on the namespace with the release input",
        )
    return labels[INSTANCE_LABEL]


def releases_of(items):
    """Sorted distinct app.kubernetes.io/instance values ('' for objects without one)."""
    return sorted({labels_of(i).get(INSTANCE_LABEL, "") for i in items})


def _words(arg):
    """The words of one argv element: the element itself, and what a shell would split it into (the
    chart runs `/bin/sh -ec "exec weed filer ... -s3 ..."`)."""
    yield arg
    try:
        yield from shlex.split(arg)
    except ValueError:  # unbalanced quotes: fall back to blanks
        yield from arg.split()


def is_s3_flag(word):
    """True for `-s3`, `--s3`, `-s3=true` and `-s3.port=...` (any number of leading dashes up to two)."""
    if not word.startswith("-") or word.startswith("---"):
        return False
    name, _, value = word.lstrip("-").partition("=")
    if name == "s3":
        return value.lower() not in _FALSE
    return name == "s3.port" and bool(value)


def serves_s3_by_flag(pod):
    """Whether any container's command or args (including words inside a shell-form string) enable S3."""
    for c in (pod.get("spec") or {}).get("containers") or []:
        for arg in (c.get("command") or []) + (c.get("args") or []):
            if any(is_s3_flag(w) for w in _words(str(arg))):
                return True
    return False


def roles_of(pod):
    """The roles a pod serves, from its component label and an s3 flag in its command or args: a
    subset of ROLES, in ROLES order. Unknown components serve none. An all-in-one pod serves S3
    only with the flag."""
    component = labels_of(pod).get(COMPONENT_LABEL, "")
    if "all-in-one" in component:
        found = set(ALL_IN_ONE_ROLES)
        if serves_s3_by_flag(pod):
            found.add("s3")
    else:
        found = set(COMPONENT_ROLES.get(component, ()))
        if "filer" in found and serves_s3_by_flag(pod):
            found.add("s3")
    return [r for r in ROLES if r in found]


def queryable(pod):
    """A pod the API server can proxy to: Running, has an IP, not being deleted."""
    status = pod.get("status") or {}
    return (
        status.get("phase") == "Running"
        and bool(status.get("podIP"))
        and not (pod.get("metadata") or {}).get("deletionTimestamp")
    )


def pods_with_role(pods, role):
    """Queryable pods serving `role`, by name."""
    return sorted(
        (p for p in pods if role in roles_of(p) and queryable(p)),
        key=lambda p: p["metadata"]["name"],
    )


def container_view(status, init):
    state = status.get("state") or {}
    kind = next(iter(state), None)
    detail = state.get(kind) or {} if kind else {}
    return {
        "name": status["name"],
        "init": init,
        "ready": bool(status.get("ready")),
        "restartCount": int(status.get("restartCount") or 0),
        "state": kind,
        "reason": detail.get("reason"),
    }


def pod_view(pod):
    meta, status = pod["metadata"], pod.get("status") or {}
    inits = [container_view(c, True) for c in status.get("initContainerStatuses") or []]
    mains = [container_view(c, False) for c in status.get("containerStatuses") or []]
    return {
        "name": meta["name"],
        "component": labels_of(pod).get(COMPONENT_LABEL),
        "roles": roles_of(pod),
        "phase": status.get("phase"),
        "ready": any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions") or []
        ),
        "podIP": status.get("podIP"),
        "deleting": bool(meta.get("deletionTimestamp")),
        "restarts": sum(c["restartCount"] for c in mains),
        "containers": (inits + mains)[:NESTED_LIMIT],
    }


def workload_view(obj, kind):
    """Desired against ready replicas of a StatefulSet or Deployment, and its images."""
    meta, spec, status = obj["metadata"], obj.get("spec") or {}, obj.get("status") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    desired = spec.get("replicas")
    return {
        "kind": kind,
        "name": meta["name"],
        "component": labels_of(obj).get(COMPONENT_LABEL),
        "desired": 1 if desired is None else int(desired),
        "ready": int(status.get("readyReplicas") or 0),
        "updated": int(status.get("updatedReplicas") or 0),
        "generationObserved": status.get("observedGeneration") == meta.get("generation"),
        "images": [c["image"] for c in containers if c.get("image")][:NESTED_LIMIT],
    }
