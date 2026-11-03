"""Pure functions on the CSI provider DaemonSet: its counts, its pods' containers and the Warning
events about it."""

import math

from k8s_vault_health.workload import (
    NESTED_MAX,
    cap,
    container_statuses,
    fmt_time,
    limit_text,
    parse_duration,
    parse_time,
    pod_ready,
    restart_total,
)


def _max_unavailable(ds):
    """The DaemonSet's maxUnavailable as a number of pods (a percentage of the desired count,
    rounded up; Kubernetes defaults to 1)."""
    desired = ds.get("status", {}).get("desiredNumberScheduled", 0)
    value = ((ds.get("spec", {}).get("updateStrategy") or {}).get("rollingUpdate") or {}).get(
        "maxUnavailable"
    )
    if value is None:
        return 1
    if isinstance(value, str) and value.endswith("%"):
        return math.ceil(int(value[:-1]) * desired / 100)
    return int(value)


def daemonset_status(ds):
    """Counts and rollout state of a DaemonSet. The API omits a count that is zero, so a missing
    numberAvailable or numberUnavailable is 0."""
    meta, status = ds["metadata"], ds.get("status", {})
    generation = meta.get("generation")
    observed = status.get("observedGeneration")
    unavailable = status.get("numberUnavailable", 0)
    max_unavailable = _max_unavailable(ds)
    return {
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "desired": status.get("desiredNumberScheduled", 0),
        "current": status.get("currentNumberScheduled", 0),
        "ready": status.get("numberReady", 0),
        "updated": status.get("updatedNumberScheduled", 0),
        "available": status.get("numberAvailable", 0),
        "unavailable": unavailable,
        "anyUnavailable": unavailable > 0,
        "misscheduled": status.get("numberMisscheduled", 0),
        "generation": generation,
        "observedGeneration": observed,
        "generationObserved": None if generation is None else observed == generation,
        "updateStrategy": (ds.get("spec", {}).get("updateStrategy") or {}).get("type"),
        "maxUnavailable": max_unavailable,
        "unavailableExceedsMax": unavailable > max_unavailable,
    }


def _state(state):
    for name in ("running", "waiting", "terminated"):
        if name in (state or {}):
            return name, state[name]
    return None, {}


def pod_views(daemonset, pods):
    """Per pod of the DaemonSet: readiness, node, and every container (init ones flagged) with its
    restart count, state and the way its last run ended. Not-ready pods first, then most restarts."""
    out = []
    for pod in pods:
        containers = []
        for is_init, s in container_statuses(pod):
            name, state = _state(s.get("state"))
            last = (s.get("lastState") or {}).get("terminated") or {}
            containers.append(
                {
                    "name": s["name"],
                    "init": is_init,
                    "ready": bool(s.get("ready", False)),
                    "restartCount": s.get("restartCount", 0),
                    "state": name,
                    "reason": state.get("reason") if name in ("waiting", "terminated") else None,
                    "lastTerminated": (
                        {
                            "reason": last.get("reason"),
                            "exitCode": last.get("exitCode"),
                            "finishedAt": last.get("finishedAt"),
                        }
                        if last
                        else None
                    ),
                }
            )
        out.append(
            {
                "daemonset": daemonset,
                "pod": pod["metadata"]["name"],
                "phase": pod.get("status", {}).get("phase"),
                "ready": pod_ready(pod),
                "node": pod.get("spec", {}).get("nodeName"),
                "restarts": restart_total(pod),
                "containers": containers[:NESTED_MAX],
                "containersTruncated": len(containers) > NESTED_MAX,
            }
        )
    out.sort(key=lambda p: (p["ready"], -p["restarts"], p["pod"]))
    return out


def _event_times(ev):
    series = ev.get("series") or {}
    meta = ev.get("metadata", {})
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


def warning_events(events, daemonsets, pods, since, now, max_items):
    """Warning events about the DaemonSets or their current pods whose last occurrence falls in the
    window, grouped by object and reason, newest first. Objects are matched by uid (a pod that was
    replaced under the same name has another uid, and its old events are not this pod's); `count`
    is the sum of the events' own lifetime counters."""
    window = parse_duration(since)
    uids = {d["metadata"]["uid"]: d["metadata"]["name"] for d in daemonsets}
    uids.update({p["metadata"]["uid"]: p["metadata"]["name"] for p in pods})
    groups = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        obj = ev.get("involvedObject") or {}
        if obj.get("uid") not in uids:
            continue
        first, last = _event_times(ev)
        if last is None or last < now - window:
            continue
        series = ev.get("series") or {}
        count = series.get("count") or ev.get("count") or 1
        key = (obj.get("kind"), obj.get("name"), ev.get("reason"))
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "kind": obj.get("kind"),
                "name": obj.get("name"),
                "reason": ev.get("reason"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last >= g["lastSeen"]:
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["name"]), reverse=True)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = limit_text(g["message"])
    return cap(rows, max_items)
