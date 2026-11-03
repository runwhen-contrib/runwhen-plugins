"""Pure functions behind the nodepool-status task.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment.
"""

from k8s_karpenter_health.common import (
    condition_of,
    condition_views,
    is_true,
    nested,
    number,
    parse_quantity,
    percent,
)

# NodeClass types this skill reads: (group, kind) -> plural. Any other group or kind (a
# provider this skill does not know, such as KWOK) is reported, never looked up.
NODECLASS_TYPES = {
    ("karpenter.k8s.aws", "EC2NodeClass"): "ec2nodeclasses",
    ("karpenter.azure.com", "AKSNodeClass"): "aksnodeclasses",
    ("karpenter.k8s.gcp", "GCPNodeClass"): "gcpnodeclasses",
}
UNITS = {"cpu": "cores", "memory": "bytes", "ephemeral-storage": "bytes"}


def _template_spec(pool):
    return ((pool.get("spec") or {}).get("template") or {}).get("spec") or {}


def nodeclass_ref(pool):
    """{group, kind, name} of spec.template.spec.nodeClassRef, or None when the pool has none.

    v1beta1 pools carry `apiVersion` instead of `group`; the group is its part before the slash.
    """
    ref = _template_spec(pool).get("nodeClassRef")
    if not ref:
        return None
    group = ref.get("group")
    if group is None and ref.get("apiVersion"):
        group = str(ref["apiVersion"]).rsplit("/", 1)[0] if "/" in ref["apiVersion"] else ""
    return {"group": group, "kind": ref.get("kind"), "name": ref.get("name")}


def nodeclass_lookup(pool):
    """('<plural>.<group>', name) for the pool's NodeClass, or (None, None) when it is not looked up."""
    ref = nodeclass_ref(pool)
    if not ref or not ref["name"]:
        return None, None
    plural = NODECLASS_TYPES.get((ref["group"], ref["kind"]))
    return (f"{plural}.{ref['group']}", ref["name"]) if plural else (None, ref["name"])


def nodeclass_view(pool, obj, state):
    """Whether the pool's NodeClass exists and is Ready.

    state: 'ok' (obj read), 'not_found' (the object does not exist), 'absent' (the NodeClass
    type is not installed), 'denied' (could not be read), 'external' (a group or kind this
    skill does not look up). `exists` and `ready` are null when not known; `lookedUp` says
    whether an answer was obtained.
    """
    ref = nodeclass_ref(pool)
    ready = condition_of(obj, "Ready") if obj else None
    return {
        "ref": ref,
        "lookedUp": state in ("ok", "not_found", "absent"),
        "typeInstalled": {"ok": True, "not_found": True, "absent": False}.get(state),
        "exists": {"ok": True, "not_found": False, "absent": False}.get(state),
        "ready": is_true(ready),
        "readyCondition": ready,
    }


def budget_view(budget):
    """One disruption budget: nodes ('10%' or '5'), reasons, schedule and duration."""
    nodes = budget.get("nodes")
    zero = False
    if nodes is not None:
        try:
            zero = parse_quantity(str(nodes).rstrip("%")) == 0
        except ValueError:
            zero = False
    return {
        "nodes": None if nodes is None else str(nodes),
        "reasons": nested(budget.get("reasons") or []),
        "schedule": budget.get("schedule"),
        "duration": budget.get("duration"),
        "allowsNone": zero,
    }


def pool_view(pool):
    """Conditions, weight, disruption settings and NodeClass reference of a NodePool."""
    meta = pool["metadata"]
    spec = pool.get("spec") or {}
    disruption = spec.get("disruption") or {}
    template = _template_spec(pool)
    conditions = condition_views(pool)
    # v1beta1 kept expireAfter under disruption
    expire = template.get("expireAfter", disruption.get("expireAfter"))
    return {
        "name": meta["name"],
        "generation": meta.get("generation"),
        "creationTimestamp": meta.get("creationTimestamp"),
        "ready": condition_of(pool, "Ready"),
        "conditions": nested(conditions),
        "notTrueConditions": nested(c["type"] for c in conditions if c["status"] != "True"),
        "weight": spec.get("weight"),
        "consolidationPolicy": disruption.get("consolidationPolicy"),
        "consolidateAfter": disruption.get("consolidateAfter"),
        "expireAfter": None if expire is None else str(expire),
        "budgets": nested(budget_view(b) for b in disruption.get("budgets") or []),
        "budgetCount": len(disruption.get("budgets") or []),
        "nodeClassRef": nodeclass_ref(pool),
    }


def _q(text):
    """A quantity string -> number in base units, or None when absent or not a quantity."""
    try:
        q = parse_quantity(text)
    except ValueError:
        return None
    return None if q is None else number(q)


def limits_view(pool, threshold_percent):
    """spec.limits against status.resources, per resource name.

    Base units: cores for cpu, bytes for memory and ephemeral-storage, a count for the rest.
    `limit` is null for a resource that is reported in status but has no limit; `used` is null
    when the controller has not reported it. `percent` and `atOrAbove` (against
    `threshold_percent`) need both; `exhausted` is used >= limit.
    """
    limits = (pool.get("spec") or {}).get("limits") or {}
    used = (pool.get("status") or {}).get("resources") or {}
    items = []
    for name in sorted(set(limits) | set(used)):
        limit, now = _q(limits.get(name)), _q(used.get(name))
        pct = None if limit is None or now is None else percent(now, limit)
        items.append(
            {
                "resource": name,
                "unit": UNITS.get(name, "count"),
                "limit": limit,
                "used": now,
                "percent": pct,
                "atOrAbove": None if pct is None else pct >= float(threshold_percent),
                "exhausted": None if limit is None or now is None else now >= limit,
            }
        )
    return {
        "declared": bool(limits),
        "thresholdPercent": float(threshold_percent),
        "items": nested(items),
    }
