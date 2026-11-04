"""The scale-set tasks: the model (`az vmss show`), the instances against it (`az vmss
get-instance-view`, `az vmss list-instances`) and the autoscale settings that target it
(`az monitor autoscale list`).

The model is facts about the scale set's configuration: sizes, policies, the image's marketplace
reference and extension names. It never returns the OS profile (admin names, SSH keys, secrets), custom
data or extension settings."""

import time

from .az import az_json
from .common import NESTED_LIMIT, listing
from .errors import RwError
from .instance_view import state_of
from .text import safe_clip
from .target import VMSS, resolve

LIST_BUDGET = 240  # seconds in which a new call of the instance task may still start


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


# --- scale-set-config ---------------------------------------------------------------------------


def model_facts(name, doc):
    """The configuration facts of a scale set from `az vmss show`."""
    profile = _dict(doc.get("virtualMachineProfile"))
    image = _dict(_dict(profile.get("storageProfile")).get("imageReference"))
    os_disk = _dict(_dict(profile.get("storageProfile")).get("osDisk"))
    repairs = _dict(doc.get("automaticRepairsPolicy"))
    upgrade = _dict(doc.get("upgradePolicy"))
    auto_os = _dict(upgrade.get("automaticOSUpgradePolicy"))
    rolling = _dict(upgrade.get("rollingUpgradePolicy"))
    scale_in = _dict(doc.get("scaleInPolicy"))
    sku = _dict(doc.get("sku"))
    extensions = _list(_dict(profile.get("extensionProfile")).get("extensions"))
    network = _dict(profile.get("networkProfile"))
    ext_rows = [
        {
            "name": str(e.get("name") or ""),
            "type": e.get("type") or e.get("typePropertiesType") or None,
            "typeHandlerVersion": e.get("typeHandlerVersion") or None,
            "provisioningState": e.get("provisioningState") or None,
        }
        for e in extensions
    ]
    return {
        "scaleSet": name,
        "location": doc.get("location") or None,
        "provisioningState": doc.get("provisioningState") or None,
        "orchestrationMode": doc.get("orchestrationMode") or None,
        "sku": {
            "name": sku.get("name") or None,
            "tier": sku.get("tier") or None,
            "capacity": sku.get("capacity"),
        },
        "zones": [z for z in (doc.get("zones") or []) if isinstance(z, str)][:NESTED_LIMIT],
        "zoneBalance": doc.get("zoneBalance"),
        "platformFaultDomainCount": doc.get("platformFaultDomainCount"),
        "singlePlacementGroup": doc.get("singlePlacementGroup"),
        "overprovision": doc.get("overprovision"),
        "upgradeMode": upgrade.get("mode") or None,
        "automaticOsUpgradeEnabled": auto_os.get("enableAutomaticOSUpgrade"),
        "rollingUpgradeMaxBatchPercent": rolling.get("maxBatchInstancePercent"),
        "rollingUpgradeMaxUnhealthyPercent": rolling.get("maxUnhealthyInstancePercent"),
        "automaticRepairsEnabled": repairs.get("enabled"),
        "automaticRepairsGracePeriod": repairs.get("gracePeriod") or None,
        "scaleInRules": [r for r in (scale_in.get("rules") or []) if isinstance(r, str)][
            :NESTED_LIMIT
        ],
        "osType": os_disk.get("osType") or None,
        "image": {
            "publisher": image.get("publisher") or None,
            "offer": image.get("offer") or None,
            "sku": image.get("sku") or None,
            "version": image.get("version") or None,
            "exactVersion": image.get("exactVersion") or None,
            "fromGalleryOrCustomImage": bool(image.get("id") or image.get("sharedGalleryImageId")),
        },
        "hasLoadBalancerHealthProbe": bool(network.get("healthProbe")),
        "extensions": listing(ext_rows, NESTED_LIMIT),
        "tags": len(_dict(doc.get("tags"))),
    }


def read_config(ctx):
    kind, subscription, group, name, resource_id, doc = resolve(ctx, (VMSS,))
    return model_facts(name, doc)


# --- scale-set-instances ------------------------------------------------------------------------


def _codes(summary):
    rows = [
        {
            "code": str(r.get("code") or ""),
            "count": r.get("count") if isinstance(r.get("count"), int) else 0,
        }
        for r in _list(summary)
    ]
    return rows[:NESTED_LIMIT]


def instance_row(inst):
    view = _dict(inst.get("instanceView"))
    statuses = _list(view.get("statuses"))
    health = _dict(_dict(view.get("vmHealth")).get("status"))
    health_code = str(health.get("code") or "")
    health_state = health_code.split("/", 1)[1] if health_code.startswith("HealthState/") else None
    agent = _dict(view.get("vmAgent"))
    agent_status = _list(agent.get("statuses"))
    exts = _list(view.get("extensions"))
    failed_ext = sum(
        1
        for e in exts
        if any(
            s.get("level") == "Error" or "failed" in str(s.get("code") or "").lower()
            for s in _list(e.get("statuses"))[:1]
        )
    )
    provisioning = inst.get("provisioningState") or None
    latest = inst.get("latestModelApplied")
    message, cut = safe_clip(health.get("message"))
    return {
        "instanceId": str(inst.get("instanceId") or ""),
        "name": str(inst.get("name") or ""),
        "provisioningState": provisioning,
        "powerState": state_of(statuses, "PowerState/"),
        "latestModelApplied": latest if isinstance(latest, bool) else None,
        "health": health_state,
        "healthMessage": message,
        "agentReady": (agent_status[0].get("displayStatus") == "Ready") if agent_status else None,
        "failedExtensions": failed_ext,
        "zone": next((z for z in (inst.get("zones") or []) if isinstance(z, str)), None),
        "truncated": cut,
    }


def problem_rank(row):
    """Sort key: instances with something to look at first, then by instance id."""
    bad = (
        (row["provisioningState"] or "").lower() == "failed"
        or row["latestModelApplied"] is False
        or row["health"] == "unhealthy"
        or row["failedExtensions"] > 0
    )
    iid = row["instanceId"]
    return (0 if bad else 1, int(iid) if iid.isdigit() else 10**12, iid)


def counts_of(rows):
    by_prov, by_power = {}, {}
    for r in rows:
        by_prov[r["provisioningState"] or "Unknown"] = (
            by_prov.get(r["provisioningState"] or "Unknown", 0) + 1
        )
        by_power[r["powerState"] or "Unknown"] = by_power.get(r["powerState"] or "Unknown", 0) + 1
    return {
        "total": len(rows),
        "provisioningFailed": sum(
            1 for r in rows if (r["provisioningState"] or "").lower() == "failed"
        ),
        "modelOutdated": sum(1 for r in rows if r["latestModelApplied"] is False),
        "modelUnknown": sum(1 for r in rows if r["latestModelApplied"] is None),
        # healthy + unhealthy + healthUnknown = total: a state Azure reports as unknown, or none at
        # all (no health extension or probe), is unknown, never counted in either of the others
        "healthy": sum(1 for r in rows if r["health"] == "healthy"),
        "unhealthy": sum(1 for r in rows if r["health"] == "unhealthy"),
        "healthUnknown": sum(1 for r in rows if r["health"] not in ("healthy", "unhealthy")),
        "withFailedExtensions": sum(1 for r in rows if r["failedExtensions"] > 0),
        "byProvisioningState": dict(sorted(by_prov.items())),
        "byPowerState": dict(sorted(by_power.items())),
    }


def read_instances(ctx, limit, *, clock=time.monotonic, budget=LIST_BUDGET):
    kind, subscription, group, name, resource_id, doc = resolve(ctx, (VMSS,))
    started = clock()
    mode = doc.get("orchestrationMode") or None
    capacity = _dict(doc.get("sku")).get("capacity")
    out = {
        "scaleSet": name,
        "orchestrationMode": mode,
        "capacity": capacity,
        "provisioningState": doc.get("provisioningState") or None,
        "statusesSummary": None,
        "extensionsSummary": None,
        "counts": None,
        "listedMatchesCapacity": None,
        "instances": None,
    }
    base = ["--name", name, "-g", group, "--subscription", subscription]
    # the cheap aggregate first: it stands when the instance list cannot be read
    try:
        view = az_json(ctx, ["vmss", "get-instance-view", *base])
        view = _dict(view)
        out["statusesSummary"] = _codes(_dict(view.get("virtualMachine")).get("statusesSummary"))
        out["extensionsSummary"] = [
            {"name": str(e.get("name") or ""), "statuses": _codes(e.get("statusesSummary"))}
            for e in _list(view.get("extensions"))[:NESTED_LIMIT]
        ]
    except RwError as exc:
        ctx.error(exc.code, exc.message, scope="instance-view")
        view = None
    if mode == "Flexible":
        # `az vmss list-instances` returns nothing useful for a Flexible set: say so, never an empty list
        message = (
            "this scale set uses Flexible orchestration, whose instances are ordinary VMs that "
            "list-instances does not return; read them as azure/virtual-machine targets"
        )
        if view is None:  # and the summary failed too: there is nothing to report
            raise RwError("invalid_input", message, scope="instances")
        ctx.error("invalid_input", message, scope="instances")
    elif clock() - started >= budget:
        ctx.error(
            "timeout",
            f"the {budget}s budget ran out before the instances were listed",
            scope="instances",
        )
    else:
        try:
            rows = az_json(ctx, ["vmss", "list-instances", *base, "--expand", "instanceView"])
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise RwError("internal", "az vmss list-instances returned no list")
            parsed = sorted((instance_row(r) for r in rows), key=problem_rank)
            out["counts"] = counts_of(parsed)
            # a listing that does not match the model's capacity is a scale operation in flight, a
            # partial listing or a model that moved: say so rather than report a clean count
            out["listedMatchesCapacity"] = (
                len(parsed) == capacity if isinstance(capacity, int) else None
            )
            out["instances"] = listing(parsed, limit)
        except RwError as exc:
            if view is None:
                raise
            ctx.error(exc.code, exc.message, scope="instances")
    return out


# --- autoscale ----------------------------------------------------------------------------------


def _rule(rule):
    trigger = _dict(rule.get("metricTrigger"))
    action = _dict(rule.get("scaleAction"))
    return {
        "metric": trigger.get("metricName") or None,
        "statistic": trigger.get("statistic") or None,
        "timeAggregation": trigger.get("timeAggregation") or None,
        "operator": trigger.get("operator") or None,
        "threshold": trigger.get("threshold"),
        "timeWindow": trigger.get("timeWindow") or None,
        "direction": action.get("direction") or None,
        "changeType": action.get("type") or None,
        "value": action.get("value"),
        "cooldown": action.get("cooldown") or None,
    }


def _profile(profile, current):
    cap = _dict(profile.get("capacity"))

    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    low, high, default = (
        number(cap.get("minimum")),
        number(cap.get("maximum")),
        number(cap.get("default")),
    )
    return {
        "name": str(profile.get("name") or ""),
        "minimum": low,
        "maximum": high,
        "default": default,
        "fixedDate": bool(profile.get("fixedDate")),
        "recurrence": bool(profile.get("recurrence")),
        "capacityAtMaximum": (current >= high)
        if isinstance(current, int) and high is not None
        else None,
        "capacityAtMinimum": (current <= low)
        if isinstance(current, int) and low is not None
        else None,
        "rules": [_rule(r) for r in _list(profile.get("rules"))[:NESTED_LIMIT]],
    }


def read_autoscale(ctx, limit):
    kind, subscription, group, name, resource_id, doc = resolve(ctx, (VMSS,))
    current = _dict(doc.get("sku")).get("capacity")
    rows = az_json(
        ctx, ["monitor", "autoscale", "list", "-g", group, "--subscription", subscription]
    )
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az monitor autoscale list returned no list")
    mine = [r for r in rows if str(r.get("targetResourceUri") or "").lower() == resource_id.lower()]
    settings = []
    for row in mine:
        settings.append(
            {
                "name": str(row.get("name") or ""),
                "enabled": row.get("enabled"),
                "profiles": [
                    _profile(p, current) for p in _list(row.get("profiles"))[:NESTED_LIMIT]
                ],
            }
        )
    return {
        "scaleSet": name,
        "currentCapacity": current if isinstance(current, int) else None,
        "settingsInGroup": len(rows),
        "found": bool(settings),
        "anyEnabled": any(s["enabled"] is True for s in settings),
        "settings": listing(settings, limit),
    }
