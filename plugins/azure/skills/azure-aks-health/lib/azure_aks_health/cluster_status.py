"""cluster-status: the cluster's provisioning and power state, version, access and network settings
and diagnostic settings, as plain facts. The node pools are the next task's."""

from .az import az_json
from .cluster import resolve, section
from .common import NESTED_LIMIT
from .errors import RwError

SETTLED = ("Succeeded", "Failed", "Canceled")  # provisioning states that are not in progress


def text(value):
    return value if isinstance(value, str) and value else None


def flag(value):
    """True or False when Azure says so, None when it says nothing."""
    return value if isinstance(value, bool) else None


def settings_list(reply):
    """The settings of `az monitor diagnostic-settings list` (a list, or {value: [...]})."""
    if isinstance(reply, dict):
        reply = reply.get("value")
    if not isinstance(reply, list):
        raise RwError("internal", "az monitor diagnostic-settings list returned no list")
    return [s for s in reply if isinstance(s, dict)]


def diagnostics(reply):
    """Facts about the diagnostic settings: how many, their names, whether any sends to Log
    Analytics, and the log categories (or category groups) that are switched on."""
    found = settings_list(reply)
    enabled = sorted(
        {
            str(log.get("category") or log.get("categoryGroup"))
            for s in found
            for log in s.get("logs") or []
            if isinstance(log, dict)
            and log.get("enabled") is True
            and (log.get("category") or log.get("categoryGroup"))
        }
    )
    names = sorted(str(s.get("name") or "") for s in found)
    return {
        "settingsCount": len(found),
        "names": names[:NESTED_LIMIT],
        "logAnalytics": any(bool(s.get("workspaceId")) for s in found),
        "enabledLogCategories": enabled[:NESTED_LIMIT],
        "enabledLogCategoriesOmitted": max(len(enabled) - NESTED_LIMIT, 0),
    }


def read(ctx):
    subscription, _, name, resource_id, doc = resolve(ctx)
    sku, power = section(doc, "sku"), section(doc, "powerState")
    access, aad = section(doc, "apiServerAccessProfile"), section(doc, "aadProfile")
    network, upgrade = section(doc, "networkProfile"), section(doc, "autoUpgradeProfile")
    oidc, security = section(doc, "oidcIssuerProfile"), section(doc, "securityProfile")
    monitor, addons = section(doc, "azureMonitorProfile"), section(doc, "addonProfiles")
    identity = section(doc, "identity")
    state = text(doc.get("provisioningState"))
    ranges = access.get("authorizedIpRanges")
    pools = doc.get("agentPoolProfiles")
    try:
        diag = diagnostics(
            az_json(
                ctx,
                [
                    "monitor",
                    "diagnostic-settings",
                    "list",
                    "--resource",
                    resource_id,
                    "--subscription",
                    subscription,
                ],
            )  # fmt: skip
        )
    except RwError as exc:
        ctx.error(exc.code, exc.message, scope="diagnostics")
        diag = None
    return {
        "cluster": name,
        "resourceGroup": text(doc.get("resourceGroup")),
        "location": text(doc.get("location")),
        "skuTier": text(sku.get("tier")),
        "provisioningState": state,
        # In progress (Creating, Updating, Upgrading, Scaling, Starting, Stopping...): the state
        # will change; Succeeded, Failed and Canceled are final.
        "transitioning": None if state is None else state not in SETTLED,
        "powerState": text(power.get("code")),
        "running": None if text(power.get("code")) is None else power.get("code") == "Running",
        "kubernetesVersion": text(doc.get("kubernetesVersion")),
        "currentKubernetesVersion": text(doc.get("currentKubernetesVersion")),
        "supportPlan": text(doc.get("supportPlan")),
        "nodeResourceGroup": text(doc.get("nodeResourceGroup")),
        "nodePoolCount": len(pools) if isinstance(pools, list) else None,
        "fqdn": text(doc.get("fqdn")),
        "privateCluster": access.get("enablePrivateCluster") is True,
        # 0 on a public cluster: the API server accepts connections from any address
        "authorizedIpRangeCount": len(ranges) if isinstance(ranges, list) else 0,
        "localAccountsDisabled": doc.get("disableLocalAccounts") is True,
        "rbacEnabled": flag(doc.get("enableRbac")),
        "aadManaged": aad.get("managed") is True,
        "azureRbacEnabled": aad.get("enableAzureRbac") is True,
        "identityType": text(identity.get("type")),
        "networkPlugin": text(network.get("networkPlugin")),
        "networkPluginMode": text(network.get("networkPluginMode")),
        "networkPolicy": text(network.get("networkPolicy")),
        "outboundType": text(network.get("outboundType")),
        "loadBalancerSku": text(network.get("loadBalancerSku")),
        "autoUpgradeChannel": text(upgrade.get("upgradeChannel")),
        "nodeOsUpgradeChannel": text(upgrade.get("nodeOsUpgradeChannel")),
        "oidcIssuerEnabled": oidc.get("enabled") is True,
        "workloadIdentityEnabled": section(security, "workloadIdentity").get("enabled") is True,
        "containerInsights": section(addons, "omsagent").get("enabled") is True
        or section(monitor, "containerInsights").get("enabled") is True,
        "managedPrometheus": section(monitor, "metrics").get("enabled") is True,
        "diagnostics": diag,
    }
