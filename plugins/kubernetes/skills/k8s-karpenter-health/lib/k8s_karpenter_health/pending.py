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


def pool_constraint(pod, pool):
    """(could_be_served, constraint) for a pod and the NodePool named `pool`.

    constraint is 'nodeSelector' (spec.nodeSelector names a pool), 'nodeAffinity' (every required
    term has a requirement on karpenter.sh/nodepool) or 'none' (no constraint on the pool label:
    any pool could serve it). could_be_served is false when the constraint excludes `pool`.
    Taints, tolerations, other labels and resource fit are not evaluated.
    """
    spec = pod.get("spec") or {}
    selector = (spec.get("nodeSelector") or {}).get(POOL_LABEL)
    terms = (
        ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution"
        )
        or {}
    ).get("nodeSelectorTerms") or []
    keyed = [
        [e for e in t.get("matchExpressions") or [] if e.get("key") == POOL_LABEL] for t in terms
    ]
    # terms are ORed, their requirements ANDed
    affinity_ok = not terms or any(
        all(_expression_allows(e, pool) for e in exprs) for exprs in keyed
    )
    constrained_by_affinity = bool(terms) and all(keyed)
    if selector is not None:
        return selector == pool and affinity_ok, "nodeSelector"
    if constrained_by_affinity:
        return affinity_ok, "nodeAffinity"
    return True, "none"


def _sum_requests(containers):
    total = {"cpu": Fraction(0), "memory": Fraction(0)}
    for c in containers:
        req = (c.get("resources") or {}).get("requests") or {}
        for key in total:
            q = parse_quantity(req.get(key))
            if q is not None:
                total[key] += q
    return total


def pod_requests(pod):
    """The pod's effective cpu and memory requests: {cpuMillicores, memoryBytes}, null when it sets none.

    App containers plus native sidecars (restartable init containers) or the largest ordinary init
    container, whichever is bigger, plus pod overhead; pod-level spec.resources replaces the sum.
    """
    spec = pod.get("spec") or {}
    pod_level = (spec.get("resources") or {}).get("requests") or {}
    sidecars = [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"]
    inits = [c for c in spec.get("initContainers") or [] if c.get("restartPolicy") != "Always"]
    steady = _sum_requests([*(spec.get("containers") or []), *sidecars])
    peak = {k: Fraction(0) for k in steady}
    for c in inits:
        one = _sum_requests([c])
        for k in peak:
            peak[k] = max(peak[k], one[k])
    overhead = spec.get("overhead") or {}
    out = {}
    for key in ("cpu", "memory"):
        if key in pod_level:
            value = parse_quantity(pod_level[key])
        else:
            value = max(steady[key], peak[key])
            if overhead.get(key) is not None:
                value += parse_quantity(overhead[key])
        out[key] = value
    return {
        "cpuMillicores": number(out["cpu"] * 1000) if out["cpu"] else None,
        "memoryBytes": int(round(out["memory"])) if out["memory"] else None,
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

    A pod counts when its PodScheduled condition is False with reason Unschedulable and its
    karpenter.sh/nodepool constraint (if any) allows `pool`. Pods that select the pool come first,
    then the rest, oldest unschedulable first. Views are capped at max_items; counts are not.
    """
    rows, counts = (
        [],
        {
            "pending": 0,
            "unschedulable": 0,
            "couldBeServed": 0,
            "constrainedToPool": 0,
            "unconstrained": 0,
            "selectOtherPool": 0,
        },
    )
    for pod in items:
        if (pod.get("status") or {}).get("phase") != "Pending":
            continue
        counts["pending"] += 1
        cond = unschedulable_condition(pod)
        if cond is None:
            continue
        counts["unschedulable"] += 1
        ok, constraint = pool_constraint(pod, pool)
        if not ok:
            counts["selectOtherPool"] += 1
            continue
        counts["couldBeServed"] += 1
        counts["unconstrained" if constraint == "none" else "constrainedToPool"] += 1
        rows.append(pod_view(pod, cond, constraint, now))
    rows.sort(
        key=lambda r: (
            r["constraint"] == "none",
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
