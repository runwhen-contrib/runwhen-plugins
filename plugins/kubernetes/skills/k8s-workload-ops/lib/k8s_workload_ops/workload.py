"""Pure functions behind the k8s-workload-ops tasks.

Everything here takes already-parsed Kubernetes JSON (or plain inputs) and
returns plain facts or a plan: no kubectl, no clock, no environment. Scripts
fetch, call these, run the mutating command, and return. Every guard that can
refuse a write lives here so a unit test can show it.
"""

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from k8s_workload_ops.errors import RwError

DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"
DAEMONSET = "DaemonSet"

TEXT_LIMIT = 1024
NESTED_LIMIT = 50
REASON_LIMIT = 256
RESTART_REASON_ANNOTATION = "runwhen.com/restart-reason"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

# ctx.resource.type -> Kind, and the plural path segment -> Kind
_BY_TYPE = {
    "kubernetes/deployment": DEPLOYMENT,
    "kubernetes/statefulset": STATEFULSET,
    "kubernetes/daemonset": DAEMONSET,
}
_BY_PLURAL = {"deployments": DEPLOYMENT, "statefulsets": STATEFULSET, "daemonsets": DAEMONSET}
# kubectl resource names
RESOURCE = {
    DEPLOYMENT: "deployments.apps",
    STATEFULSET: "statefulsets.apps",
    DAEMONSET: "daemonsets.apps",
}
# kubectl <kind>/<name> prefix
SHORT = {
    DEPLOYMENT: "deployment.apps",
    STATEFULSET: "statefulset.apps",
    DAEMONSET: "daemonset.apps",
}


# --------------------------------------------------------------- target ----


def resolve_kind(resource_type, resource_path=None):
    """Workload Kind from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path` (`.../deployments/web`). Anything else raises ValueError."""
    if resource_type in _BY_TYPE:
        return _BY_TYPE[resource_type]
    if not resource_type and resource_path:
        for segment in reversed(str(resource_path).strip("/").split("/")):
            if segment in _BY_PLURAL:
                return _BY_PLURAL[segment]
    raise ValueError(f"k8s-workload-ops does not apply to {resource_type or resource_path!r}")


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


# -------------------------------------------------------------- inputs ----

_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text, name="timeout"):
    """'90s', '2m', '1m30s', '500ms', '1h' -> seconds (float). Bad input raises
    RwError('invalid_input')."""
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s:
        raise RwError("invalid_input", f"{name} must be a duration such as 90s or 2m, got {text!r}")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


def kubectl_duration(seconds):
    """Seconds -> the whole-second form `kubectl --timeout` accepts (never the
    'd' unit, which Go durations do not know)."""
    return f"{max(1, math.ceil(seconds))}s"


def clean_reason(reason):
    """The restart reason, trimmed; None when absent or blank. Over 256
    characters, or containing control, line/paragraph-separator or bidi/format
    characters, raises RwError('invalid_input')."""
    if reason is None:
        return None
    text = str(reason).strip()
    if not text:
        return None
    if len(text) > REASON_LIMIT:
        raise RwError(
            "invalid_input", f"reason is {len(text)} characters; the limit is {REASON_LIMIT}"
        )
    # Cc control, Cf format (bidi marks and overrides U+200E/F, U+202A-E, U+2066-9, zero-width),
    # Zl/Zp line and paragraph separators (U+2028, U+2029)
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text):
        raise RwError(
            "invalid_input",
            "reason must not contain control, line-separator or bidirectional-format characters",
        )
    return text


# ------------------------------------------------------------- gitops ----

_GITOPS_PREFIXES = (
    ("flux", "kustomize.toolkit.fluxcd.io/"),
    ("flux", "helm.toolkit.fluxcd.io/"),
    ("flux", "fluxcd.io/"),
    ("argocd", "argocd.argoproj.io/"),
)
_GITOPS_MANAGED_BY = {"flux": "flux", "fluxcd": "flux", "argocd": "argocd", "argo-cd": "argocd"}


def gitops(workload):
    """Whether Flux or Argo CD labels or annotations are on the workload.

    A fact for the approver, never a refusal: GitOps will revert most of what
    these tasks change, and that is the approver's call to make. Only keys are
    reported (values are never read out).
    """
    meta = workload.get("metadata", {})
    signals, tool = [], None
    for where, mapping in (("label", meta.get("labels")), ("annotation", meta.get("annotations"))):
        for key, value in sorted((mapping or {}).items()):
            found = next((t for t, prefix in _GITOPS_PREFIXES if key.startswith(prefix)), None)
            if key == "app.kubernetes.io/managed-by":
                found = _GITOPS_MANAGED_BY.get(str(value).lower())
            if found:
                tool = tool or found
                signals.append({"kind": where, "key": key, "tool": found})
    return {"managed": bool(signals), "tool": tool, "signals": signals[:NESTED_LIMIT]}


def target_view(kind, workload):
    """The `target` output every task returns: what was acted on, and whether
    a GitOps controller manages it."""
    meta, spec = workload["metadata"], workload.get("spec", {})
    g = gitops(workload)
    return {
        "kind": kind,
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "paused": bool(spec.get("paused", False)) if kind == DEPLOYMENT else None,
        "updateStrategy": update_strategy(kind, workload),
        "gitopsManaged": g["managed"],
        "gitops": g,
    }


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
            raise ValueError(f"unknown selector operator {op!r}")
    if not terms:
        raise ValueError("workload has an empty selector")
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


def owned_revisions(workload, items):
    """ReplicaSets (Deployment) or ControllerRevisions (StatefulSet/DaemonSet)
    whose controller is this workload."""
    uid = workload["metadata"]["uid"]
    return [i for i in items if uid in _controller_uids(i)]


def owned_pods(kind, workload, replicasets, pods):
    """Pods that match the workload's selector AND hang off it through the
    ownerReferences chain (Deployment -> ReplicaSet -> Pod; StatefulSet or
    DaemonSet -> Pod). A pod with the right labels but another controller is
    not this workload's pod, and these tasks never touch it."""
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


# ------------------------------------------------------------ revisions ----


def images(pod_spec):
    """Every container image of a pod spec (at most 50); init containers
    (native sidecars included) carry init: true."""
    out = [
        {"container": c["name"], "image": c.get("image"), "init": False}
        for c in pod_spec.get("containers") or []
    ]
    out += [
        {"container": c["name"], "image": c.get("image"), "init": True}
        for c in pod_spec.get("initContainers") or []
    ]
    return out[:NESTED_LIMIT]


def revision_views(kind, items):
    """ReplicaSets or ControllerRevisions as revision views, newest revision
    first: {name, revision, created, images, replicas}. Items without a
    revision number are skipped."""
    out = []
    for item in items:
        meta = item["metadata"]
        if kind == DEPLOYMENT:
            number = (meta.get("annotations") or {}).get(REVISION_ANNOTATION)
            pod_spec = item.get("spec", {}).get("template", {}).get("spec", {})
            replicas = item.get("spec", {}).get("replicas")
        else:
            number = item.get("revision")
            pod_spec = (
                ((item.get("data") or {}).get("spec") or {}).get("template", {}).get("spec", {})
            )
            replicas = None
        if number is None:
            continue
        out.append(
            {
                "name": meta["name"],
                "revision": int(number),
                "created": meta.get("creationTimestamp"),
                "images": images(pod_spec),
                "replicas": replicas,
            }
        )
    out.sort(key=lambda v: v["revision"], reverse=True)
    return out


def current_revision(kind, workload, views):
    """The revision number the workload is on now, or None when it has no
    revision yet. Deployment: its own annotation. StatefulSet: the revision
    named by status.updateRevision. DaemonSet: the newest ControllerRevision."""
    if kind == DEPLOYMENT:
        number = (workload["metadata"].get("annotations") or {}).get(REVISION_ANNOTATION)
        return int(number) if number is not None else None
    if kind == STATEFULSET:
        name = workload.get("status", {}).get("updateRevision")
        for v in views:
            if v["name"] == name:
                return v["revision"]
    return views[0]["revision"] if views else None


def update_strategy(kind, workload):
    """The workload's update strategy type (RollingUpdate, Recreate, OnDelete)."""
    spec = workload.get("spec", {})
    strategy = spec.get("strategy") if kind == DEPLOYMENT else spec.get("updateStrategy")
    return (strategy or {}).get("type")


def template_images(workload):
    return images(workload.get("spec", {}).get("template", {}).get("spec", {}))


def revision_state(kind, workload, views):
    """{revision, images} of the workload as it runs now (images from its own
    pod template, never from a stale revision)."""
    return {
        "revision": current_revision(kind, workload, views),
        "images": template_images(workload),
    }


# ----------------------------------------------------------- guard: write ----


def check_restartable(kind, workload):
    """A paused Deployment cannot be restarted (kubectl refuses); say so
    before any write."""
    if kind == DEPLOYMENT and workload.get("spec", {}).get("paused"):
        raise RwError(
            "invalid_input", "the Deployment is paused; resume the rollout before restarting it"
        )


def restart_patch(restarted_at, reason):
    """The one merge patch a restart sends: the pod template's restartedAt
    annotation (what `kubectl rollout restart` sets) and the reason annotation
    on the workload, so there is no half-done state. No reason clears an older
    one (a null in a merge patch deletes the key, and is a no-op when absent)."""
    return {
        "metadata": {"annotations": {RESTART_REASON_ANNOTATION: reason}},
        "spec": {
            "template": {
                "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}
            }
        },
    }


def has_restart_reason(workload):
    return RESTART_REASON_ANNOTATION in (workload["metadata"].get("annotations") or {})


def classify_rollout_failure(stderr):
    """('reason', is_slow) for a failed `kubectl rollout status`.

    A rollout that is slow, stuck or on a strategy kubectl cannot watch is a
    fact about the rollout (completed: false), not a failure of the task;
    anything else is left to the caller to report as an error.
    """
    text = (stderr or "").lower()
    if "timed out" in text:
        return "timeout", True
    if "exceeded its progress deadline" in text:
        return "progress_deadline_exceeded", True
    if "only available for rollingupdate" in text:
        return "update_strategy_on_delete", True
    return None, False


# ------------------------------------------------------------- rollback ----


def rollback_plan(kind, workload, views, to_revision, history_limit=None):
    """What `kubectl rollout undo` would do: {from, to, changed, toRevision,
    available}. toRevision is the --to-revision argument (None = previous).

    Raises RwError('invalid_input') when there is nothing to roll back to, the
    asked-for revision does not exist, or the Deployment is paused.
    """
    if kind == DEPLOYMENT and workload.get("spec", {}).get("paused"):
        raise RwError(
            "invalid_input", "the Deployment is paused; resume the rollout before rolling it back"
        )
    if to_revision is not None and (
        isinstance(to_revision, bool) or not isinstance(to_revision, int) or to_revision < 1
    ):
        raise RwError(
            "invalid_input", f"toRevision must be a positive integer, got {to_revision!r}"
        )
    current = current_revision(kind, workload, views)
    numbers = [v["revision"] for v in views]
    if current is None:
        raise RwError(
            "invalid_input",
            f"{kind} {workload['metadata']['name']} has no revision to roll back from",
        )
    by_number = {v["revision"]: v for v in views}
    if to_revision is None:
        older = [n for n in numbers if n < current]
        if not older:
            limit = (
                f" (revisionHistoryLimit is {history_limit})" if history_limit is not None else ""
            )
            raise RwError(
                "invalid_input",
                f"no previous revision to roll back to{limit}; current is {current}",
            )
        target = max(older)
    elif to_revision not in by_number:
        raise RwError(
            "invalid_input",
            f"revision {to_revision} is not in the history; available: {sorted(numbers, reverse=True)[:NESTED_LIMIT]}",
        )
    else:
        target = to_revision
    return {
        "from": revision_state(kind, workload, views),
        "to": {"revision": target, "images": by_number[target]["images"]},
        "changed": target != current,
        "toRevision": target if to_revision is not None else None,
        "available": [
            {
                "revision": v["revision"],
                "created": v["created"],
                "images": v["images"][:NESTED_LIMIT],
                "current": v["revision"] == current,
            }
            for v in views[:NESTED_LIMIT]
        ],
    }


def undo_was_skipped(output):
    """kubectl prints 'skipped rollback (current template already matches
    revision N)' and changes nothing when the template is already that one."""
    return "skipped rollback" in (output or "").lower()


# ---------------------------------------------------------------- scale ----


def check_scale_input(kind, replicas, allow_scale_to_zero):
    """Refusals that need no cluster read: DaemonSet, bad replicas, scale to zero."""
    if kind == DAEMONSET:
        raise RwError(
            "invalid_input", "a DaemonSet has no replicas to scale; it runs one pod per node"
        )
    if replicas is None:
        raise RwError("invalid_input", "replicas is required")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
        raise RwError(
            "invalid_input", f"replicas must be an integer of 0 or more, got {replicas!r}"
        )
    if replicas == 0 and not allow_scale_to_zero:
        raise RwError(
            "invalid_input",
            "scaling to 0 stops the workload; refused unless the allowScaleToZero setting is true",
        )


def hpas_targeting(kind, name, hpas):
    """HorizontalPodAutoscalers whose scaleTargetRef is this workload."""
    out = []
    for h in hpas:
        ref = h.get("spec", {}).get("scaleTargetRef") or {}
        if ref.get("kind") == kind and ref.get("name") == name:
            spec, status = h["spec"], h.get("status", {})
            out.append(
                {
                    "name": h["metadata"]["name"],
                    "minReplicas": spec.get("minReplicas", 1),
                    "maxReplicas": spec.get("maxReplicas"),
                    "currentReplicas": status.get("currentReplicas"),
                    "desiredReplicas": status.get("desiredReplicas"),
                }
            )
    return out


def scale_plan(kind, workload, replicas, hpas, override_hpa):
    """{from, to, changed, readyBefore, hpas, overrodeHpa}. Refuses (invalid_input)
    when an autoscaler targets the workload and `override_hpa` is not true:
    the autoscaler would put the replica count back."""
    if hpas and not override_hpa:
        names = ", ".join(h["name"] for h in hpas)
        raise RwError(
            "invalid_input",
            f"HorizontalPodAutoscaler {names} targets this workload and would undo a manual scale; "
            "set overrideHpa to true to scale anyway",
        )
    current = workload.get("spec", {}).get("replicas", 1)
    return {
        "from": current,
        "to": replicas,
        "changed": current != replicas,
        "readyBefore": workload.get("status", {}).get("readyReplicas", 0),
        "hpas": None if hpas is None else hpas[:NESTED_LIMIT],
        "overrodeHpa": bool(hpas) and bool(override_hpa),
    }


# --------------------------------------------------------------- delete ----


def pod_view(pod):
    status = pod.get("status", {})
    ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
    return {
        "name": pod["metadata"]["name"],
        "phase": status.get("phase"),
        "ready": ready is not None and ready.get("status") == "True",
        "node": pod.get("spec", {}).get("nodeName"),
        "terminating": pod["metadata"].get("deletionTimestamp") is not None,
    }


def delete_plan(owned, pod_names):
    """The pods to delete: every owned pod, or the owned pods in `pod_names`.

    A name that is not one of the workload's own pods raises
    RwError('invalid_input') and nothing is deleted: a pod the workload does
    not own is never touched, and a partial delete is not silently accepted.
    An empty list raises too (omit podNames to mean all).
    """
    by_name = {p["metadata"]["name"]: p for p in owned}
    if pod_names is None:
        chosen = sorted(by_name)
    else:
        if isinstance(pod_names, str) or not isinstance(pod_names, (list, tuple)):
            raise RwError("invalid_input", "podNames must be a list of pod names")
        if not pod_names:
            raise RwError(
                "invalid_input", "podNames is empty; omit it to delete every pod of the workload"
            )
        if not all(isinstance(n, str) and n for n in pod_names):
            raise RwError("invalid_input", "podNames must be non-empty strings")
        unknown = sorted({n for n in pod_names if n not in by_name})
        if unknown:
            raise RwError(
                "invalid_input",
                f"not pods of this workload (or already gone): {unknown[:NESTED_LIMIT]}; nothing was deleted",
            )
        chosen = sorted(set(pod_names))
    return {
        "ownedCount": len(by_name),
        "pods": [pod_view(by_name[n]) for n in chosen],
        "deletesAll": bool(chosen) and len(chosen) == len(by_name),
    }


def parse_deleted(output):
    """Names from `kubectl delete -o name` output (`pod/x`)."""
    names = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("pod/"):
            names.append(line.split("/", 1)[1].split()[0])
    return names


# ------------------------------------------------------------ resources ----

FIELDS = {
    "cpuRequest": ("requests", "cpu"),
    "cpuLimit": ("limits", "cpu"),
    "memoryRequest": ("requests", "memory"),
    "memoryLimit": ("limits", "memory"),
}

_QUANTITY = re.compile(
    r"^\+?(\d+(?:\.\d*)?|\.\d+)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E|[eE][+-]?\d+)?$"
)
MAX_EXPONENT = 30  # 1e30 is far beyond any real CPU or memory value
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


def parse_quantity(text):
    """A Kubernetes quantity ('100m', '0.5', '64Mi', '1G', '1e3') -> Decimal.
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
            if abs(exponent) > MAX_EXPONENT:
                raise ValueError(f"exponent out of range in {text!r}")
            return Decimal(number) * Decimal(10) ** exponent
        return Decimal(number) * _SCALE[suffix]
    except (InvalidOperation, ArithmeticError):  # Overflow, Underflow, ...
        raise ValueError(f"not a quantity: {text!r}") from None


def _unit_suffix(text):
    match = _QUANTITY.match(str(text).strip())
    return (match.group(2) or "") if match else ""


def _container(workload, name):
    pod_spec = workload.get("spec", {}).get("template", {}).get("spec", {})
    for c in pod_spec.get("containers") or []:
        if c["name"] == name:
            return c, False
    for c in pod_spec.get("initContainers") or []:
        if c["name"] == name:
            return c, True
    return None, False


def choose_container(workload, container):
    """The container to change: the named one, else the only regular container.
    Several containers and no name raises RwError('invalid_input')."""
    pod_spec = workload.get("spec", {}).get("template", {}).get("spec", {})
    names = [c["name"] for c in pod_spec.get("containers") or []]
    if container:
        if _container(workload, container)[0] is None:
            known = names + [c["name"] for c in pod_spec.get("initContainers") or []]
            raise RwError(
                "invalid_input", f"no container {container!r} in the pod template; it has {known}"
            )
        return container
    if len(names) == 1:
        return names[0]
    raise RwError(
        "invalid_input",
        f"container is required: the pod template has {len(names)} containers {names}",
    )


def field_values(container_spec):
    """{cpuRequest, cpuLimit, memoryRequest, memoryLimit} -> quantity string or None."""
    res = container_spec.get("resources") or {}
    return {f: (res.get(section) or {}).get(key) for f, (section, key) in FIELDS.items()}


def check_resource_input(values):
    """Validate the five-input set before any cluster call: at least one field,
    each a positive quantity. Returns {field: str} of the fields to change."""
    wanted = {
        f: str(v).strip() for f, v in values.items() if v is not None and str(v).strip() != ""
    }
    if not wanted:
        raise RwError(
            "invalid_input", "give at least one of cpuRequest, cpuLimit, memoryRequest, memoryLimit"
        )
    for field, text in wanted.items():
        try:
            quantity = parse_quantity(text)
        except ValueError:
            raise RwError(
                "invalid_input", f"{field} is not a Kubernetes quantity: {text!r}"
            ) from None
        if quantity <= 0:
            # kubectl set resources treats a zero value as "remove it"
            raise RwError("invalid_input", f"{field} must be greater than zero, got {text!r}")
        suffix = _unit_suffix(text)
        if (
            field.startswith("memory")
            and quantity < 2**20
            and (
                suffix == ""
                or suffix[:1] in ("e", "E")
                and len(suffix) > 1
                or suffix in ("n", "u", "m")
            )
        ):
            raise RwError(
                "invalid_input",
                f"{field} {text!r} is under 1Mi with no byte unit (a bare number is bytes, "
                "m is milli): did you mean Mi?",
            )
    return wanted


def resource_plan(workload, container, wanted):
    """What `kubectl set resources` will change: {container, init, fields,
    requests, limits}. `fields` has all four fields with before/after.

    Refuses (invalid_input) when the resulting request would exceed the
    resulting limit for cpu or memory.
    """
    name = choose_container(workload, container)
    spec, init = _container(workload, name)
    before = field_values(spec)
    after = {f: wanted.get(f, before[f]) for f in FIELDS}
    for resource in ("cpu", "memory"):
        request, limit = after[f"{resource}Request"], after[f"{resource}Limit"]
        if (
            request is not None
            and limit is not None
            and parse_quantity(request) > parse_quantity(limit)
        ):
            raise RwError(
                "invalid_input",
                f"{resource} request {request} would exceed the {resource} limit {limit} on container {name}",
            )
    return {
        "container": name,
        "init": init,
        "fields": resource_fields(before, after, wanted),
        "requests": _kubectl_pairs(wanted, "requests"),
        "limits": _kubectl_pairs(wanted, "limits"),
    }


def _kubectl_pairs(wanted, section):
    return ",".join(
        f"{key}={wanted[f]}" for f, (s, key) in FIELDS.items() if s == section and f in wanted
    )


def _same(a, b):
    if a is None or b is None:
        return a is b
    return parse_quantity(a) == parse_quantity(b)


def resource_fields(before, after, wanted):
    """Per field: {field, before, after, requested, differs} (differs: after is not before)."""
    return [
        {
            "field": f,
            "before": before[f],
            "after": after[f],
            "requested": f in wanted,
            "differs": not _same(before[f], after[f]),
        }
        for f in FIELDS
    ]


def resources_after(workload, plan):
    """The plan's fields re-read from the workload kubectl returned, so the
    report shows what the API server holds, not what was asked for."""
    spec, _init = _container(workload, plan["container"])
    after = field_values(spec) if spec is not None else {f: None for f in FIELDS}
    before = {x["field"]: x["before"] for x in plan["fields"]}
    wanted = {x["field"] for x in plan["fields"] if x["requested"]}
    return resource_fields(before, after, wanted)
