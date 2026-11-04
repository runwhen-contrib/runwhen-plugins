"""The App Service plan: state, SKU and instance count, and autoscale.

A plan is reached from a plan target, or from a web app target through the app's `serverFarmId`: the
plan an app runs on is part of the app (D47), and it may sit in another resource group than the app."""

from types import SimpleNamespace

from .arm import API_AUTOSCALE, get, get_list
from .errors import RwError
from .site import props_of, read_site
from .target import PLAN, parse_id, plan_id


def plan_target(ctx, t):
    """(the plan as a target, the site object or None): the target itself for a plan, the app's plan
    for a web app. An app that does not run on a plan has none to read: invalid_input."""
    if t.type == PLAN:
        return t, None
    site = read_site(ctx, t)
    farm = props_of(site, "web app").get("serverFarmId")
    if not farm:
        raise RwError(
            "invalid_input",
            f"{t.name} does not run on an App Service plan, so it has no plan to read",
        )
    sub, group, name = parse_id(farm, "serverfarms")
    return SimpleNamespace(type=PLAN, subscription=sub, group=group, name=name), site


def read_plan(ctx, t):
    """The plan object (a not_found propagates). Checked to be the plan asked for."""
    doc = get(ctx, plan_id(t.subscription, t.group, t.name))
    props_of(doc, "plan")
    sub, group, name = parse_id(doc.get("id"), "serverfarms")
    if name.lower() != t.name.lower() or group.lower() != t.group.lower() or sub != t.subscription:
        raise RwError("internal", f"Azure answered for another plan than {t.name}")
    return doc


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(value):
    return value if isinstance(value, bool) else None


def plan_state(doc):
    """The `planState` output: SKU, instance count and status of the plan."""
    props = props_of(doc, "plan")
    sku = doc.get("sku") if isinstance(doc.get("sku"), dict) else {}
    status = props.get("status")
    if not isinstance(status, str) or not status:
        raise RwError("internal", "Azure returned a plan without a status")
    workers = _int(props.get("numberOfWorkers"))
    capacity = _int(sku.get("capacity"))
    return {
        "plan": str(doc.get("name")),
        "kind": str(doc.get("kind") or "") or None,
        "isLinux": bool(props.get("reserved")),
        "location": str(doc.get("location") or "") or None,
        "planStatus": status,
        "ready": status == "Ready",
        "provisioningState": props.get("provisioningState") or None,
        "sku": {
            "name": sku.get("name") or None,
            "tier": sku.get("tier") or None,
            "size": sku.get("size") or None,
            "family": sku.get("family") or None,
            "capacity": capacity,
        },
        "instanceCount": workers if workers is not None else capacity,
        "maximumNumberOfWorkers": _int(props.get("maximumNumberOfWorkers")),
        "numberOfSites": _int(props.get("numberOfSites")),
        "elasticScaleEnabled": _flag(props.get("elasticScaleEnabled")),
        "maximumElasticWorkerCount": _int(props.get("maximumElasticWorkerCount")),
        "perSiteScaling": _flag(props.get("perSiteScaling")),
        "zoneRedundant": _flag(props.get("zoneRedundant")),
        "isSpot": _flag(props.get("isSpot")),
        "inAppServiceEnvironment": bool(props.get("hostingEnvironmentProfile")),
    }


def read_autoscale(ctx, t):
    """The autoscale settings of the plan's subscription and whether the whole list was read.

    An autoscale setting is a resource of its own that names the plan as its target, and it can live
    in any resource group (usually the plan's, not always), so the list is the subscription's and the
    plan is found by `targetResourceUri`. A search of the plan's group alone would call a plan
    "without autoscale" when its setting sits elsewhere."""
    path = f"/subscriptions/{t.subscription}/providers/Microsoft.Insights/autoscalesettings"
    return get_list(ctx, path, API_AUTOSCALE)


def _bound(value):
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def autoscale(rows, complete, t, instances, limit):
    """The `autoscale` output: the autoscale settings that target this plan. `instances` is the
    plan's current instance count. `atMaximum` says it has reached a profile's ceiling; it is only
    given for an enabled setting (a disabled one scales nothing) and the profile that is active now
    is not evaluated (profiles can be scheduled), so it is a fact about each profile, not about now."""
    want = plan_id(t.subscription, t.group, t.name).lower()
    out = []
    for row in rows:
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        if str(props.get("targetResourceUri") or "").lower() != want:
            continue
        profiles = []
        for p in props.get("profiles") or []:
            if not isinstance(p, dict):
                continue
            cap = p.get("capacity") if isinstance(p.get("capacity"), dict) else {}
            low, high, default = (_bound(cap.get(k)) for k in ("minimum", "maximum", "default"))
            profiles.append(
                {
                    "name": str(p.get("name") or ""),
                    "minimum": low,
                    "maximum": high,
                    "default": default,
                    "ruleCount": len(p.get("rules") or []),
                    "scheduled": bool(p.get("fixedDate") or p.get("recurrence")),
                    "atMaximum": None if high is None or instances is None else instances >= high,
                }
            )
        enabled = props.get("enabled") if isinstance(props.get("enabled"), bool) else None
        if enabled is not True:
            for profile in profiles:
                profile["atMaximum"] = None
        out.append(
            {
                "name": str(row.get("name") or ""),
                "enabled": enabled,
                "profiles": profiles[:50],
                "profilesOmitted": max(len(profiles) - 50, 0),
                "notificationCount": len(props.get("notifications") or []),
            }
        )
    out.sort(key=lambda s: s["name"].lower())
    return {
        "plan": t.name,
        "instanceCount": instances,
        # a list cut short that held no setting for the plan says nothing: null, not false
        "configured": True if out else (False if complete else None),
        "enabled": any(s["enabled"] is True for s in out),
        "settings": out[:limit],
        "total": len(out),
        "omitted": max(len(out) - limit, 0),
        "listComplete": complete,
    }


def scale_bounds(auto):
    """{enabled, minimum, maximum} of an `autoscale` output, for a scale to compare its request with.

    enabled: true when an enabled setting targets the plan, false when the list was read whole and holds
    none, null when that cannot be told (the list was cut short and holds none). minimum and maximum are
    the widest envelope of the enabled settings' profiles (the active profile is not known), null when
    there is none or a profile does not give the number."""
    live = [s for s in auto["settings"] if s["enabled"] is True]
    if live:
        enabled = True
    elif auto["configured"] is None:
        enabled = None
    else:
        enabled = False
    lows = [p["minimum"] for s in live for p in s["profiles"]]
    highs = [p["maximum"] for s in live for p in s["profiles"]]
    known = lambda values: bool(values) and all(v is not None for v in values)  # noqa: E731
    return {
        "enabled": enabled,
        "minimum": min(lows) if known(lows) else None,
        "maximum": max(highs) if known(highs) else None,
    }
