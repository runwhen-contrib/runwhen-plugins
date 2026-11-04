"""resource-health: Azure's own verdict on the web app or the plan (Microsoft.ResourceHealth)."""

from .arm import API_HEALTH, get
from .common import clip
from .errors import RwError
from .plan import read_plan
from .site import read_site
from .target import PLAN, own_id

URL = "{id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current"


def read(ctx, t):
    # the object first: a target that is gone is not_found, not an Unknown health state
    if t.type == PLAN:
        read_plan(ctx, t)
    else:
        read_site(ctx, t)
    reply = get(ctx, URL.format(id=own_id(t)), API_HEALTH)
    props = reply.get("properties")
    state = props.get("availabilityState") if isinstance(props, dict) else None
    if not isinstance(state, str) or not state:
        # a reply without a state is not "healthy": the platform did not say
        raise RwError("internal", "Resource Health returned no availability state")
    summary, cut_summary = clip(props.get("summary"))
    detail, cut_detail = clip(props.get("detailedStatus"))
    events = props.get("serviceImpactingEvents")
    return {
        "resource": t.name,
        "resourceType": "app-service-plan" if t.type == PLAN else "web-app",
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
