"""Pure functions behind node-conditions and node-restarts.

They take parsed Node and Event JSON and return plain facts.
"""

from k8s_node_health.common import (
    cap,
    fmt_time,
    nested,
    parse_duration,
    parse_time,
    seconds_since,
    trunc,
)

# Conditions reported when their status is not "False" (True or Unknown).
PROBLEM_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

# Node event reasons that mean a node went away, came back or (re)started.
# "Starting" counts only from the kubelet: kube-proxy also emits it.
RESTART_REASONS = ("NodeNotReady", "NodeReady", "Rebooted", "RegisteredNode", "Starting")
KUBELET = "kubelet"


def _condition(node, ctype):
    for c in (node.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def _condition_view(c):
    message, cut = trunc(c.get("message"))
    return {
        "type": c.get("type"),
        "status": c.get("status"),
        "reason": c.get("reason"),
        "message": message,
        "truncated": cut,
        "lastTransition": c.get("lastTransitionTime"),
    }


# ------------------------------------------------------ node-conditions ----


def node_view(node, now):
    """Ready condition, pressure conditions, cordon, taints, kubelet version and age of one node."""
    meta = node["metadata"]
    spec = node.get("spec") or {}
    ready = _condition(node, "Ready")
    ready_view = _condition_view(ready) if ready else None
    created = parse_time(meta.get("creationTimestamp"))
    conditions = [
        _condition_view(c)
        for c in (node.get("status") or {}).get("conditions") or []
        if c.get("type") in PROBLEM_CONDITIONS and c.get("status") != "False"
    ]
    return {
        "name": meta["name"],
        "ready": {
            "status": ready_view["status"] if ready_view else None,
            "reason": ready_view["reason"] if ready_view else None,
            "message": ready_view["message"] if ready_view else None,
            "truncated": ready_view["truncated"] if ready_view else False,
            "lastTransition": ready_view["lastTransition"] if ready_view else None,
        },
        "conditions": nested(conditions),
        "unschedulable": bool(spec.get("unschedulable")),
        "taints": nested(
            {"key": t.get("key"), "effect": t.get("effect")} for t in spec.get("taints") or []
        ),
        "kubeletVersion": ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion"),
        "created": meta.get("creationTimestamp"),
        "ageSeconds": seconds_since(created, now),
    }


def node_conditions(nodes, now, max_items):
    """(views capped at max_items with nodes that are not Ready first, counts over every node)."""
    views = sorted(
        (node_view(n, now) for n in nodes),
        key=lambda v: (v["ready"]["status"] == "True", v["name"]),
    )
    counts = {
        "total": len(views),
        "ready": sum(v["ready"]["status"] == "True" for v in views),
        "notReady": sum(v["ready"]["status"] != "True" for v in views),
        "unschedulable": sum(v["unschedulable"] for v in views),
        "withConditions": sum(bool(v["conditions"]) for v in views),
    }
    return cap(views, max_items), counts


# -------------------------------------------------------- node-restarts ----


def ready_transitions(nodes, now, since):
    """Nodes whose Ready condition changed state within `since`, most recent first."""
    cutoff = now - parse_duration(since)
    rows = []
    for node in nodes:
        ready = _condition(node, "Ready")
        changed = parse_time(ready.get("lastTransitionTime")) if ready else None
        if changed and changed >= cutoff:
            rows.append(
                {
                    "node": node["metadata"]["name"],
                    "status": ready.get("status"),
                    "reason": ready.get("reason"),
                    "lastTransition": fmt_time(changed),
                    "secondsAgo": seconds_since(changed, now),
                }
            )
    return sorted(rows, key=lambda r: (r["secondsAgo"], r["node"]))


def event_time(event):
    """When an event was last seen: series, lastTimestamp, eventTime or creation, whichever is set.

    Events written through events.k8s.io carry only eventTime/series, which
    the classic awk date comparison on lastTimestamp dropped.
    """
    series = event.get("series") or {}
    for value in (
        series.get("lastObservedTime"),
        event.get("lastTimestamp"),
        event.get("eventTime"),
        (event.get("metadata") or {}).get("creationTimestamp"),
    ):
        if value:
            return parse_time(value)
    return None


def _first_time(event):
    for value in (
        event.get("firstTimestamp"),
        event.get("eventTime"),
        (event.get("metadata") or {}).get("creationTimestamp"),
    ):
        if value:
            return parse_time(value)
    return event_time(event)


def _component(event):
    return (event.get("source") or {}).get("component") or event.get("reportingComponent")


def _count(event):
    return (event.get("series") or {}).get("count") or event.get("count") or 1


def node_events(events, now, since, max_items):
    """Node restart events within `since`, grouped per node and reason, nodes seen last first.

    `count` adds the events' own counters (an event object repeated N times
    says N); it is not a count of events inside the window.
    """
    cutoff = now - parse_duration(since)
    nodes = {}
    for e in events:
        inv = e.get("involvedObject") or {}
        reason = e.get("reason")
        if inv.get("kind") != "Node" or reason not in RESTART_REASONS:
            continue
        if reason == "Starting" and _component(e) != KUBELET:
            continue
        last = event_time(e)
        if last is None or last < cutoff:
            continue
        group = nodes.setdefault(inv.get("name"), {}).setdefault(reason, [])
        group.append((last, e))
    rows = []
    for name, reasons in nodes.items():
        entries = []
        for reason, items in reasons.items():
            items.sort(key=lambda t: t[0])
            last, newest = items[-1]
            message, cut = trunc(newest.get("message"))
            entries.append(
                {
                    "reason": reason,
                    "type": newest.get("type"),
                    "count": sum(_count(e) for _t, e in items),
                    "firstSeen": fmt_time(min(_first_time(e) for _t, e in items)),
                    "lastSeen": fmt_time(last),
                    "message": message,
                    "reportingComponent": _component(newest),
                    "truncated": cut,
                }
            )
        entries.sort(key=lambda r: (r["lastSeen"], r["reason"]), reverse=True)
        rows.append(
            {
                "node": name,
                "total": sum(r["count"] for r in entries),
                "lastSeen": entries[0]["lastSeen"],
                "reasons": nested(entries),
            }
        )
    rows.sort(key=lambda r: (r["lastSeen"], r["node"]), reverse=True)
    return cap(rows, max_items)
