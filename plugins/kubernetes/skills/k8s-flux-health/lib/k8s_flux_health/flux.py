"""Pure functions over Flux HelmRelease and Kustomization objects (parsed JSON in, outputs out)."""

import re
from datetime import datetime, timezone

from k8s_flux_health.errors import RwError

TEXT_LIMIT = 1024
NESTED_LIMIT = 50

# Flux kinds this skill targets. `resource` is fully qualified so that kubectl never
# resolves the name against another API group.
HELMRELEASE = "HelmRelease"
KUSTOMIZATION = "Kustomization"
KINDS = {
    HELMRELEASE: {
        "type": "kubernetes/helmrelease",
        "plural": "helmreleases",
        "resource": "helmreleases.helm.toolkit.fluxcd.io",
    },
    KUSTOMIZATION: {
        "type": "kubernetes/kustomization",
        "plural": "kustomizations",
        "resource": "kustomizations.kustomize.toolkit.fluxcd.io",
    },
}
_BY_TYPE = {v["type"]: k for k, v in KINDS.items()}
_BY_PLURAL = {v["plural"]: k for k, v in KINDS.items()}

REQUESTED_AT = "reconcile.fluxcd.io/requestedAt"
# A Flux object created from another Flux object carries that parent's name and namespace in these labels.
_OWNER_LABELS = (
    (KUSTOMIZATION, "kustomize.toolkit.fluxcd.io/name", "kustomize.toolkit.fluxcd.io/namespace"),
    (HELMRELEASE, "helm.toolkit.fluxcd.io/name", "helm.toolkit.fluxcd.io/namespace"),
)


# --------------------------------------------------------------- target ----


def resolve_kind(resource_type, resource_path=None):
    """Flux Kind from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path` (`.../helmreleases/web`). Anything else raises RwError('invalid_input')."""
    if resource_type in _BY_TYPE:
        return _BY_TYPE[resource_type]
    if not resource_type and resource_path:
        for segment in reversed(str(resource_path).strip("/").split("/")):
            if segment in _BY_PLURAL:
                return _BY_PLURAL[segment]
    raise RwError(
        "invalid_input",
        f"k8s-flux-health does not apply to {resource_type or resource_path!r}",
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


# ----------------------------------------------------------- ready-status ----


def _spec(obj):
    return obj.get("spec") or {}


def _status(obj):
    return obj.get("status") or {}


def condition_status(obj, ctype):
    """'True' / 'False' / 'Unknown' for a condition type, None when the object has no such condition."""
    for c in _status(obj).get("conditions") or []:
        if c.get("type") == ctype:
            return c.get("status")
    return None


def condition_views(obj):
    """Every condition, as Flux wrote it, capped at 50, messages cut at 1024 characters."""
    views = []
    for c in (_status(obj).get("conditions") or [])[:NESTED_LIMIT]:
        message, truncated = limit_text(c.get("message"))
        views.append(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": message,
                "truncated": truncated,
                "observedGeneration": c.get("observedGeneration"),
                "lastTransitionTime": c.get("lastTransitionTime"),
            }
        )
    return views


def state_view(kind, obj):
    """Readiness facts: the Ready, Reconciling and Stalled conditions with their reasons, the
    suspend flag and whether the controller has seen the latest spec. A condition that is absent is
    null (not 'False'): a Kustomization that is Stalled may carry no Reconciling condition."""
    meta, status = obj.get("metadata") or {}, _status(obj)
    generation, observed = meta.get("generation"), status.get("observedGeneration")
    conditions = condition_views(obj)
    by_type = {c["type"]: c for c in conditions}
    ready = by_type.get("Ready") or {}
    stalled = by_type.get("Stalled") or {}
    return {
        "kind": kind,
        "apiVersion": obj.get("apiVersion"),
        "suspended": bool(_spec(obj).get("suspend", False)),
        "generation": generation,
        "observedGeneration": observed,
        # null when either side is unknown; false while the controller has not yet seen the latest spec
        "generationObserved": None
        if generation is None or observed is None
        else observed == generation,
        "ready": ready.get("status"),
        "readyReason": ready.get("reason"),
        "readyLastTransitionTime": ready.get("lastTransitionTime"),
        "reconciling": (by_type.get("Reconciling") or {}).get("status"),
        "stalled": stalled.get("status"),
        "stalledReason": stalled.get("reason"),
        "conditions": conditions,
        "conditionCount": len(_status(obj).get("conditions") or []),
    }


def _snapshot_view(snap):
    return {
        "name": snap.get("name"),
        "namespace": snap.get("namespace"),
        "releaseVersion": snap.get("version"),
        "chartName": snap.get("chartName"),
        "chartVersion": snap.get("chartVersion"),
        "appVersion": snap.get("appVersion"),
        "status": snap.get("status"),
        "lastDeployed": (snap.get("lastDeployed") or None),
    }


def helm_applied(obj):
    """(revision, source) of what is deployed. v2beta1/v2beta2 HelmReleases carry
    status.lastAppliedRevision; v2 does not, so the chart version of the newest history entry
    that Helm reports as 'deployed' is used. (None, None) when nothing was ever deployed."""
    status = _status(obj)
    if status.get("lastAppliedRevision"):
        return status["lastAppliedRevision"], "status.lastAppliedRevision"
    for snap in status.get("history") or []:
        if snap.get("status") == "deployed" and snap.get("chartVersion"):
            return snap["chartVersion"], "status.history"
    return None, None


def helm_view(obj):
    """HelmRelease-only facts: the chart it installs from and the release history."""
    spec = _spec(obj)
    chart_spec = (spec.get("chart") or {}).get("spec") or {}
    ref = chart_spec.get("sourceRef") or spec.get("chartRef") or {}
    history = _status(obj).get("history") or []
    return {
        "chart": chart_spec.get("chart"),
        "chartVersion": chart_spec.get("version"),
        "source": {
            "kind": ref.get("kind"),
            "name": ref.get("name"),
            "namespace": ref.get("namespace") or (obj.get("metadata") or {}).get("namespace"),
        }
        if ref
        else None,
        "interval": spec.get("interval"),
        "history": [_snapshot_view(s) for s in history[:NESTED_LIMIT]],
        "historyCount": len(history),
        "failures": _status(obj).get("failures"),
        "installFailures": _status(obj).get("installFailures"),
        "upgradeFailures": _status(obj).get("upgradeFailures"),
    }


def kustomization_view(obj):
    """Kustomization-only facts: source, path and how many objects it applied."""
    spec = _spec(obj)
    ref = spec.get("sourceRef") or {}
    entries = (_status(obj).get("inventory") or {}).get("entries")
    return {
        "path": spec.get("path"),
        "source": {
            "kind": ref.get("kind"),
            "name": ref.get("name"),
            "namespace": ref.get("namespace") or (obj.get("metadata") or {}).get("namespace"),
        }
        if ref
        else None,
        "interval": spec.get("interval"),
        "prune": spec.get("prune"),
        "targetNamespace": spec.get("targetNamespace"),
        "inventoryCount": None if entries is None else len(entries),
    }


def revisions_view(kind, obj):
    """Applied versus attempted revision.

    revisionsDiffer is true when a revision was attempted and is not the one applied (a stuck
    upgrade). neverApplied is true when nothing was applied at all: the classic check compared
    two nulls as equal and called a release that never applied anything fine."""
    status = _status(obj)
    attempted = status.get("lastAttemptedRevision") or None
    if kind == HELMRELEASE:
        applied, source = helm_applied(obj)
    else:
        applied = status.get("lastAppliedRevision") or None
        source = "status.lastAppliedRevision" if applied else None
    return {
        "lastAppliedRevision": applied,
        "appliedSource": source,
        "lastAttemptedRevision": attempted,
        "revisionsDiffer": attempted is not None and attempted != applied,
        "neverApplied": applied is None,
        "lastHandledReconcileAt": status.get("lastHandledReconcileAt"),
        "helm": helm_view(obj) if kind == HELMRELEASE else None,
        "kustomization": kustomization_view(obj) if kind == KUSTOMIZATION else None,
    }


# --------------------------------------------------------- dependencies ----


def dependency_refs(obj):
    """spec.dependsOn as [(name, namespace)]; the namespace defaults to the object's own."""
    own = (obj.get("metadata") or {}).get("namespace")
    return [(d.get("name"), d.get("namespace") or own) for d in (_spec(obj).get("dependsOn") or [])]


def dependency_view(name, namespace, dep, *, read, note=None):
    """One dependency. `read` False: it was not looked at (`note` says why), so ready is null.
    dep is the fetched object, or None when it was read and does not exist."""
    view = {
        "name": name,
        "namespace": namespace,
        "read": read,
        "exists": None if not read else dep is not None,
        "ready": None,
        "readyReason": None,
        "generationObserved": None,
        "suspended": None,
        "note": note,
    }
    if read and dep is not None:
        state = state_view(None, dep)
        view.update(
            ready=state["ready"],
            readyReason=state["readyReason"],
            generationObserved=state["generationObserved"],
            suspended=state["suspended"],
        )
    if read and dep is None:
        view["note"] = "the object named in dependsOn does not exist"
    return view


# ------------------------------------------------------------ write tasks ----


def target_view(kind, obj):
    """The object acted on, and the Flux object that applies it (if any)."""
    meta = obj.get("metadata") or {}
    labels = meta.get("labels") or {}
    managed_by = None
    for owner_kind, name_label, ns_label in _OWNER_LABELS:
        if labels.get(name_label):
            managed_by = {
                "kind": owner_kind,
                "name": labels[name_label],
                "namespace": labels.get(ns_label),
            }
            break
    return {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "managedBy": managed_by,
    }


def request_timestamp(now=None):
    """The value for reconcile.fluxcd.io/requestedAt: RFC 3339 with microseconds, so two requests
    in the same second differ (Flux reports a request as handled by echoing the value)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def reconcile_annotation(obj):
    return ((obj.get("metadata") or {}).get("annotations") or {}).get(REQUESTED_AT)


def handled(obj, requested_at):
    """True once the controller reports having handled this request."""
    return _status(obj).get("lastHandledReconcileAt") == requested_at


def reconcile_after(kind, obj):
    """Where the object stands after the reconcile request: Ready and the two revisions."""
    state = state_view(kind, obj)
    rev = revisions_view(kind, obj)
    message = next(
        (c["message"] for c in state["conditions"] if c["type"] == "Ready"),
        None,
    )
    ready = next((c for c in state["conditions"] if c["type"] == "Ready"), None) or {}
    return {
        "ready": state["ready"],
        "readyReason": state["readyReason"],
        "readyMessage": message,
        "readyMessageTruncated": bool(ready.get("truncated")),
        "stalled": state["stalled"],
        "lastAppliedRevision": rev["lastAppliedRevision"],
        "lastAttemptedRevision": rev["lastAttemptedRevision"],
        "lastHandledReconcileAt": rev["lastHandledReconcileAt"],
    }


def suspend_patch(suspend):
    """Merge patch for spec.suspend. Resume sets false (what `flux resume` does)."""
    return {"spec": {"suspend": bool(suspend)}}
