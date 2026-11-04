"""What every azure-vm-health task shares about its target: a virtual machine or a scale set, read
from `ctx.resource`, looked up with the one `az ... show` that turns a name into its ARM resource id,
and the thresholds the metric and in-guest tasks compare against."""

import re
from types import SimpleNamespace

from .az import az_json
from .common import GUID, number
from .errors import RwError

VM = "azure/virtual-machine"
VMSS = "azure/vm-scale-set"
# kind -> (az command group, ARM resource type segment)
KINDS = {VM: ("vm", "virtualMachines"), VMSS: ("vmss", "virtualMachineScaleSets")}
# VM and scale set names: up to 64 characters of letters, digits, `_ . -`; the first is not a hyphen
# (it would read as an option). Resource group names: up to 90 characters of letters, digits,
# `_ . ( ) -`, not starting with a hyphen.
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_GROUP = re.compile(r"^[\w.()][\w.()-]{0,89}$")
_ID = (
    r"^/subscriptions/(?P<sub>[0-9a-fA-F-]{{36}})/resourceGroups/(?P<group>[\w.()-]{{1,90}})"
    r"/providers/Microsoft\.Compute/{segment}/(?P<name>[A-Za-z0-9_][A-Za-z0-9_.-]{{0,63}})$"
)
RESOURCE_ID = {
    kind: re.compile(_ID.format(segment=segment), re.I) for kind, (_, segment) in KINDS.items()
}


def target(resource, allowed=(VM, VMSS)):
    """(type, subscription id, resource group, name) from the run's target; any type but the
    `allowed` ones is refused. The subscription and the resource group are identity fields of the
    target (its parents in the path); the group is lower case, as the identity keeps it."""
    resource = resource or {}
    kind = resource.get("type")
    if kind not in allowed or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"this task runs on {' or '.join(allowed)}, got {kind!r}",
        )
    subscription = str(resource.get("subscription") or "")
    if not GUID.match(subscription):
        raise RwError("invalid_input", "the target has no subscription id")
    group = str(resource.get("resourcegroup") or "")
    if not _GROUP.match(group):
        raise RwError("invalid_input", "the target has no valid resource group")
    name = str(resource["name"])
    if not _NAME.match(name):
        raise RwError("invalid_input", f"{name!r} is not a virtual machine or scale set name")
    return kind, subscription.lower(), group, name


def resolve(ctx, allowed=(VM, VMSS)):
    """The resource the task targets, read through `az vm|vmss show -g`: (type, subscription, group,
    name, ARM id, document). A resource that is gone is not_found; the id is what the Monitor and
    Resource Health calls take."""
    kind, subscription, group, name = target(ctx.resource, allowed)
    doc = az_json(
        ctx, [KINDS[kind][0], "show", "--name", name, "-g", group, "--subscription", subscription]
    )
    return (kind, subscription, group, name, *checked_id(doc, kind, subscription, group, name))


def checked_id(doc, kind, subscription, group, name):
    """(ARM id, document) of what `az ... show` or `get-instance-view` answered, after checking
    that it is the resource asked for: its name, its type and its scope."""
    command = KINDS[kind][0]
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RwError("internal", f"az {command} show returned no resource")
    if str(doc.get("name", "")).lower() != name.lower():
        raise RwError("internal", f"az {command} answered for another resource than {name}")
    found = RESOURCE_ID[kind].match(str(doc["id"]))
    if not found:
        raise RwError("internal", f"az {command} returned an id that is not a {kind} resource id")
    if found["group"].lower() != group.lower() or found["sub"].lower() != subscription:
        raise RwError("internal", f"az {command} answered for another scope than {name}'s")
    return str(doc["id"]), doc


def read_thresholds(settings):
    """The thresholds the tasks declare, checked before any az call."""
    return SimpleNamespace(
        cpu_threshold_percent=number(
            settings, "cpuThresholdPercent", "cpu_threshold_percent", 0, 100
        ),
        available_memory_floor_mb=number(
            settings, "availableMemoryFloorMb", "available_memory_floor_mb", 0, 10**7
        ),
        disk_used_percent=number(settings, "diskUsedPercent", "disk_used_percent", 0, 100),
        memory_used_percent=number(settings, "memoryUsedPercent", "memory_used_percent", 0, 100),
        uptime_days=number(settings, "uptimeDays", "uptime_days", 0, 36500),
    )
