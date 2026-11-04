"""activity-failures: the cluster's failed operations in the Azure Activity Log over a window.

The activity log is read for the cluster's resource group (`az monitor activity-log list -g`,
status Failed) and the events that belong to the cluster, or to something under it such as a node
pool, are kept: az applies only one of its resource filters at a time, and the log files a node
pool's operations under the pool's own resource id, so a filter on the cluster's id alone would
miss them. Ids are compared without case (the log often spells them in capitals). The caller of
an operation is not selected: it is a person's name or address, and the question is what failed."""

import json
import re
from datetime import datetime

from .az import az_json
from .cluster import resolve
from .common import clip, listing, span, window
from .errors import RwError

SELECT = (
    "eventTimestamp", "level", "operationName", "properties", "resourceId", "status", "subStatus",
)  # fmt: skip


_STAMP = re.compile(r"^(?P<head>[^.Z+]+)(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d\d:\d\d)?$")


def moment(text):
    """The datetime of an RFC 3339 timestamp (Azure writes seven fractional digits, which Python
    does not read), or None."""
    found = _STAMP.match(text) if isinstance(text, str) else None
    if not found:
        return None
    frac = f".{found['frac'][:6].ljust(6, '0')}" if found["frac"] else ""
    zone = found["zone"] if found["zone"] not in (None, "Z") else "+00:00"
    try:
        return datetime.fromisoformat(f"{found['head']}{frac}{zone}")
    except ValueError:
        return None


def under(resource_id, cluster_id):
    """The resource the event is about: 'cluster' for the cluster itself, the rest of the id for
    something under it (agentPools/batch), None for anything else."""
    rid, cid = str(resource_id or ""), cluster_id
    if rid.lower() == cid.lower():
        return "cluster"
    if rid.lower().startswith(cid.lower() + "/"):
        return rid[len(cid) + 1 :]
    return None


def localized(value):
    """The `value` of az's {value, localizedValue} pair."""
    return (
        value.get("value")
        if isinstance(value, dict) and isinstance(value.get("value"), str)
        else None
    )


def reason(properties):
    """(error code, message) from an event's statusMessage, which holds the error as JSON when
    the operation failed ({"status":"Failed","error":{"code":..,"message":..}}) and plain text
    otherwise."""
    message = properties.get("statusMessage") if isinstance(properties, dict) else None
    if not isinstance(message, str) or not message:
        return None, ""
    try:
        body = json.loads(message)
    except ValueError:
        return None, message
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code, text = error.get("code"), error.get("message")
        return (code if isinstance(code, str) else None), (text if isinstance(text, str) else "")
    return None, message


def group_failures(events, cluster_id):
    """(groups, events for the cluster, events for other resources). A group is the events with the
    same operation, target, level, sub-status and error code."""
    groups, mine, other = {}, 0, 0
    for event in events:
        target = under(event.get("resourceId"), cluster_id)
        if target is None:
            other += 1
            continue
        mine += 1
        code, message = reason(event.get("properties"))
        label = (
            localized(event.get("operationName")) or "",
            target,
            event.get("level") if isinstance(event.get("level"), str) else None,
            localized(event.get("subStatus")),
            code,
        )
        when = event.get("eventTimestamp") if isinstance(event.get("eventTimestamp"), str) else None
        entry = groups.setdefault(label, {"count": 0, "stamps": [], "message": "", "at": None})
        entry["count"] += 1
        stamp = moment(when)
        if stamp is not None:
            entry["stamps"].append((stamp, when))
            if entry["at"] is None or stamp >= entry["at"]:
                entry["at"], entry["message"] = stamp, message  # the newest event's text
    out = []
    for (operation, target, level, sub_status, code), entry in groups.items():
        text, cut = clip(entry["message"])
        stamps = sorted(entry["stamps"])
        out.append(
            {
                "operation": operation,
                "target": target,
                "level": level,
                "subStatus": sub_status,
                "errorCode": code,
                "message": text,
                "truncated": cut,
                "count": entry["count"],
                "firstSeen": stamps[0][1] if stamps else None,
                "lastSeen": stamps[-1][1] if stamps else None,
            }
        )
    out.sort(key=lambda g: (-g["count"], g["operation"], g["target"], g["errorCode"] or ""))
    return out, mine, other


def read(ctx, window_input, max_items, scan_limit):
    seconds = window(window_input)
    subscription, group, name, cluster_id, _ = resolve(ctx)
    start, end = span(ctx.now(), seconds)
    reply = az_json(
        ctx,
        [
            "monitor", "activity-log", "list", "-g", group, "--status", "Failed",
            "--start-time", start, "--end-time", end,
            "--max-events", str(scan_limit + 1),  # one more than the limit: to see a cut
            "--select", *SELECT, "--subscription", subscription,
        ],
    )  # fmt: skip
    if not isinstance(reply, list) or not all(isinstance(e, dict) for e in reply):
        raise RwError("internal", "az monitor activity-log list returned no list")
    cut = len(reply) > scan_limit
    events = reply[:scan_limit]
    groups, mine, other = group_failures(events, cluster_id)
    return {
        "cluster": name,
        "windowSeconds": int(seconds),
        "start": start,
        "end": end,
        "scanLimit": scan_limit,
        "eventsScanned": len(events),
        # True: the group has more failed operations than scanLimit, so the cluster's list may be
        # incomplete (and an empty one proves nothing).
        "scanCut": cut,
        "eventsForCluster": mine,
        "eventsForOtherResources": other,
        "failures": listing(groups, max_items),
    }
