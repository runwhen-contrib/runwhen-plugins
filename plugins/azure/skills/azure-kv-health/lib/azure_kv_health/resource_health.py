"""resource-health: Azure's own verdict on the vault (Microsoft.ResourceHealth)."""

from .az import az_json
from .common import clip
from .errors import RwError
from .vault import resolve

API_VERSION = "2022-10-01"
# a relative URL: az rest sends an ARM resource id to the active cloud's Resource Manager endpoint
URL = "{id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version={api}"


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
