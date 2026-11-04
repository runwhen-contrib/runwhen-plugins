"""The synthetic Azure the azure-aks-health scenarios are captured from.

Everything here is the ARM REST body Microsoft documents for an operation (each builder names its
reference page), for invented resources: one subscription, the three resource groups of the other
scenarios, and two AKS clusters, `aks-orders-prod` (healthy) and `aks-orders-dev` (broken, and
stopped in the `stopped` scenario). `capture_aks.py` serves these bodies to the pinned `az` through
`fake_az.py`; what the CLI prints for them is what the stub answers.

Documentation ranges only for addresses (RFC 5737), GUIDs from the stub's list
(tests/unit/test_fixture_hygiene.py), no real names.
"""

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
TENANT = "c0ffee00-1234-4abc-8def-0123456789ab"
CALLER = "9d2c5a10-77b4-4f3e-8a1c-5e6f7a8b9c0d"
CLUSTER_IDENTITY = "00000000-0000-4000-8000-0000000000b1"  # the cluster's system-assigned identity
KUBELET_CLIENT = "00000000-0000-4000-8000-0000000000b2"
KUBELET_OBJECT = "00000000-0000-4000-8000-0000000000b3"
ADMIN_GROUP = "00000000-0000-4000-8000-0000000000b4"  # an Entra group
OIDC_ID = "00000000-0000-4000-8000-0000000000b5"
WORKSPACE = (
    f"/subscriptions/{SUB}/resourceGroups/rg-observability/providers/"
    "Microsoft.OperationalInsights/workspaces/law-central"
)
RP = "Microsoft.ContainerService"

GROUPS = {  # name -> location (the same three groups as the key vault scenarios)
    "rg-orders-dev": "westeurope",
    "rg-orders-prod": "eastus",
    "RG-Platform": "northeurope",
}
PROD = ("aks-orders-prod", "rg-orders-prod", "eastus")
DEV = ("aks-orders-dev", "rg-orders-dev", "westeurope")


def cluster_id(name, group):
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/{RP}/managedClusters/{name}"


def node_group(name, group, location):
    return f"MC_{group}_{name}_{location}"


# --- agent pools (https://learn.microsoft.com/rest/api/aks/agent-pools/list) ------------------------------


def prune(value):
    """The body without its None values: the API leaves out what a resource does not have."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def pool(**kw):
    """The properties of an agent pool as the API returns them; `kw` overrides the defaults."""
    props = {
        "count": 3,
        "vmSize": "Standard_D4s_v5",
        "osDiskSizeGB": 128,
        "osDiskType": "Managed",
        "kubeletDiskType": "OS",
        "maxPods": 30,
        "type": "VirtualMachineScaleSets",
        "availabilityZones": ["1", "2", "3"],
        "enableAutoScaling": False,
        "scaleDownMode": "Delete",
        "provisioningState": "Succeeded",
        "powerState": {"code": "Running"},
        "orchestratorVersion": "1.33.4",
        "currentOrchestratorVersion": "1.33.4",
        "enableNodePublicIP": False,
        "mode": "User",
        "osType": "Linux",
        "osSKU": "Ubuntu",
        "nodeImageVersion": "AKSUbuntu-2204gen2containerd-202510.20.0",
        "upgradeSettings": {"maxSurge": "10%"},
        "enableFIPS": False,
    }
    props.update(kw)
    return prune(props)


def pools_prod():
    return {
        "system": pool(
            mode="System",
            count=3,
            enableAutoScaling=True,
            minCount=3,
            maxCount=5,
            vmSize="Standard_D2s_v5",
        ),
        "apps": pool(count=4, enableAutoScaling=True, minCount=2, maxCount=10),
    }


def pools_dev(power="Running"):
    return {
        "system": pool(
            mode="System",
            count=1,
            vmSize="Standard_D2s_v5",
            orchestratorVersion="1.29.9",
            currentOrchestratorVersion="1.29.9",
            availabilityZones=None,
            powerState={"code": power},
        ),
        "apps": pool(
            count=10,
            enableAutoScaling=True,
            minCount=2,
            maxCount=10,
            orchestratorVersion="1.29.9",
            currentOrchestratorVersion="1.28.14",  # the pool is still on the old version
            availabilityZones=None,
            powerState={"code": power},
        ),
        "batch": pool(
            count=3,
            enableAutoScaling=True,
            minCount=0,
            maxCount=5,
            scaleSetPriority="Spot",
            scaleSetEvictionPolicy="Delete",
            spotMaxPrice=-1.0,
            provisioningState="Failed",
            orchestratorVersion="1.29.9",
            currentOrchestratorVersion="1.29.9",
            availabilityZones=None,
            powerState={"code": power},
        ),
    }


def agent_pools_body(name, group, pools):
    base = cluster_id(name, group)
    return {
        "value": [
            {
                "id": f"{base}/agentPools/{pool_name}",
                "name": pool_name,
                "type": f"{RP}/managedClusters/agentPools",
                "properties": props,
            }
            for pool_name, props in pools.items()
        ]
    }


# --- the clusters (https://learn.microsoft.com/rest/api/aks/managed-clusters/get) --------------------------


def cluster_body(name, group, location, *, pools, **overrides):
    nrg = node_group(name, group, location)
    props = {
        "provisioningState": "Succeeded",
        "powerState": {"code": "Running"},
        "kubernetesVersion": "1.33.4",
        "currentKubernetesVersion": "1.33.4",
        "dnsPrefix": f"{name}-dns",
        "fqdn": f"{name}-dns-1a2b3c4d.hcp.{location}.azmk8s.io",
        "nodeResourceGroup": nrg,
        "supportPlan": "KubernetesOfficial",
        "enableRBAC": True,
        "agentPoolProfiles": [{"name": n, **p} for n, p in pools.items()],
        "servicePrincipalProfile": {"clientId": "msi"},
        "networkProfile": {
            "networkPlugin": "azure",
            "networkPluginMode": "overlay",
            "networkPolicy": "cilium",
            "networkDataplane": "cilium",
            "loadBalancerSku": "standard",
            "outboundType": "loadBalancer",
            "serviceCidr": "198.51.100.0/24",
            "dnsServiceIP": "198.51.100.10",
            "podCidr": "192.0.2.0/24",
            "serviceCidrs": ["198.51.100.0/24"],
            "podCidrs": ["192.0.2.0/24"],
            "ipFamilies": ["IPv4"],
        },
        "apiServerAccessProfile": {
            "enablePrivateCluster": False,
            "authorizedIPRanges": ["203.0.113.0/24"],
        },
        "aadProfile": {
            "managed": True,
            "enableAzureRBAC": True,
            "adminGroupObjectIDs": [ADMIN_GROUP],
            "tenantID": TENANT,
        },
        "disableLocalAccounts": True,
        "autoUpgradeProfile": {"upgradeChannel": "patch", "nodeOSUpgradeChannel": "NodeImage"},
        "oidcIssuerProfile": {
            "enabled": True,
            "issuerURL": f"https://{location}.oic.prod-aks.azure.com/{TENANT}/{OIDC_ID}/",
        },
        "securityProfile": {"workloadIdentity": {"enabled": True}},
        "addonProfiles": {
            "omsagent": {
                "enabled": True,
                "config": {"logAnalyticsWorkspaceResourceID": WORKSPACE},
            }
        },
        "azureMonitorProfile": {"metrics": {"enabled": True}},
        "identityProfile": {
            "kubeletidentity": {
                "resourceId": f"/subscriptions/{SUB}/resourceGroups/{nrg}/providers/"
                f"Microsoft.ManagedIdentity/userAssignedIdentities/{name}-agentpool",
                "clientId": KUBELET_CLIENT,
                "objectId": KUBELET_OBJECT,
            }
        },
        "maxAgentPools": 100,
    }
    props.update(overrides)
    return prune(
        {
            "id": cluster_id(name, group),
            "name": name,
            "type": f"{RP}/ManagedClusters",
            "location": location,
            "tags": {"env": "prod" if "prod" in name else "dev", "team": "orders"},
            "sku": {"name": "Base", "tier": "Standard" if "prod" in name else "Free"},
            "identity": {
                "type": "SystemAssigned",
                "principalId": CLUSTER_IDENTITY,
                "tenantId": TENANT,
            },
            "properties": props,
        }
    )


def prod_cluster():
    name, group, location = PROD
    return cluster_body(name, group, location, pools=pools_prod())


def dev_cluster(*, stopped=False):
    """Broken: a failed provisioning state, a version AKS no longer supports, one failed spot pool,
    an API server open to everyone and local accounts on. Stopped: the same cluster, switched off."""
    name, group, location = DEV
    return cluster_body(
        name,
        group,
        location,
        pools=pools_dev("Stopped" if stopped else "Running"),
        provisioningState="Succeeded" if stopped else "Failed",
        powerState={"code": "Stopped" if stopped else "Running"},
        kubernetesVersion="1.29.9",
        currentKubernetesVersion="1.29.9",
        apiServerAccessProfile={"enablePrivateCluster": False},
        disableLocalAccounts=False,
        autoUpgradeProfile={"upgradeChannel": "none"},
        aadProfile=None,
        addonProfiles={},
        azureMonitorProfile=None,
        securityProfile={},
        oidcIssuerProfile=None,
    )


def aks_list_body(*clusters):
    return {"value": list(clusters)}


# --- versions (https://learn.microsoft.com/rest/api/aks/managed-clusters/list-kubernetes-versions) -------------


def kubernetes_versions():
    def minor(version, patches, *, default=False, preview=False, plans=("KubernetesOfficial",)):
        out = {
            "version": version,
            "capabilities": {"supportPlan": list(plans)},
            "patchVersions": {p: {"upgrades": ups} for p, ups in patches.items()},
        }
        if default:
            out["isDefault"] = True
        if preview:
            out["isPreview"] = True
        return out

    return {
        "values": [
            minor("1.35", {"1.35.0": []}, preview=True),
            minor("1.34", {"1.34.1": [], "1.34.0": ["1.34.1"]}, default=True),
            minor("1.33", {"1.33.5": ["1.34.1"], "1.33.4": ["1.33.5", "1.34.1"]}),
            minor(
                "1.32",
                {"1.32.9": ["1.33.4", "1.33.5"], "1.32.8": ["1.32.9", "1.33.4"]},
                plans=("KubernetesOfficial", "AKSLongTermSupport"),
            ),
        ]
    }


def upgrade_profile(name, group, version, upgrades, pools):
    """https://learn.microsoft.com/rest/api/aks/managed-clusters/get-upgrade-profile"""
    return {
        "id": f"{cluster_id(name, group)}/upgradeProfiles/default",
        "name": "default",
        "type": f"{RP}/managedClusters/upgradeProfiles",
        "properties": {
            "controlPlaneProfile": {
                "kubernetesVersion": version,
                "osType": "Linux",
                "upgrades": [
                    {"kubernetesVersion": v, **({"isPreview": True} if p else {})}
                    for v, p in upgrades
                ],
            },
            "agentPoolProfiles": [
                {
                    "kubernetesVersion": pool_version,
                    "name": pool_name,
                    "osType": "Linux",
                    "upgrades": [{"kubernetesVersion": v} for v, p in upgrades if not p],
                }
                for pool_name, pool_version in pools
            ],
        },
    }


# --- diagnostic settings (https://learn.microsoft.com/rest/api/monitor/diagnostic-settings/list) ----------------


def diagnostic_settings(name, group):
    return {
        "value": [
            {
                "id": f"{cluster_id(name, group)}/providers/microsoft.insights/diagnosticSettings/aks-to-law",
                "name": "aks-to-law",
                "type": "Microsoft.Insights/diagnosticSettings",
                "properties": {
                    "workspaceId": WORKSPACE,
                    "logs": [
                        {"category": "kube-apiserver", "enabled": True},
                        {"category": "kube-audit-admin", "enabled": True},
                        {"category": "cluster-autoscaler", "enabled": True},
                        {"category": "guard", "enabled": False},
                    ],
                    "metrics": [{"category": "AllMetrics", "enabled": False}],
                },
            }
        ]
    }


# --- Resource Health (https://learn.microsoft.com/rest/api/resourcehealth/availability-statuses/get-by-resource) ---


def resource_health(name, group, location, *, degraded):
    base = {
        "id": f"{cluster_id(name, group)}/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
        "name": "current",
        "type": "Microsoft.ResourceHealth/AvailabilityStatuses",
        "location": location,
    }
    if not degraded:
        props = {
            "availabilityState": "Available",
            "title": "Available",
            "summary": "There aren't any known Azure platform problems affecting this Managed Cluster.",
            "detailedStatus": "",
            "reasonType": "",
            "context": "",
            "category": "",
            "reasonChronicity": "Persistent",
            "occurredTime": "2026-10-29T08:00:00Z",
            "reportedTime": "2026-11-04T00:30:00.0000000Z",
            "serviceImpactingEvents": [],
        }
    else:
        props = {
            "availabilityState": "Degraded",
            "title": "Degraded",
            "summary": "We are investigating reduced availability of the Kubernetes API server for AKS in West Europe. Some requests may fail or take longer than usual.",
            "detailedStatus": "Reduced availability (platform event 4MK2-8T1).",
            "reasonType": "Unplanned",
            "context": "Azure initiated",
            "category": "Unplanned",
            "reasonChronicity": "Transient",
            "occurredTime": "2026-11-03T21:40:00Z",
            "reportedTime": "2026-11-03T21:44:12.1250000Z",
            "rootCauseAttributionTime": "2026-11-03T21:40:00Z",
            "healthEventType": "ServiceHealth",
            "healthEventCause": "PlatformInitiated",
            "healthEventCategory": "Unplanned",
            "healthEventId": "4MK2-8T1",
            "serviceImpactingEvents": [
                {
                    "eventStartTime": "2026-11-03T21:40:00Z",
                    "eventStatusLastModifiedTime": "2026-11-04T00:10:00Z",
                    "correlationId": "8a1b2c3d-0000-4000-8000-000000000001",
                    "status": {"value": "Active"},
                    "incidentProperties": {
                        "title": "Azure Kubernetes Service - West Europe - Reduced API server availability",
                        "service": "Azure Kubernetes Service",
                        "region": "West Europe",
                        "incidentType": "Incident",
                    },
                }
            ],
        }
    return {**base, "properties": props}


# --- Azure Monitor metrics (https://learn.microsoft.com/rest/api/monitor/metrics/list) -----------------------

METRICS = {  # metric -> (display name, description)
    "node_cpu_usage_percentage": ("CPU Usage Percentage", "Percentage of CPU used by the nodes"),
    "node_memory_working_set_percentage": (
        "Memory Working Set Percentage",
        "Percentage of the memory working set used by the nodes",
    ),
    "node_disk_usage_percentage": (
        "Disk Used Percentage",
        "Percentage of the disk used by the nodes",
    ),
}
FIRST_GRAIN = "2026-11-03T04:00:00Z"  # the captured window: 24 hourly grains from here


def grain(i):
    day, hour = (3, 4 + i) if 4 + i < 24 else (4, 4 + i - 24)
    return f"2026-11-{day:02d}T{hour:02d}:00:00Z"


def metric_body(name, group, location, metric, averages, maximums):
    """24 hourly grains; None in `averages` is a grain Azure Monitor has no value for."""
    display, description = METRICS[metric]
    data = []
    for i, (avg, top) in enumerate(zip(averages, maximums)):
        point = {"timeStamp": grain(i)}
        if avg is not None:
            point["average"] = avg
        if top is not None:
            point["maximum"] = top
        data.append(point)
    return {
        "cost": 0,
        "timespan": f"{grain(0)}/{grain(24)}",
        "interval": "PT1H",
        "namespace": "Microsoft.ContainerService/managedClusters",
        "resourceregion": location,
        "value": [
            {
                "id": f"{cluster_id(name, group)}/providers/Microsoft.Insights/metrics/{metric}",
                "type": "Microsoft.Insights/metrics",
                "name": {"value": metric, "localizedValue": display},
                "displayDescription": description,
                "unit": "Percent",
                "errorCode": "Success",
                "timeseries": [{"metadatavalues": [], "data": data}],
            }
        ],
    }


def calm(base, peak_extra):
    averages = [round(base + (i % 5) * 1.5, 1) for i in range(24)]
    return averages, [round(a + peak_extra, 1) for a in averages]


def metrics_prod():
    return {
        "node_cpu_usage_percentage": calm(34, 18),
        "node_memory_working_set_percentage": calm(48, 14),
        "node_disk_usage_percentage": calm(21, 9),
    }


def metrics_dev():
    """CPU and memory climb over the last hours; one disk grain has no value and so does the newest."""
    cpu = [round(40 + i * 0.5, 1) for i in range(18)] + [86.0, 91.5, 94.0, 96.5, 97.0, 95.5]
    mem = [round(60 + i * 0.4, 1) for i in range(18)] + [82.0, 85.5, 88.0, 90.5, 91.0, 92.5]
    disk = [round(30 + (i % 4) * 2.0, 1) for i in range(24)]
    disk[9] = None
    disk[23] = None
    return {
        "node_cpu_usage_percentage": (cpu, [min(round(v + 3.5, 1), 100.0) for v in cpu]),
        "node_memory_working_set_percentage": (mem, [min(round(v + 4.0, 1), 100.0) for v in mem]),
        "node_disk_usage_percentage": (
            disk,
            [None if v is None else round(v + 6.0, 1) for v in disk],
        ),
    }


def metrics_stopped():
    """A stopped cluster reports nothing: every grain is there, none has a value."""
    empty = [None] * 24
    return {m: (empty, empty) for m in METRICS}


# --- the activity log (https://learn.microsoft.com/rest/api/monitor/activity-logs/list) -----------------------


def local(value):
    return {"value": value, "localizedValue": value}


def event(when, operation, resource_id, *, sub_status, code, message, level="Error"):
    return {
        "eventTimestamp": when,
        "level": level,
        "operationName": local(operation),
        "resourceId": resource_id,
        "status": local("Failed"),
        "subStatus": local(sub_status),
        "properties": {
            "statusCode": sub_status,
            "serviceRequestId": "",
            "statusMessage": '{"status":"Failed","error":{"code":"%s","message":"%s"}}'
            % (code, message),
        },
    }


def events_healthy():
    """One failed operation in the group, on a storage account: the cluster has none."""
    return {
        "value": [
            event(
                "2026-11-04T01:12:30.1234567Z",
                "Microsoft.Storage/storageAccounts/write",
                f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.Storage/storageAccounts/storders01",
                sub_status="Conflict",
                code="StorageAccountAlreadyExists",
                message="The storage account named storders01 is already taken.",
            )
        ]
    }


def events_broken():
    name, group, _ = DEV
    cid = cluster_id(name, group)
    upper = cid.upper()  # the activity log often spells an id in capitals
    return {
        "value": [
            event(
                "2026-11-04T02:41:05.4400000Z",
                f"{RP}/managedClusters/agentPools/write",
                f"{cid}/agentPools/batch",
                sub_status="BadRequest",
                code="QuotaExceeded",
                message="Operation could not be completed as it results in exceeding approved Total Regional Cores quota. Additional details - Deployment Model: Resource Manager, Location: westeurope, Current Limit: 100, Current Usage: 98, Additional Required: 8.",
            ),
            event(
                "2026-11-04T02:11:40.9100000Z",
                f"{RP}/managedClusters/write",
                upper,
                sub_status="Conflict",
                code="VMExtensionProvisioningError",
                message="VM has reported a failure when processing extension 'cse-agent-0'. Error message: Enable failed: failed to execute command: command terminated with exit status=50.",
            ),
            event(
                "2026-11-04T01:52:18.2200000Z",
                f"{RP}/managedClusters/agentPools/write",
                f"{cid}/agentPools/batch",
                sub_status="BadRequest",
                code="QuotaExceeded",
                message="Operation could not be completed as it results in exceeding approved Total Regional Cores quota. Additional details - Deployment Model: Resource Manager, Location: westeurope, Current Limit: 100, Current Usage: 98, Additional Required: 8.",
            ),
            event(
                "2026-11-03T23:30:00.0000000Z",
                f"{RP}/managedClusters/write",
                cid,
                sub_status="Conflict",
                code="VMExtensionProvisioningError",
                message="VM has reported a failure when processing extension 'cse-agent-0'. Error message: Enable failed: failed to execute command: command terminated with exit status=50.",
            ),
            event(
                "2026-11-03T22:05:10.0000000Z",
                f"{RP}/managedClusters/write",
                f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/{RP}/managedClusters/aks-orders-dev2",
                sub_status="Conflict",
                code="VMExtensionProvisioningError",
                message="Another cluster whose name starts with this one's.",
            ),
            event(
                "2026-11-03T21:00:00.0000000Z",
                "Microsoft.Network/publicIPAddresses/write",
                f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/Microsoft.Network/publicIPAddresses/pip-orders-dev",
                sub_status="Conflict",
                code="PublicIPCountLimitReached",
                message="Cannot create more than 10 public IP addresses for this subscription in this region.",
                level="Warning",
            ),
        ]
    }


def events_none():
    return {"value": []}


# --- errors (https://learn.microsoft.com/rest/api/azure/#common-error-response) ------------------------------


def arm_error(code, message, status):
    return status, {"error": {"code": code, "message": message}}


def forbidden_error(action, scope):
    return arm_error(
        "AuthorizationFailed",
        f"The client '{CALLER}' with object id '{CALLER}' does not have authorization to perform action "
        f"'{action}' over scope '{scope}' or the scope is invalid. If access was recently granted, "
        "please refresh your credentials.",
        403,
    )


THROTTLED = arm_error(
    "TooManyRequests",
    "The request is being throttled as the limit has been reached for operation type - 'Read'. "
    "For more information, see https://aka.ms/srpthrottlinglimits. Please try again later.",
    429,
)


def not_found_error(resource, group):
    return arm_error(
        "ResourceNotFound",
        f"The Resource '{resource}' under resource group '{group}' was not found. "
        "For more details please go to https://aka.ms/ARMResourceNotFoundFix",
        404,
    )
