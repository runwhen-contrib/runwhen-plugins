"""activity-errors: the Azure Activity Log entries of a virtual machine or scale set at level
Critical, Error or Warning over a window (`az monitor activity-log list --resource-id`).

The log is read for the resource id the lookup returned, at most READ_CAP events (the newest
first, as Azure lists them); when the cap is reached the counts are lower bounds and `readCapReached`
says so. Who did it (the caller and its claims) is not returned."""

import re

from .az import az_json
from .common import TEXT_LIMIT, clip, listing, span, window
from .errors import RwError
from .target import resolve

READ_CAP = 1000
LEVELS = ("Critical", "Error", "Warning")
# who the operation was refused for: a person's or principal's identifiers in the message
_WHO = re.compile(r"\b(client|object id)\s+'[^']*'", re.I)


def redact(text):
    """The message without the identifiers of who was refused (`client '...'`, `object id '...'`)."""
    return _WHO.sub(lambda m: f"{m.group(1)} '<redacted>'", text) if isinstance(text, str) else text


def _text(value):
    """The `value` of a LocalizableString (az prints `{"localizedValue", "value"}`), or a string."""
    if isinstance(value, dict):
        return value.get("value") or value.get("localizedValue") or None
    return value if isinstance(value, str) and value else None


def event_facts(event):
    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    message, cut = clip(redact(props.get("statusMessage") or event.get("description")), TEXT_LIMIT)
    return {
        "time": event.get("eventTimestamp"),
        "level": event.get("level"),
        "operation": _text(event.get("operationName")),
        "status": _text(event.get("status")),
        "subStatus": _text(event.get("subStatus")),
        "message": message,
        "correlationId": event.get("correlationId") or None,
        "truncated": cut,
    }


def read(ctx, window_input, limit):
    seconds = window(window_input)
    kind, subscription, group, name, resource_id, _ = resolve(ctx)
    start, end = span(ctx.now(), seconds)
    rows = az_json(
        ctx,
        [
            "monitor", "activity-log", "list",
            "--resource-id", resource_id,
            "--start-time", start,
            "--end-time", end,
            "--max-events", str(READ_CAP),
            "--subscription", subscription,
        ],
    )  # fmt: skip
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az monitor activity-log list returned no list")
    events = [event_facts(r) for r in rows if r.get("level") in LEVELS]
    counts = {level: sum(1 for e in events if e["level"] == level) for level in LEVELS}
    # worst level first, then newest first: ISO timestamps with a time zone sort as text
    events.sort(key=lambda e: str(e["time"] or ""), reverse=True)
    events.sort(key=lambda e: LEVELS.index(e["level"]))
    return {
        "resource": name,
        "resourceType": kind,
        "windowSeconds": int(seconds),
        "eventsRead": len(rows),
        "readCapReached": len(rows) >= READ_CAP,
        "counts": counts,
        "events": listing(events, limit),
    }
