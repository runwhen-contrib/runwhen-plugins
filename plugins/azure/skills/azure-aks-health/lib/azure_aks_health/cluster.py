"""What every azure-aks-health task shares about its target: the AKS cluster, read from
`ctx.resource`, looked up with the one `az aks show` that turns a cluster name into its document
and ARM resource id, and the thresholds the metric tasks compare against.

The shape of `az aks show` output is the CLI's, not the REST API's: properties are lifted to the top
level and some names differ (`enableRbac`, `authorizedIpRanges`, `dnsServiceIp`, `osSku`); a pool's
ARM `type` stays `type` and its pool type becomes `typePropertiesType`."""

import re
from types import SimpleNamespace

from .az import az_json
from .common import GUID, number
from .errors import RwError

TYPE = "azure/aks-cluster"
# AKS cluster names: 1-63 characters, letters, digits, underscores and hyphens, starting and ending
# with a letter or digit. Resource group names: up to 90 characters of letters, digits, `_ . ( ) -`,
# not starting with a hyphen (it would read as an option).
_CLUSTER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")
_GROUP = re.compile(r"^[\w.()][\w.()-]{0,89}$")
# The ARM id of a cluster, as `az aks show` returns it; it is used in a URL and compared with the
# resource ids in the activity log.
CLUSTER_ID = re.compile(
    r"^/subscriptions/(?P<sub>[0-9a-fA-F-]{36})/resourceGroups/(?P<group>[\w.()-]{1,90})"
    r"/providers/Microsoft\.ContainerService/managedClusters/(?P<name>[A-Za-z0-9_-]{1,63})$",
    re.I,
)
LOCATION = re.compile(r"^[a-z0-9]{2,40}$")


def target(resource):
    """(subscription id, resource group, cluster name) from the run's target; anything but an
    azure/aks-cluster is refused. The subscription and the resource group are identity fields of the
    target (its parents in the path); the group is lower case, as the identity keeps it."""
    resource = resource or {}
    if resource.get("type") != TYPE or not resource.get("name"):
        raise RwError(
            "invalid_input", f"this task runs on an azure/aks-cluster, got {resource.get('type')!r}"
        )
    subscription = str(resource.get("subscription") or "")
    if not GUID.match(subscription):
        raise RwError("invalid_input", "the target has no subscription id")
    group = str(resource.get("resourcegroup") or "")
    if not _GROUP.match(group):
        raise RwError("invalid_input", "the target has no valid resource group")
    name = str(resource["name"])
    if not _CLUSTER.match(name):
        raise RwError("invalid_input", f"{name!r} is not an AKS cluster name")
    return subscription.lower(), group, name


def resolve(ctx):
    """The cluster the task targets, read through `az aks show -g`: (subscription, group, name,
    ARM id, document). A cluster that is gone is not_found; the id is what the Monitor commands
    take and what the activity log's resource ids are compared with."""
    subscription, group, name = target(ctx.resource)
    doc = az_json(ctx, ["aks", "show", "--name", name, "-g", group, "--subscription", subscription])
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RwError("internal", "az aks show returned no cluster")
    if str(doc.get("name", "")).lower() != name.lower():
        raise RwError("internal", f"az aks show answered for another cluster than {name}")
    found = CLUSTER_ID.match(str(doc["id"]))
    if not found:
        raise RwError("internal", "az aks show returned an id that is not a cluster resource id")
    if found["group"].lower() != group.lower() or found["sub"].lower() != subscription:
        raise RwError("internal", f"az aks show answered for another scope than {name}'s")
    return subscription, group, name, str(doc["id"]), doc


def read_thresholds(settings):
    """The thresholds the metric task declares, checked before any az call."""
    return SimpleNamespace(
        cpu_percent=number(settings, "cpuThresholdPercent", "cpu_threshold_percent", 0, 100),
        memory_percent=number(
            settings, "memoryThresholdPercent", "memory_threshold_percent", 0, 100
        ),
        disk_percent=number(settings, "diskThresholdPercent", "disk_threshold_percent", 0, 100),
    )


def section(doc, key):
    """doc[key] if it is an object, else {}: az leaves out or nulls what a cluster does not have."""
    value = doc.get(key) if isinstance(doc, dict) else None
    return value if isinstance(value, dict) else {}


def read_scan_limit(settings):
    """The most activity-log events the task reads, checked before any az call."""
    return number(settings, "activityScanLimit", "activity_scan_limit", 1, 5000, integer=True)
