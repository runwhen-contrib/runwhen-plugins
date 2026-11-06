"""Warning events in a namespace, grouped by involved object and reason."""

from k8s_namespace_health.common import cap, fmt_time, parse_time, trunc, within
from k8s_namespace_health.owners import index_objects, resolve_owner

# Involved kinds whose owning workload can be looked up from the namespace listing.
OWNED_KINDS = ("Pod", "ReplicaSet", "Job")


def _event_times(ev):
    series = ev.get("series") or {}
    last = (
        parse_time(series.get("lastObservedTime"))
        or parse_time(ev.get("lastTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(ev.get("firstTimestamp"))
        or parse_time(ev.get("metadata", {}).get("creationTimestamp"))
    )
    first = parse_time(ev.get("firstTimestamp")) or parse_time(ev.get("eventTime")) or last
    return first, last


def _lookup(index, kind, name, uid):
    """The live object an event is about, matched on uid when the event carries
    one: a pod recreated under the same name (a StatefulSet pod) is a different
    object, and events about the old one must not be attributed to the new."""
    obj = index.get((kind, name))
    if obj is not None and uid and obj["metadata"].get("uid") != uid:
        return None
    return obj


def warning_events(events, pods, replicasets, jobs, since, now, max_items):
    """Warning events last seen within `since`, grouped by (kind, name, uid, reason).

    `count` sums the events' own occurrence counters, which are lifetime counts:
    an event last seen in the window may have started before it. For a Pod,
    ReplicaSet or Job the `owner` is the top-level workload (Deployment,
    StatefulSet, DaemonSet, Job, CronJob) and `objectExists` says whether that
    very object (matched on uid) is still in the namespace; an event about an
    object that is gone is kept, with owner null. `pods`, `replicasets` and
    `jobs` may be None when they could not be listed: `objectExists` is then
    null for that kind.
    """
    known = {"Pod": pods, "ReplicaSet": replicasets, "Job": jobs}
    index = index_objects(**{k: v for k, v in known.items() if v is not None})
    groups = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        obj = ev.get("involvedObject") or {}
        first, last = _event_times(ev)
        if not within(last, since, now):
            continue
        series = ev.get("series") or {}
        count = series.get("count") or ev.get("count") or 1
        key = (obj.get("kind"), obj.get("name"), obj.get("uid"), ev.get("reason"))
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "kind": obj.get("kind"),
                "name": obj.get("name"),
                "uid": obj.get("uid"),
                "reason": ev.get("reason"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "reportingComponent": ev.get("reportingComponent")
                or (ev.get("source") or {}).get("component"),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last and (g["lastSeen"] is None or last >= g["lastSeen"]):
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["name"] or ""), reverse=True)
    rows = cap(rows, max_items)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = trunc(g["message"])
        if g["kind"] in OWNED_KINDS and known[g["kind"]] is not None:
            target = _lookup(index, g["kind"], g["name"], g["uid"])
            g["objectExists"] = target is not None
            g["owner"] = resolve_owner(target, index) if target is not None else None
        else:
            g["objectExists"] = None
            g["owner"] = None
    return rows
