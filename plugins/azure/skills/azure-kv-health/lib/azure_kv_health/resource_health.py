"""resource-health: Azure's own verdict on the vault (Microsoft.ResourceHealth)."""

from .az import az_json
from .common import clip, resolve
from .errors import RwError

API_VERSION = "2022-10-01"
URL = "https://management.azure.com{id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version={api}"


def read(ctx):
    subscription, name, resource_id, _ = resolve(ctx)
    reply = az_json(
        ctx, ["rest", "--method", "get", "--url", URL.format(id=resource_id, api=API_VERSION)]
    )
    props = reply.get("properties") if isinstance(reply, dict) else None
    state = props.get("availabilityState") if isinstance(props, dict) else None
    if not isinstance(state, str) or not state:
        # a reply without a state is not "healthy": the platform did not say
        raise RwError("internal", "Resource Health returned no availability state")
    summary, cut_summary = clip(props.get("summary"))
    detail, cut_detail = clip(props.get("detailedStatus"))
    events = props.get("serviceImpactingEvents")
    return {
        "vault": name,
        "availabilityState": state,
        "healthy": state == "Available",
        "summary": summary,
        "detailedStatus": detail,
        "reasonType": props.get("reasonType") or None,
        "reasonChronicity": props.get("reasonChronicity") or None,
        "occurredTime": props.get("occurredTime"),
        "reportedTime": props.get("reportedTime"),
        "serviceImpactingEvents": len(events) if isinstance(events, list) else 0,
        "truncated": cut_summary or cut_detail,
    }
