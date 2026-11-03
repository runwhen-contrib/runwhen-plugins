"""Pure functions behind the nodeclaims task."""

from k8s_karpenter_health.common import (
    cap,
    condition_of,
    condition_views,
    is_set_true,
    nested,
    parse_time,
    seconds_since,
)

POOL_LABEL = "karpenter.sh/nodepool"
CAPACITY_LABEL = "karpenter.sh/capacity-type"
INSTANCE_LABEL = "node.kubernetes.io/instance-type"
ZONE_LABEL = "topology.kubernetes.io/zone"


def belongs_to(claim, pool):
    """Whether the NodeClaim is the pool's: it carries the pool label and, if it names a NodePool owner
    with a uid, that is this pool (a claim of an earlier pool of the same name is not)."""
    meta = claim.get("metadata") or {}
    if (meta.get("labels") or {}).get(POOL_LABEL) != pool["metadata"]["name"]:
        return False
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "NodePool" and ref.get("uid") and pool["metadata"].get("uid"):
            if ref["uid"] != pool["metadata"]["uid"]:
                return False
    return True


def phase_of(claim):
    """Where the NodeClaim is in its life, from its conditions.

    Deleting (deletionTimestamp set), Ready, else the first step that is not True:
    Launching (Launched), Registering (Registered), Initializing (Initialized); NotReady when
    all three are True but Ready is not.
    """
    if (claim.get("metadata") or {}).get("deletionTimestamp"):
        return "Deleting"
    if is_set_true(claim, "Ready"):
        return "Ready"
    for ctype, phase in (
        ("Launched", "Launching"),
        ("Registered", "Registering"),
        ("Initialized", "Initializing"),
    ):
        if not is_set_true(claim, ctype):
            return phase
    return "NotReady"


def _not_ready_seconds(claim, phase, created, now):
    """Seconds the claim has been not Ready, or None when it is Ready or Deleting.

    A claim that has not got through Launched, Registered and Initialized never was Ready: it is
    measured from creation. One that got through them and is not Ready (its node went NotReady) is
    measured from the Ready condition's lastTransitionTime, so a claim that served for days and
    failed a minute ago is not "stuck" for days; with no such time, from creation.
    """
    if phase in ("Ready", "Deleting"):
        return None
    since = created
    if phase == "NotReady":
        ready = condition_of(claim, "Ready")
        since = parse_time(ready and ready["lastTransitionTime"]) or created
    return seconds_since(since, now)


def claim_view(claim, now, stuck_minutes):
    """One NodeClaim: phase, age, whether it is stuck, its node and what it was launched as.

    stuck: not Ready for longer than `stuck_minutes` (see _not_ready_seconds), not being deleted. `capacityType`,
    `instanceType` and `zone` come from the labels Karpenter sets, null when absent.
    """
    meta = claim["metadata"]
    status = claim.get("status") or {}
    labels = meta.get("labels") or {}
    created = parse_time(meta.get("creationTimestamp"))
    deleted = parse_time(meta.get("deletionTimestamp"))
    age = seconds_since(created, now)
    phase = phase_of(claim)
    not_ready = _not_ready_seconds(claim, phase, created, now)
    ref = (claim.get("spec") or {}).get("nodeClassRef") or {}
    return {
        "name": meta["name"],
        "creationTimestamp": meta.get("creationTimestamp"),
        "ageSeconds": age,
        "phase": phase,
        "notReadySeconds": not_ready,
        "stuck": not_ready is not None and not_ready > float(stuck_minutes) * 60,
        "deleting": phase == "Deleting",
        "deletingSeconds": seconds_since(deleted, now),
        "launched": condition_of(claim, "Launched"),
        "registered": condition_of(claim, "Registered"),
        "initialized": condition_of(claim, "Initialized"),
        "ready": condition_of(claim, "Ready"),
        "drifted": is_set_true(claim, "Drifted"),
        "empty": is_set_true(claim, "Empty"),
        "consolidatable": is_set_true(claim, "Consolidatable"),
        "conditions": nested(condition_views(claim)),
        "nodeName": status.get("nodeName") or None,
        "providerID": status.get("providerID") or None,
        "capacityType": labels.get(CAPACITY_LABEL),
        "instanceType": labels.get(INSTANCE_LABEL),
        "zone": labels.get(ZONE_LABEL),
        "nodeClassRef": {
            "group": ref.get("group"),
            "kind": ref.get("kind"),
            "name": ref.get("name"),
        }
        if ref
        else None,
    }


def _order(view):
    # stuck first, then anything not Ready, then the rest; newest first within each
    created = parse_time(view["creationTimestamp"])
    return (
        0 if view["stuck"] else 1 if view["phase"] != "Ready" else 2,
        -(created.timestamp() if created else 0),
        view["name"],
    )


def nodeclaims_for(items, pool, now, stuck_minutes, max_items):
    """(views, counts) for the NodeClaims the pool owns; views are capped at max_items, counts are not."""
    views = sorted(
        (claim_view(c, now, stuck_minutes) for c in items if belongs_to(c, pool)), key=_order
    )
    by_phase, by_capacity = {}, {}
    for v in views:
        by_phase[v["phase"]] = by_phase.get(v["phase"], 0) + 1
        key = v["capacityType"] or "unknown"
        by_capacity[key] = by_capacity.get(key, 0) + 1
    counts = {
        "total": len(views),
        "ready": by_phase.get("Ready", 0),
        "notReady": sum(1 for v in views if v["phase"] not in ("Ready", "Deleting")),
        "stuck": sum(1 for v in views if v["stuck"]),
        "deleting": by_phase.get("Deleting", 0),
        "drifted": sum(1 for v in views if v["drifted"]),
        "byPhase": by_phase,
        "byCapacityType": by_capacity,
        "stuckMinutes": float(stuck_minutes),
    }
    return cap(views, max_items), counts
