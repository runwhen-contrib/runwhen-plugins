"""Pure functions behind the pending-pods task."""

from fractions import Fraction

from k8s_karpenter_health.common import (
    cap,
    fmt_time,
    number,
    parse_duration,
    parse_quantity,
    parse_time,
    seconds_since,
    trunc,
)

POOL_LABEL = "karpenter.sh/nodepool"
CONSTRAINT_COUNT = {
    "nodeSelector": "constrainedToPool",
    "nodeAffinity": "constrainedToPool",
    "nodeAffinityOther": "otherConstraint",
    "none": "unconstrained",
}
CONSTRAINT_RANK = {"nodeSelector": 0, "nodeAffinity": 0, "nodeAffinityOther": 1, "none": 2}


def unschedulable_condition(pod):
    """The pod's PodScheduled condition when it is False with reason Unschedulable, else None."""
    for cond in (pod.get("status") or {}).get("conditions") or []:
        if (
            cond.get("type") == "PodScheduled"
            and cond.get("status") == "False"
            and cond.get("reason") == "Unschedulable"
        ):
            return cond
    return None


def _expression_allows(expr, pool):
    """Whether one node selector requirement on the pool label lets a node of `pool` match."""
    op, values = expr.get("operator"), expr.get("values") or []
    if op == "In":
        return pool in values
    if op == "NotIn":
        return pool not in values
    if op == "Exists":
        return True
    if op == "DoesNotExist":
        return False
    return True  # Gt / Lt do not apply to this label


def _required_terms(pod):
    affinity = ((pod.get("spec") or {}).get("affinity") or {}).get("nodeAffinity") or {}
    block = affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    return block.get("nodeSelectorTerms") or []


def pool_constraint(pod, pool):
    """(could_be_served, constraint) for a pod and the NodePool named `pool`.

    constraint is 'nodeSelector' (spec.nodeSelector names a pool), 'nodeAffinity' (every required
    term has an `In` requirement on karpenter.sh/nodepool), 'nodeAffinityOther' (every required
    term has a requirement on the label but not all are `In`: NotIn, Exists, DoesNotExist; such a
    pod does not select the pool) or 'none' (no constraint on the pool label: any pool could serve
    it). could_be_served is false when the constraint excludes `pool`. Taints, tolerations, other
    labels and resource fit are not evaluated.
    """
    spec = pod.get("spec") or {}
    selector = (spec.get("nodeSelector") or {}).get(POOL_LABEL)
    terms = _required_terms(pod)
    keyed = [
        [e for e in t.get("matchExpressions") or [] if e.get("key") == POOL_LABEL] for t in terms
    ]
    # terms are ORed, their requirements ANDed
    affinity_ok = not terms or any(
        all(_expression_allows(e, pool) for e in exprs) for exprs in keyed
    )
    if selector is not None:
        return selector == pool and affinity_ok, "nodeSelector"
    if terms and all(keyed):
        selecting = all(any(e.get("operator") == "In" for e in exprs) for exprs in keyed)
        return affinity_ok, "nodeAffinity" if selecting else "nodeAffinityOther"
    return True, "none"


def is_daemonset_pod(pod):
    """Whether a DaemonSet owns the pod (it is bound to a node that exists, never to a new one)."""
    return any(r.get("kind") == "DaemonSet" for r in (pod["metadata"].get("ownerReferences") or []))


def is_pinned_to_node(pod):
    """Whether every required node selector term pins the pod to a node by name (matchFields
    metadata.name In [...]): Karpenter cannot make a different node for it."""
    terms = _required_terms(pod)
    return bool(terms) and all(
        any(
            f.get("key") == "metadata.name" and f.get("operator") == "In"
            for f in t.get("matchFields") or []
        )
        for t in terms
    )


def _amount(container, resource):
    return parse_quantity(((container.get("resources") or {}).get("requests") or {}).get(resource))


def _effective(pod, resource):
    """The pod's effective request of cpu or memory in base units (a Fraction), as the scheduler counts it.

    Pod-level `spec.resources` (1.34+), when it sets the value, is the total. Otherwise the larger
    of the app containers plus all native sidecars (init containers with restartPolicy Always) and
    the peak while the init containers run (each ordinary init container plus the sidecars started
    before it). Pod overhead is added either way.
    """
    spec = pod.get("spec") or {}
    zero = Fraction(0)
    pod_level = parse_quantity(
        (((spec.get("resources") or {}).get("requests")) or {}).get(resource)
    )
    if pod_level is not None:
        total = pod_level
    else:
        app = sum((_amount(c, resource) or zero for c in spec.get("containers") or []), zero)
        sidecars = peak = zero
        for c in spec.get("initContainers") or []:
            amount = _amount(c, resource) or zero
            if c.get("restartPolicy") == "Always":
                sidecars += amount
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + amount)
        total = max(app + sidecars, peak)
    return total + (parse_quantity((spec.get("overhead") or {}).get(resource)) or zero)


def pod_requests(pod):
    """The pod's effective cpu and memory requests: {cpuMillicores, memoryBytes}, null when it sets none."""
    cpu, memory = _effective(pod, "cpu"), _effective(pod, "memory")
    return {
        "cpuMillicores": number(cpu * 1000) if cpu else None,
        "memoryBytes": int(round(memory)) if memory else None,
    }


def pod_view(pod, cond, constraint, now):
    """One Unschedulable pod: where it is, how long, why the scheduler said so, what it asked for."""
    meta = pod["metadata"]
    message, cut = trunc(cond.get("message"))
    owner = next(
        (r for r in meta.get("ownerReferences") or [] if r.get("controller")),
        (meta.get("ownerReferences") or [{}])[0],
    )
    return {
        "namespace": meta.get("namespace"),
        "name": meta["name"],
        "uid": meta.get("uid"),
        "ageSeconds": seconds_since(parse_time(meta.get("creationTimestamp")), now),
        "unschedulableSeconds": seconds_since(parse_time(cond.get("lastTransitionTime")), now),
        "constraint": constraint,
        "message": message,
        "truncated": cut,
        "requests": pod_requests(pod),
        "owner": {"kind": owner.get("kind"), "name": owner.get("name")}
        if owner.get("kind")
        else None,
    }


def pending_pods_for(items, pool, now, max_items):
    """(views, counts) of the Pending pods the NodePool `pool` could serve.

    A pod counts when its PodScheduled condition is False with reason Unschedulable, no DaemonSet
    owns it and it is not pinned to a node by name (those are counted apart as `daemonSetPods` and
    `pinnedToNode`: a node Karpenter makes cannot take them), and its karpenter.sh/nodepool
    constraint (if any) allows `pool`. Pods that select the pool come first, then those with another
    kind of constraint, then the unconstrained; oldest unschedulable first within each. Views are
    capped at max_items; counts are not.
    """
    rows = []
    counts = {
        "pending": 0,
        "unschedulable": 0,
        "daemonSetPods": 0,
        "pinnedToNode": 0,
        "selectOtherPool": 0,
        "couldBeServed": 0,
        "constrainedToPool": 0,
        "otherConstraint": 0,
        "unconstrained": 0,
    }
    for pod in items:
        if (pod.get("status") or {}).get("phase") != "Pending":
            continue
        counts["pending"] += 1
        cond = unschedulable_condition(pod)
        if cond is None:
            continue
        counts["unschedulable"] += 1
        if is_daemonset_pod(pod):
            counts["daemonSetPods"] += 1
            continue
        if is_pinned_to_node(pod):
            counts["pinnedToNode"] += 1
            continue
        ok, constraint = pool_constraint(pod, pool)
        if not ok:
            counts["selectOtherPool"] += 1
            continue
        counts["couldBeServed"] += 1
        counts[CONSTRAINT_COUNT[constraint]] += 1
        rows.append(pod_view(pod, cond, constraint, now))
    rows.sort(
        key=lambda r: (
            CONSTRAINT_RANK[r["constraint"]],
            -(r["unschedulableSeconds"] or 0),
            r["namespace"] or "",
            r["name"],
        )
    )
    return cap(rows, max_items), counts


# --------------------------------------------------------------- events ----


def _event_times(ev):
    series = ev.get("series") or {}
    meta = ev.get("metadata") or {}
    last = (
        parse_time(series.get("lastObservedTime"))
        or parse_time(ev.get("lastTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(ev.get("firstTimestamp"))
        or parse_time(meta.get("creationTimestamp"))
    )
    first = (
        parse_time(ev.get("firstTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(meta.get("creationTimestamp"))
        or last
    )
    return first, last


def pod_events(events, pods, since, now, max_items):
    """Events about the given pods whose last occurrence is inside the window, grouped by pod and reason.

    `pods` are pod_view rows. An event is matched on involvedObject.uid, so an event about an
    earlier pod of the same name is not taken for this one. `count` sums the events' own
    counters, which are lifetime counts. Newest first, capped at max_items.
    """
    window = parse_duration(since)
    by_uid = {p["uid"]: p for p in pods if p.get("uid")}
    groups = {}
    for ev in events:
        obj = ev.get("involvedObject") or {}
        pod = by_uid.get(obj.get("uid"))
        if pod is None or obj.get("kind") != "Pod":
            continue
        first, last = _event_times(ev)
        if last is None or last < now - window:
            continue
        count = (ev.get("series") or {}).get("count") or ev.get("count") or 1
        key = (obj["uid"], ev.get("reason"))
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "namespace": pod["namespace"],
                "pod": pod["name"],
                "reason": ev.get("reason"),
                "type": ev.get("type"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "reportingComponent": ev.get("reportingComponent")
                or (ev.get("source") or {}).get("component")
                or None,
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last >= g["lastSeen"]:
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["pod"]), reverse=True)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = trunc(g["message"])
    return cap(rows, max_items)
