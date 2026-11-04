"""node-pools: every node pool's count, provisioning and power state, version and autoscaler bounds."""

from .az import az_json
from .cluster import resolve, section
from .common import NESTED_LIMIT, listing
from .errors import RwError

FAILED = ("Failed", "Canceled")
SETTLED = ("Succeeded", "Failed", "Canceled")


def text(value):
    return value if isinstance(value, str) and value else None


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def describe(pool):
    """The facts of one pool, from a row of `az aks nodepool list`. A pool's ARM `type` is the
    resource type; its pool type (VirtualMachineScaleSets) is `typePropertiesType`."""
    count, low, high = (
        number(pool.get("count")),
        number(pool.get("minCount")),
        number(pool.get("maxCount")),
    )
    autoscale = pool.get("enableAutoScaling") is True
    state = text(pool.get("provisioningState"))
    power = text(section(pool, "powerState").get("code"))
    desired, current = (
        text(pool.get("orchestratorVersion")),
        text(pool.get("currentOrchestratorVersion")),
    )
    zones = pool.get("availabilityZones")
    return {
        "name": str(pool.get("name") or ""),
        "mode": text(pool.get("mode")),
        "osType": text(pool.get("osType")),
        "osSku": text(pool.get("osSku")),
        "vmSize": text(pool.get("vmSize")),
        "poolType": text(pool.get("typePropertiesType")),
        "scaleSetPriority": text(pool.get("scaleSetPriority")),
        "count": count,
        "provisioningState": state,
        "transitioning": None if state is None else state not in SETTLED,
        "powerState": power,
        "autoscaleEnabled": autoscale,
        "minCount": low if autoscale else None,
        "maxCount": high if autoscale else None,
        # the autoscaler has no room left to add (at max) or to remove (at min) a node
        "atMaxCount": (count >= high)
        if autoscale and count is not None and high is not None
        else None,
        "atMinCount": (count <= low)
        if autoscale and count is not None and low is not None
        else None,
        "orchestratorVersion": desired,
        "currentOrchestratorVersion": current,
        "versionDiffers": None if desired is None or current is None else desired != current,
        "nodeImageVersion": text(pool.get("nodeImageVersion")),
        "availabilityZones": [str(z) for z in zones][:NESTED_LIMIT]
        if isinstance(zones, list)
        else [],
        "maxSurge": text(section(pool, "upgradeSettings").get("maxSurge")),
    }


def read(ctx, limit):
    subscription, group, name, _, doc = resolve(ctx)
    rows = az_json(
        ctx,
        ["aks", "nodepool", "list", "--cluster-name", name, "-g", group, "--subscription", subscription],
    )  # fmt: skip
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az aks nodepool list returned no list")
    if not rows:
        # a cluster always has a system pool: an empty list is not "no pools", it is a bad answer
        raise RwError("internal", "az aks nodepool list returned no node pool for the cluster")
    pools = sorted((describe(r) for r in rows), key=lambda p: p["name"])
    counted = [p["count"] for p in pools if p["count"] is not None]
    system = [p for p in pools if p["mode"] == "System"]
    failed = [p["name"] for p in pools if p["provisioningState"] in FAILED]
    stopped = [p["name"] for p in pools if p["powerState"] not in (None, "Running")]
    sections = listing(pools, limit)
    return {
        "cluster": name,
        "clusterProvisioningState": text(doc.get("provisioningState")),
        "clusterPowerState": text(section(doc, "powerState").get("code")),
        "poolCount": len(pools),
        "nodeCount": sum(counted),
        "nodeCountKnown": len(counted) == len(pools),
        "systemPoolCount": len(system),
        "systemNodeCount": sum(p["count"] or 0 for p in system),
        "failedPoolCount": len(failed),
        "failedPools": failed[:NESTED_LIMIT],
        "notRunningPoolCount": len(stopped),
        "notRunningPools": stopped[:NESTED_LIMIT],
        "pools": sections,
    }
