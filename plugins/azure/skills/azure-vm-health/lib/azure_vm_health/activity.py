"""activity-errors: the Azure Activity Log entries of a virtual machine or scale set at level
Critical, Error or Warning over a window.

The log is read for the resource group (`az monitor activity-log list -g`, at most `scan_limit`
events) and the events that belong to the resource, or to something under it such as an extension,
are kept: az applies only one of its resource filters at a time, and the log files an extension's
operations under the extension's own resource id, so a filter on the VM's id alone would miss
them. Ids are compared without case (the log often spells the group in capitals). When the group
has more events than `scan_limit` the resource's list may be incomplete: `scanCut` says so. The
caller of an operation is never selected (az is asked for only the fields below, so the caller and
the claims are not fetched), and what is returned is redacted of identifiers and secrets."""

from .az import az_json
from .common import listing, span, window
from .errors import RwError
from .target import RESOURCE_ID, resolve
from .text import redact_who, safe_clip  # noqa: F401  (redact_who: part of the module's API)

LEVELS = ("Critical", "Error", "Warning")
SELECT = (
    "eventTimestamp", "level", "operationName", "properties", "resourceId", "status", "subStatus",
    "correlationId", "description",
)  # fmt: skip


def redact(text):
    """The message without the identifiers of who was refused or who acted (`client '...'`,
    `object id '...'`, `user '...'`, `principal '...'`, `Caller: ...`, e-mail addresses)."""
    return redact_who(text)


def _text(value):
    """The `value` of a LocalizableString (az prints `{"localizedValue", "value"}`), or a string."""
    if isinstance(value, dict):
        return value.get("value") or value.get("localizedValue") or None
    return value if isinstance(value, str) and value else None


def under(event_id, resource_id):
    """'resource' for an event about the resource itself, the rest of the id for something under
    it (`extensions/CustomScript`), None for anything else. Ids are compared without case."""
    rid, base = str(event_id or ""), resource_id
    if rid.lower() == base.lower():
        return "resource"
    if rid.lower().startswith(base.lower() + "/"):
        return rid[len(base) + 1 :]
    return None


def event_facts(event, target="resource"):
    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    message, cut = safe_clip(props.get("statusMessage") or event.get("description"), who=True)
    return {
        "time": event.get("eventTimestamp"),
        "level": event.get("level"),
        "target": target[:256],
        "operation": _text(event.get("operationName")),
        "status": _text(event.get("status")),
        "subStatus": _text(event.get("subStatus")),
        "message": message,
        "correlationId": event.get("correlationId") or None,
        "truncated": cut,
    }


def read(ctx, window_input, limit, scan_limit):
    seconds = window(window_input)
    kind, subscription, _, name, resource_id, _ = resolve(ctx)
    # the group as the resource's own id spells it: the log's filter on a group name need not be
    # case-insensitive, and the identity keeps the group lower case
    group = RESOURCE_ID[kind].match(resource_id)["group"]
    start, end = span(ctx.now(), seconds)
    reply = az_json(
        ctx,
        [
            "monitor", "activity-log", "list", "-g", group,
            "--start-time", start, "--end-time", end,
            "--max-events", str(scan_limit + 1),  # one more than the limit: to see a cut
            "--select", *SELECT, "--subscription", subscription,
        ],
    )  # fmt: skip
    if not isinstance(reply, list) or not all(isinstance(r, dict) for r in reply):
        raise RwError("internal", "az monitor activity-log list returned no list")
    cut = len(reply) > scan_limit
    scanned = reply[:scan_limit]
    mine = [(r, under(r.get("resourceId"), resource_id)) for r in scanned]
    mine = [(r, target) for r, target in mine if target is not None]
    events = [event_facts(r, target) for r, target in mine if r.get("level") in LEVELS]
    counts = {level: sum(1 for e in events if e["level"] == level) for level in LEVELS}
    # worst level first, then newest first: ISO timestamps with a time zone sort as text
    events.sort(key=lambda e: str(e["time"] or ""), reverse=True)
    events.sort(key=lambda e: LEVELS.index(e["level"]))
    return {
        "resource": name,
        "resourceType": kind,
        "windowSeconds": int(seconds),
        "scanLimit": scan_limit,
        "eventsScanned": len(scanned),
        # True: the group has more events than scanLimit, so this resource's list may be
        # incomplete (and an empty one proves nothing)
        "scanCut": cut,
        "eventsForResource": len(mine),
        "eventsForOtherResources": len(scanned) - len(mine),
        "counts": counts,
        "events": listing(events, limit),
    }
