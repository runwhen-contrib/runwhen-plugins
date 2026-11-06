"""What every azure-kv-health task shares about its target: the vault, read from `ctx.resource`,
looked up with the one `az keyvault show` that turns a vault name into its ARM resource id, and the
thresholds the metric tasks compare against."""

import re
from types import SimpleNamespace

from .az import az_json
from .common import GUID, number
from .errors import RwError

TYPE = "azure/keyvault"
# Key Vault names: 3-24 characters, letters, digits and hyphens. Resource group names: up to 90
# characters of letters, digits, `_ . ( ) -`, not starting with a hyphen (it would read as an option).
_VAULT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,23}$")
_GROUP = re.compile(r"^[\w.()][\w.()-]{0,89}$")
# The ARM id of a vault, as `az keyvault show` returns it; it goes into a KQL string and into a URL.
VAULT_ID = re.compile(
    r"^/subscriptions/(?P<sub>[0-9a-fA-F-]{36})/resourceGroups/(?P<group>[\w.()-]{1,90})"
    r"/providers/Microsoft\.KeyVault/vaults/(?P<name>[A-Za-z0-9-]{3,24})$",
    re.I,
)


def target(resource):
    """(subscription id, resource group, vault name) from the run's target; anything but an
    azure/keyvault is refused. The subscription and the resource group are identity fields of the
    target (its parents in the path); the group is lower case, as the identity keeps it."""
    resource = resource or {}
    if resource.get("type") != TYPE or not resource.get("name"):
        raise RwError(
            "invalid_input", f"this task runs on an azure/keyvault, got {resource.get('type')!r}"
        )
    subscription = str(resource.get("subscription") or "")
    if not GUID.match(subscription):
        raise RwError("invalid_input", "the target has no subscription id")
    group = str(resource.get("resourcegroup") or "")
    if not _GROUP.match(group):
        raise RwError("invalid_input", "the target has no valid resource group")
    name = str(resource["name"])
    if not _VAULT.match(name):
        raise RwError("invalid_input", f"{name!r} is not a Key Vault name")
    return subscription.lower(), group, name


def resolve(ctx):
    """The vault the task targets, read through `az keyvault show -g`: (subscription, name, ARM
    id, document). A vault that is gone is not_found; the id is what the Monitor commands take."""
    subscription, group, name = target(ctx.resource)
    doc = az_json(
        ctx, ["keyvault", "show", "--name", name, "-g", group, "--subscription", subscription]
    )
    if not isinstance(doc, dict) or not doc.get("id"):
        raise RwError("internal", "az keyvault show returned no vault")
    if str(doc.get("name", "")).lower() != name.lower():
        raise RwError("internal", f"az keyvault show answered for another vault than {name}")
    found = VAULT_ID.match(str(doc["id"]))
    if not found:
        raise RwError("internal", "az keyvault show returned an id that is not a vault resource id")
    if found["group"].lower() != group.lower() or found["sub"].lower() != subscription:
        raise RwError("internal", f"az keyvault show answered for another scope than {name}'s")
    return subscription, name, str(doc["id"]), doc


def read_thresholds(settings):
    """The thresholds the metric tasks declare, checked before any az call."""
    return SimpleNamespace(
        availability_min_percent=number(
            settings, "availabilityMinPercent", "availability_min_percent", 0, 100
        ),
        request_threshold=number(settings, "requestThreshold", "request_threshold", 0, 10**9),
        latency_threshold_ms=number(
            settings, "latencyThresholdMs", "latency_threshold_ms", 0, 10**7
        ),
    )
