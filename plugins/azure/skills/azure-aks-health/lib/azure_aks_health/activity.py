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
from .cluster import CLUSTER_ID, resolve
from .common import clip, listing, span, window
from .errors import RwError

SELECT = (
    "eventTimestamp", "level", "operationName", "properties", "resourceId", "status", "subStatus",
)  # fmt: skip


_STAMP = re.compile(
    r"^(?P<head>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(?P<frac>\d+))?"
    r"(?P<zone>Z|[+-]\d\d:\d\d)?$"
)
ID_LIMIT = 256  # operation, target and the codes are names; a longer one is cut
MAX_DEPTH = 8  # error.details nesting that is followed
# who the operation was refused for: a person's or principal's identifiers in the message
_WHO = re.compile(r"\b(client|object id)\s+'[^']*'", re.I)


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


def redact(text):
    """The message without the identifiers of who was refused (`client '...'`, `object id '...'`)."""
    return _WHO.sub(lambda m: f"{m.group(1)} '<redacted>'", text)


def short(value):
    """A name cut to ID_LIMIT characters (None stays None)."""
    return value[:ID_LIMIT] if isinstance(value, str) else None


def _error(node):
    return node if isinstance(node, dict) else None


def _leaf(error, depth=0):
    """(code, message) of the deepest cause in an ARM error. ARM wraps the cause: the outer code is
    generic (DeploymentFailed, ResourceOperationFailure) and the first of `details[]` holds what
    went wrong, which may nest again, or be a message that is itself an error document. A level
    without a code or a message hands the nearest outer one down."""
    code = error.get("code") if isinstance(error.get("code"), str) else None
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    if message.lstrip().startswith("{") and depth < MAX_DEPTH:
        try:
            embedded = _error(json.loads(message))
        except ValueError:
            embedded = None
        embedded = _error(embedded.get("error")) if embedded else None
        if embedded:
            inner_code, inner_message = _leaf(embedded, depth + 1)
            return inner_code or code, inner_message or message
    details = error.get("details")
    if depth < MAX_DEPTH and isinstance(details, list):
        for detail in details:
            if _error(detail):
                inner_code, inner_message = _leaf(detail, depth + 1)
                return inner_code or code, inner_message or message
    return code, message


def reason(properties):
    """(outer code, leaf code, leaf message) from an event's statusMessage, which holds the error as
    JSON when the operation failed ({"status":"Failed","error":{"code":..,"message":..,
    "details":[..]}}) and plain text otherwise. The leaf is the deepest cause in error.details; the
    outer code is the one on the error itself. Both codes are None for plain text."""
    message = properties.get("statusMessage") if isinstance(properties, dict) else None
    if not isinstance(message, str) or not message:
        return None, None, ""
    try:
        body = json.loads(message)
    except ValueError:
        return None, None, message
    error = _error(body.get("error")) if isinstance(body, dict) else None
    if error:
        outer = error.get("code") if isinstance(error.get("code"), str) else None
        leaf_code, leaf_message = _leaf(error)
        return outer, leaf_code, leaf_message
    return None, None, message


def group_failures(events, cluster_id):
    """(groups, events for the cluster, events for other resources). A group is the events with the
    same operation, target, level, sub-status and (leaf, outer) error codes."""
    groups, mine, other = {}, 0, 0
    for event in events:
        target = under(event.get("resourceId"), cluster_id)
        if target is None:
            other += 1
            continue
        mine += 1
        outer, code, message = reason(event.get("properties"))
        label = (
            localized(event.get("operationName")) or "",
            target,
            event.get("level") if isinstance(event.get("level"), str) else None,
            localized(event.get("subStatus")),
            code,
            outer,
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
    for (operation, target, level, sub_status, code, outer), entry in groups.items():
        text, cut = clip(redact(entry["message"]))
        stamps = sorted(entry["stamps"])
        out.append(
            {
                "operation": short(operation),
                "target": short(target),
                "level": short(level),
                "subStatus": short(sub_status),
                "errorCode": short(code),
                "outerCode": short(outer),
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
    subscription, _, name, cluster_id, _ = resolve(ctx)
    # the group as the cluster's own id spells it: the log's filter on a group name need not be
    # case-insensitive, and the identity keeps the group lower case
    group = CLUSTER_ID.match(cluster_id)["group"]
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
