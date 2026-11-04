"""The synthetic Azure the azure-vm-health stub scenarios are rendered from (render_vm.py).

Nothing here talks to Azure. `answer()` is a stand-in for Azure Resource Manager: given an HTTP request
that the real az 2.92 CLI makes (in the azure@1 image, with its transport replaced), it returns the REST
body Microsoft documents for that operation. render_vm.py runs the real CLI against it, so what the
stub prints is the CLI's own formatting, and stderr and exit codes are the CLI's own.

REST references (the bodies follow these):
  https://learn.microsoft.com/rest/api/compute/virtual-machines/get
  https://learn.microsoft.com/rest/api/compute/virtual-machines/instance-view
  https://learn.microsoft.com/rest/api/compute/virtual-machines/list
  https://learn.microsoft.com/rest/api/compute/virtual-machine-scale-sets/get
  https://learn.microsoft.com/rest/api/compute/virtual-machine-scale-sets/get-instance-view
  https://learn.microsoft.com/rest/api/compute/virtual-machine-scale-set-vms/list
  https://learn.microsoft.com/rest/api/compute/virtual-machines/run-command
  https://learn.microsoft.com/rest/api/monitor/metrics/list
  https://learn.microsoft.com/rest/api/monitor/activity-logs/list
  https://learn.microsoft.com/rest/api/monitor/autoscale-settings/list-by-resource-group
  https://learn.microsoft.com/rest/api/resourcehealth/availability-statuses/get-by-resource
"""

import json
import re

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
CALLER = "9d2c5a10-77b4-4f3e-8a1c-5e6f7a8b9c0d"
CORRELATION = "8a1b2c3d-0000-4000-8000-000000000001"
EVENT_CORRELATION = "8a1b2c3d-0000-4000-8000-000000000002"
VM_IDS = {  # the VM's own vmId, synthetic
    "vm-orders-web01": "5e1f0c2a-7b3d-4a9e-8c10-2d4f6a8b0c01",
    "vm-orders-batch02": "5e1f0c2a-7b3d-4a9e-8c10-2d4f6a8b0c02",
    "vm-platform-legacy": "5e1f0c2a-7b3d-4a9e-8c10-2d4f6a8b0c03",
}
VMSS_IDS = {
    "vmss-orders-api": "6f2a1d3b-8c4e-4b0f-9d21-3e5a7b9c1d01",
    "vmss-orders-jobs": "6f2a1d3b-8c4e-4b0f-9d21-3e5a7b9c1d02",
}

GROUP_SPELLING = {
    "rg-orders-dev": "rg-orders-dev",
    "rg-orders-prod": "rg-orders-prod",
    "rg-platform": "RG-Platform",
}
GROUP_LOCATION = {
    "rg-orders-dev": "westeurope",
    "rg-orders-prod": "eastus",
    "rg-platform": "northeurope",
}


def rid(group, provider_type, name):
    return f"/subscriptions/{SUB}/resourceGroups/{GROUP_SPELLING[group]}/providers/Microsoft.Compute/{provider_type}/{name}"


def status(code, level, display, message=None, time="2026-11-04T01:12:09+00:00"):
    s = {"code": code, "level": level, "displayStatus": display, "time": time}
    if message is not None:
        s["message"] = message
    return s


def ok_ext(name, ext_type, version):
    return {
        "name": name,
        "type": ext_type,
        "typeHandlerVersion": version,
        "statuses": [
            status(
                "ProvisioningState/succeeded", "Info", "Provisioning succeeded", "Extension enabled"
            )
        ],
    }


BOOT_BLOBS = {
    "consoleScreenshotBlobUri": "https://stordersdiag.blob.core.windows.net/bootdiagnostics-{vm}/{vm}.screenshot.bmp",
    "serialConsoleLogBlobUri": "https://stordersdiag.blob.core.windows.net/bootdiagnostics-{vm}/{vm}.serialconsole.log",
}

# --- virtual machines -----------------------------------------------------------------------------
VMS = {
    "vm-orders-web01": dict(
        group="rg-orders-prod",
        size="Standard_D2s_v5",
        provisioning="Succeeded",
        power="running",
        agent="ready",
        boot=True,
        extension="ok",
        tags={"env": "prod", "team": "orders"},
    ),
    "vm-orders-batch02": dict(
        group="rg-orders-dev",
        size="Standard_D4s_v5",
        provisioning="Failed",
        power="running",
        agent="not-ready",
        boot=False,
        extension="failed",
        tags={"env": "dev", "team": "orders"},
    ),
    "vm-platform-legacy": dict(
        group="rg-platform",
        size="Standard_B2s",
        provisioning="Succeeded",
        power="deallocated",
        agent=None,
        boot=False,
        extension=None,
        tags={"team": "platform"},
    ),
}


def vm_resource(name, expand=False):
    v = VMS[name]
    props = {
        "vmId": VM_IDS[name],
        "hardwareProfile": {"vmSize": v["size"]},
        "storageProfile": {
            "imageReference": {
                "publisher": "Canonical",
                "offer": "ubuntu-24_04-lts",
                "sku": "server",
                "version": "latest",
                "exactVersion": "24.04.202510150",
            },
            "osDisk": {
                "osType": "Linux",
                "name": f"{name}_OsDisk",
                "createOption": "FromImage",
                "caching": "ReadWrite",
                "diskSizeGB": 64,
                "managedDisk": {
                    "storageAccountType": "Premium_LRS",
                    "id": f"/subscriptions/{SUB}/resourceGroups/{GROUP_SPELLING[v['group']]}/providers/Microsoft.Compute/disks/{name}_OsDisk",
                },
            },
            "dataDisks": [],
        },
        "osProfile": {
            "computerName": name,
            "adminUsername": "azureuser",
            "linuxConfiguration": {"disablePasswordAuthentication": True, "provisionVMAgent": True},
            "secrets": [],
            "allowExtensionOperations": True,
        },
        "networkProfile": {
            "networkInterfaces": [
                {
                    "id": f"/subscriptions/{SUB}/resourceGroups/{GROUP_SPELLING[v['group']]}/providers/Microsoft.Network/networkInterfaces/{name}-nic"
                }
            ]
        },
        "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}}
        if v["boot"]
        else {"bootDiagnostics": {"enabled": False}},
        "provisioningState": v["provisioning"],
        "timeCreated": "2026-06-12T08:30:11.1234567+00:00",
    }
    if expand:
        props["instanceView"] = instance_view(name)
    return {
        "name": name,
        "id": rid(v["group"], "virtualMachines", name),
        "type": "Microsoft.Compute/virtualMachines",
        "location": GROUP_LOCATION[v["group"]],
        "tags": v["tags"],
        "properties": props,
    }


def instance_view(name):
    v = VMS[name]
    statuses = []
    if v["provisioning"] == "Failed":
        statuses.append(
            status(
                "ProvisioningState/failed/VMExtensionProvisioningError",
                "Error",
                "Provisioning failed",
                "VM has reported a failure when processing extension 'CustomScript'. Error message: \"Enable failed: failed to execute command: command terminated with exit status=3\".",
            )
        )
    else:
        statuses.append(status("ProvisioningState/succeeded", "Info", "Provisioning succeeded"))
    statuses.append(
        status(
            f"PowerState/{v['power']}",
            "Info",
            {"running": "VM running", "deallocated": "VM deallocated"}[v["power"]],
            time=None,
        )
    )
    view = {
        "computerName": name,
        "osName": "ubuntu",
        "osVersion": "24.04",
        "hyperVGeneration": "V2",
        "platformUpdateDomain": 0,
        "platformFaultDomain": 0,
        "disks": [
            {
                "name": f"{name}_OsDisk",
                "statuses": [
                    status("ProvisioningState/succeeded", "Info", "Provisioning succeeded")
                ],
            }
        ],
        "statuses": statuses,
    }
    if v["agent"] == "ready":
        view["vmAgent"] = {
            "vmAgentVersion": "2.13.1.1",
            "statuses": [
                status("ProvisioningState/succeeded", "Info", "Ready", "Guest Agent is running")
            ],
            "extensionHandlers": [],
        }
    elif v["agent"] == "not-ready":
        view["vmAgent"] = {
            "vmAgentVersion": "Unknown",
            "statuses": [
                status(
                    "ProvisioningState/Unavailable",
                    "Warning",
                    "Not Ready",
                    "VM Agent is unresponsive.",
                )
            ],
        }
    if v["extension"] == "ok":
        view["extensions"] = [
            ok_ext(
                "AzureMonitorLinuxAgent", "Microsoft.Azure.Monitor.AzureMonitorLinuxAgent", "1.33.1"
            )
        ]
    elif v["extension"] == "failed":
        view["extensions"] = [
            ok_ext(
                "AzureMonitorLinuxAgent", "Microsoft.Azure.Monitor.AzureMonitorLinuxAgent", "1.33.1"
            ),
            {
                "name": "CustomScript",
                "type": "Microsoft.Azure.Extensions.CustomScript",
                "typeHandlerVersion": "2.1.10",
                "statuses": [
                    status(
                        "ProvisioningState/failed/3",
                        "Error",
                        "Provisioning failed",
                        "Enable failed: failed to execute command: command terminated with exit status=3\n[stdout]\n\n[stderr]\nmkdir: cannot create directory '/data/app': No space left on device\n",
                    )
                ],
            },
        ]
    if v["boot"]:
        view["bootDiagnostics"] = {k: u.format(vm=name) for k, u in BOOT_BLOBS.items()}
    return view


# --- scale sets -------------------------------------------------------------------------------------
VMSS = {
    "vmss-orders-api": dict(
        group="rg-orders-prod", capacity=3, tags={"env": "prod", "team": "orders"}
    ),
    "vmss-orders-jobs": dict(
        group="rg-orders-dev", capacity=5, tags={"env": "dev", "team": "orders"}
    ),
}


def vmss_resource(name):
    v = VMSS[name]
    prod = name == "vmss-orders-api"
    props = {
        "singlePlacementGroup": False,
        "orchestrationMode": "Uniform",
        "overprovision": not prod,
        "platformFaultDomainCount": 1,
        "provisioningState": "Succeeded",
        "uniqueId": VMSS_IDS[name],
        "zoneBalance": False,
        "upgradePolicy": {
            "mode": "Rolling" if prod else "Manual",
            **(
                {
                    "rollingUpgradePolicy": {
                        "maxBatchInstancePercent": 20,
                        "maxUnhealthyInstancePercent": 20,
                        "maxUnhealthyUpgradedInstancePercent": 20,
                        "pauseTimeBetweenBatches": "PT0S",
                    },
                    "automaticOSUpgradePolicy": {
                        "enableAutomaticOSUpgrade": False,
                        "disableAutomaticRollback": False,
                    },
                }
                if prod
                else {}
            ),
        },
        "scaleInPolicy": {"rules": ["Default"], "forceDeletion": False},
        "virtualMachineProfile": {
            "osProfile": {
                "computerNamePrefix": name[-8:],
                "adminUsername": "azureuser",
                "linuxConfiguration": {"disablePasswordAuthentication": True},
                "secrets": [],
            },
            "storageProfile": {
                "imageReference": {
                    "publisher": "Canonical",
                    "offer": "ubuntu-24_04-lts",
                    "sku": "server",
                    "version": "latest",
                },
                "osDisk": {
                    "osType": "Linux",
                    "createOption": "FromImage",
                    "caching": "ReadWrite",
                    "managedDisk": {"storageAccountType": "Premium_LRS"},
                },
            },
            "networkProfile": {
                "networkInterfaceConfigurations": [
                    {"name": "nic", "properties": {"primary": True}}
                ],
                **(
                    {
                        "healthProbe": {
                            "id": f"/subscriptions/{SUB}/resourceGroups/{GROUP_SPELLING[v['group']]}/providers/Microsoft.Network/loadBalancers/lb-orders/probes/http"
                        }
                    }
                    if prod
                    else {}
                ),
            },
            "extensionProfile": {
                "extensions": [
                    {
                        "name": "HealthExtension",
                        "properties": {
                            "type": "ApplicationHealthLinux",
                            "typeHandlerVersion": "1.0",
                            "provisioningState": "Succeeded",
                            "settings": {
                                "protocol": "http",
                                "port": 8080,
                                "requestPath": "/healthz",
                            },
                        },
                    }
                ]
            }
            if prod
            else {
                "extensions": [
                    {
                        "name": "CustomScript",
                        "properties": {
                            "type": "CustomScript",
                            "typeHandlerVersion": "2.1",
                            "provisioningState": "Succeeded",
                        },
                    }
                ]
            },
        },
        "automaticRepairsPolicy": {
            "enabled": prod,
            "gracePeriod": "PT30M",
            "repairAction": "Replace",
        },
    }
    return {
        "name": name,
        "id": rid(v["group"], "virtualMachineScaleSets", name),
        "type": "Microsoft.Compute/virtualMachineScaleSets",
        "location": GROUP_LOCATION[v["group"]],
        "tags": v["tags"],
        "zones": ["1", "2", "3"] if prod else [],
        "sku": {
            "name": "Standard_D2s_v5" if prod else "Standard_D4s_v5",
            "tier": "Standard",
            "capacity": v["capacity"],
        },
        "properties": props,
    }


def vmss_instance_view(name):
    jobs = name == "vmss-orders-jobs"
    summary = (
        [
            {"code": "ProvisioningState/succeeded", "count": 3},
            {"code": "ProvisioningState/failed/VMExtensionProvisioningError", "count": 1},
            {"code": "ProvisioningState/updating", "count": 1},
        ]
        if jobs
        else [{"code": "ProvisioningState/succeeded", "count": 3}]
    )
    return {
        "virtualMachine": {"statusesSummary": summary},
        "extensions": [
            {
                "name": "CustomScript" if jobs else "HealthExtension",
                "statusesSummary": [
                    {"code": "ProvisioningState/succeeded", "count": 4 if jobs else 3}
                ]
                + ([{"code": "ProvisioningState/failed/3", "count": 1}] if jobs else []),
            }
        ],
        "statuses": [status("ProvisioningState/succeeded", "Info", "Provisioning succeeded")],
    }


def vmss_instances(name):
    v = VMSS[name]
    jobs = name == "vmss-orders-jobs"
    rows = []
    for i in range(v["capacity"]):
        failed, outdated, health = False, False, "healthy"
        if jobs:
            failed, outdated, health = (
                (i == 3),
                (i in (1, 4)),
                ("unhealthy" if i == 2 else "healthy"),
            )
        iid = str(i)
        statuses = [
            status(
                "ProvisioningState/failed/VMExtensionProvisioningError"
                if failed
                else "ProvisioningState/succeeded",
                "Error" if failed else "Info",
                "Provisioning failed" if failed else "Provisioning succeeded",
            ),
            status("PowerState/running", "Info", "VM running", time=None),
        ]
        view = {
            "platformUpdateDomain": i % 5,
            "platformFaultDomain": 0,
            "computerName": f"{name[-8:]}{i:06d}",
            "osName": "ubuntu",
            "osVersion": "24.04",
            "vmAgent": {
                "vmAgentVersion": "2.13.1.1",
                "statuses": [
                    status("ProvisioningState/succeeded", "Info", "Ready", "Guest Agent is running")
                ],
            },
            "vmHealth": {
                "status": {
                    "code": f"HealthState/{health}",
                    "level": "Info" if health == "healthy" else "Error",
                    "displayStatus": "Healthy" if health == "healthy" else "Unhealthy",
                    **(
                        {
                            "message": "Application health probe failed: connection refused on port 8080"
                        }
                        if health == "unhealthy"
                        else {}
                    ),
                }
            },
            "extensions": [
                {
                    "name": "CustomScript" if jobs else "HealthExtension",
                    "type": "Microsoft.Azure.Extensions.CustomScript"
                    if jobs
                    else "Microsoft.ManagedServices.ApplicationHealthLinux",
                    "typeHandlerVersion": "2.1.10",
                    "statuses": [
                        status(
                            "ProvisioningState/failed/3",
                            "Error",
                            "Provisioning failed",
                            "Enable failed: exit status=3",
                        )
                        if failed
                        else status("ProvisioningState/succeeded", "Info", "Provisioning succeeded")
                    ],
                }
            ],
            "statuses": statuses,
        }
        rows.append(
            {
                "instanceId": iid,
                "name": f"{name}_{iid}",
                "id": f"{rid(v['group'], 'virtualMachineScaleSets', name)}/virtualMachines/{iid}",
                "type": "Microsoft.Compute/virtualMachineScaleSets/virtualMachines",
                "location": GROUP_LOCATION[v["group"]],
                "sku": {
                    "name": "Standard_D4s_v5" if jobs else "Standard_D2s_v5",
                    "tier": "Standard",
                },
                "zones": [str(i % 3 + 1)] if not jobs else [],
                "properties": {
                    "latestModelApplied": not outdated,
                    "modelDefinitionApplied": "VirtualMachineScaleSet",
                    "vmId": f"7a3b2e4c-9d5f-4c1a-8e32-4f6b8c0d2e{i:02d}",
                    "provisioningState": "Failed"
                    if failed
                    else ("Updating" if jobs and i == 4 else "Succeeded"),
                    "protectionPolicy": {
                        "protectFromScaleIn": False,
                        "protectFromScaleSetActions": False,
                    },
                    "instanceView": view,
                },
            }
        )
    return rows


def autoscale_resources(group):
    def setting(name, vmss, enabled, low, high, default, cpu_out, cpu_in):
        return {
            "id": f"/subscriptions/{SUB}/resourceGroups/{GROUP_SPELLING[group]}/providers/microsoft.insights/autoscalesettings/{name}",
            "name": name,
            "type": "Microsoft.Insights/autoscaleSettings",
            "location": GROUP_LOCATION[group],
            "tags": {},
            "properties": {
                "profiles": [
                    {
                        "name": "default",
                        "capacity": {
                            "minimum": str(low),
                            "maximum": str(high),
                            "default": str(default),
                        },
                        "rules": [
                            {
                                "metricTrigger": {
                                    "metricName": "Percentage CPU",
                                    "metricResourceUri": rid(
                                        group, "virtualMachineScaleSets", vmss
                                    ),
                                    "timeGrain": "PT1M",
                                    "statistic": "Average",
                                    "timeWindow": "PT5M",
                                    "timeAggregation": "Average",
                                    "operator": "GreaterThan",
                                    "threshold": cpu_out,
                                    "dividePerInstance": False,
                                },
                                "scaleAction": {
                                    "direction": "Increase",
                                    "type": "ChangeCount",
                                    "value": "1",
                                    "cooldown": "PT5M",
                                },
                            },
                            {
                                "metricTrigger": {
                                    "metricName": "Percentage CPU",
                                    "metricResourceUri": rid(
                                        group, "virtualMachineScaleSets", vmss
                                    ),
                                    "timeGrain": "PT1M",
                                    "statistic": "Average",
                                    "timeWindow": "PT10M",
                                    "timeAggregation": "Average",
                                    "operator": "LessThan",
                                    "threshold": cpu_in,
                                    "dividePerInstance": False,
                                },
                                "scaleAction": {
                                    "direction": "Decrease",
                                    "type": "ChangeCount",
                                    "value": "1",
                                    "cooldown": "PT10M",
                                },
                            },
                        ],
                    }
                ],
                "notifications": [
                    {
                        "operation": "Scale",
                        "email": {
                            "sendToSubscriptionAdministrator": False,
                            "sendToSubscriptionCoAdministrators": False,
                            "customEmails": [],
                        },
                        "webhooks": [],
                    }
                ],
                "enabled": enabled,
                "targetResourceUri": rid(group, "virtualMachineScaleSets", vmss),
                "name": name,
            },
        }

    if group == "rg-orders-prod":
        return [setting("as-orders-api", "vmss-orders-api", True, 2, 6, 3, 70, 25)]
    if group == "rg-orders-dev":
        other = setting("as-orders-batch", "vmss-orders-batch", True, 1, 4, 1, 80, 30)
        return [setting("as-orders-jobs", "vmss-orders-jobs", True, 2, 5, 2, 75, 25), other]
    return []


# --- metrics ----------------------------------------------------------------------------------------
METRIC_UNITS = {
    "Percentage CPU": "Percent",
    "Available Memory Bytes": "Bytes",
    "Disk Read Bytes": "Bytes",
    "Disk Write Bytes": "Bytes",
    "Network In Total": "Bytes",
    "Network Out Total": "Bytes",
}
GIB = 1024**3


def metric_points(metric, aggs, broken, missing=False):
    """24 intervals of 15 minutes ending 2026-11-04T04:00Z; the newest first-to-last is oldest first."""
    pts = []
    for i in range(24):
        h, m = divmod(i * 15, 60)
        stamp = (
            f"2026-11-03T{22 + h:02d}:{m:02d}:00+00:00"
            if 22 + h < 24
            else f"2026-11-04T{22 + h - 24:02d}:{m:02d}:00+00:00"
        )
        p = {"timeStamp": stamp}
        if missing:
            pts.append(p)
            continue
        if metric == "Percentage CPU":
            avg = 18.0 + (i % 5) * 2.5
            mx = avg + 22.0
            if broken and i in (14, 15, 16):
                avg, mx = 83.5 + i % 3, 96.0 - (i % 3)
            vals = {"average": round(avg, 2), "maximum": round(mx, 2)}
        elif metric == "Available Memory Bytes":
            low = 6.2 * GIB if not broken else 1.4 * GIB
            avg = low - (i % 4) * 0.1 * GIB
            mn = avg - 0.2 * GIB
            if broken and i in (18, 19):
                avg, mn = 0.31 * GIB, 0.17 * GIB
            vals = {"average": float(int(avg)), "minimum": float(int(mn))}
        elif metric in ("Disk Read Bytes", "Disk Write Bytes"):
            vals = {
                "total": float(
                    40_000_000 + i * 1_500_000 + (800_000_000 if broken and i in (14, 15) else 0)
                )
            }
        else:
            vals = {"total": float(120_000_000 + i * 2_000_000)}
        if metric == "Available Memory Bytes" and broken and i == 23:
            p.update({})  # newest interval still has a value
        for a in aggs:
            p[a] = vals[a]
        pts.append(p)
    return pts


def metrics_body(resource_id, namespace, region, metric, aggs, broken, missing):
    return {
        "cost": 0,
        "timespan": "2026-11-03T22:00:00Z/2026-11-04T04:00:00Z",
        "interval": "PT15M",
        "namespace": namespace,
        "resourceregion": region,
        "value": [
            {
                "id": f"{resource_id}/providers/Microsoft.Insights/metrics/{metric}",
                "type": "Microsoft.Insights/metrics",
                "name": {"value": metric, "localizedValue": metric},
                "displayDescription": f"{metric} of the resource",
                "unit": METRIC_UNITS[metric],
                "timeseries": [
                    {"metadatavalues": [], "data": metric_points(metric, aggs, broken, missing)}
                ],
                "errorCode": "Success",
            }
        ],
    }


# --- activity log -----------------------------------------------------------------------------------
def event(
    resource_id,
    group,
    op_value,
    op_text,
    level,
    status_value,
    sub_status,
    message,
    time,
    category="Administrative",
):
    return {
        "authorization": {"action": op_value, "scope": resource_id},
        "caller": CALLER,
        "channels": "Operation",
        "claims": {"aud": "https://management.core.windows.net/", "appid": CALLER},
        "correlationId": EVENT_CORRELATION,
        "description": "",
        "eventDataId": "0e1f2a3b-4c5d-4e6f-8a7b-9c0d1e2f3a4b",
        "eventName": {"value": "EndRequest", "localizedValue": "End request"},
        "category": {"value": category, "localizedValue": category},
        "eventTimestamp": time,
        "id": f"{resource_id}/events/0e1f2a3b-4c5d-4e6f-8a7b-9c0d1e2f3a4b/ticks/639003000000000000",
        "level": level,
        "operationId": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "operationName": {"value": op_value, "localizedValue": op_text},
        "resourceGroupName": GROUP_SPELLING[group],
        "resourceProviderName": {
            "value": "Microsoft.Compute",
            "localizedValue": "Microsoft.Compute",
        },
        "resourceType": {
            "value": op_value.rsplit("/", 1)[0],
            "localizedValue": op_value.rsplit("/", 1)[0],
        },
        "resourceId": resource_id,
        "status": {"value": status_value, "localizedValue": status_value},
        "subStatus": {"value": sub_status, "localizedValue": sub_status},
        "submissionTimestamp": time,
        "subscriptionId": SUB,
        "tenantId": "c0ffee00-1234-4abc-8def-0123456789ab",
        "properties": {
            "statusCode": "Conflict" if status_value == "Failed" else "OK",
            "statusMessage": message,
            "serviceRequestId": None,
        },
        "relatedEvents": [],
    }


def events_for(resource_id, group, broken):
    base = [
        event(
            resource_id,
            group,
            "Microsoft.Compute/virtualMachines/write",
            "Create or Update Virtual Machine",
            "Informational",
            "Succeeded",
            "",
            "",
            "2026-11-04T01:02:03.4560000Z",
        )
    ]
    if not broken:
        return base
    return [
        event(
            resource_id,
            group,
            "Microsoft.Compute/virtualMachines/extensions/write",
            "Create or Update Virtual Machine Extension",
            "Error",
            "Failed",
            "Conflict",
            json.dumps(
                {
                    "status": "Failed",
                    "error": {
                        "code": "ResourceDeploymentFailure",
                        "message": "The resource operation completed with terminal provisioning state 'Failed'.",
                        "details": [
                            {
                                "code": "VMExtensionProvisioningError",
                                "message": "VM has reported a failure when processing extension 'CustomScript'. Error message: \"Enable failed: failed to execute command: command terminated with exit status=3\".",
                            }
                        ],
                    },
                }
            ),
            "2026-11-04T01:12:09.1230000Z",
        ),
        event(
            resource_id,
            group,
            "Microsoft.Compute/virtualMachines/restart/action",
            "Restart Virtual Machine",
            "Warning",
            "Failed",
            "",
            "The VM agent did not respond within the allotted time.",
            "2026-11-04T02:41:55.0000000Z",
        ),
        event(
            resource_id,
            group,
            "Microsoft.Compute/virtualMachines/write",
            "Create or Update Virtual Machine",
            "Critical",
            "Failed",
            "InternalServerError",
            "An internal execution error occurred.",
            "2026-11-04T03:05:20.0000000Z",
        ),
    ] + base


# --- resource health ---------------------------------------------------------------------------------
def health_body(resource_id, location, state, kind_label):
    base = {
        "id": f"{resource_id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
        "location": location,
        "name": "current",
        "type": "Microsoft.ResourceHealth/AvailabilityStatuses",
    }
    if state == "Available":
        base["properties"] = {
            "availabilityState": "Available",
            "title": "Available",
            "summary": f"There aren't any known Azure platform problems affecting this {kind_label}.",
            "reasonType": "",
            "occurredTime": "2026-11-04T02:00:00Z",
            "reportedTime": "2026-11-04T02:00:00Z",
            "serviceImpactingEvents": [],
        }
    else:
        base["properties"] = {
            "availabilityState": "Unavailable",
            "title": "Unavailable",
            "summary": f"We're sorry, your {kind_label} is unavailable because of an unplanned platform event.",
            "detailedStatus": "The host server was rebooted unexpectedly; the VM is being recovered on another host (event 3QK1-8T0).",
            "reasonType": "Unplanned",
            "context": "Azure initiated",
            "category": "Unplanned",
            "reasonChronicity": "Transient",
            "occurredTime": "2026-11-04T01:30:00Z",
            "reportedTime": "2026-11-04T01:31:44.2250000Z",
            "rootCauseAttributionTime": "2026-11-04T01:30:00Z",
            "healthEventType": "ServiceHealth",
            "healthEventCause": "PlatformInitiated",
            "healthEventCategory": "Unplanned",
            "healthEventId": "3QK1-8T0",
            "serviceImpactingEvents": [
                {
                    "eventStartTime": "2026-11-04T01:30:00Z",
                    "eventStatusLastModifiedTime": "2026-11-04T02:10:00Z",
                    "correlationId": CORRELATION,
                    "status": {"value": "Active"},
                    "incidentProperties": {
                        "title": "Virtual Machines - Unexpected host reboot",
                        "service": "Virtual Machines",
                        "region": location,
                        "incidentType": "Incident",
                    },
                }
            ],
        }
    return base


# --- the guest's answers to the four scripts (guest/*.txt, see capture/guest.sh) ---------------------
def run_command_body(stdout, stderr=""):
    msg = f"Enable succeeded: \n[stdout]\n{stdout}\n[stderr]\n{stderr}\n"
    return {
        "value": [
            {
                "code": "ProvisioningState/succeeded",
                "level": "Info",
                "displayStatus": "Provisioning succeeded",
                "message": msg,
                "time": None,
            }
        ]
    }


# --- Azure Resource Manager -----------------------------------------------------------------------------
ACTION_BY_ROUTE = [
    (r"/runcommand$", "Microsoft.Compute/virtualMachines/runCommand/action"),
    (
        r"/virtualmachinescalesets/[^/]+/virtualmachines$",
        "Microsoft.Compute/virtualMachineScaleSets/virtualMachines/read",
    ),
    (
        r"/virtualmachinescalesets/[^/]+/instanceview$",
        "Microsoft.Compute/virtualMachineScaleSets/instanceView/read",
    ),
    (r"/virtualmachinescalesets(/[^/]+)?$", "Microsoft.Compute/virtualMachineScaleSets/read"),
    (
        r"/virtualmachines/[^/]+/instanceview$",
        "Microsoft.Compute/virtualMachines/instanceView/read",
    ),
    (r"/virtualmachines(/[^/]+)?$", "Microsoft.Compute/virtualMachines/read"),
    (r"/microsoft\.insights/metrics$", "Microsoft.Insights/metrics/read"),
    (r"/microsoft\.insights/autoscalesettings$", "Microsoft.Insights/autoscalesettings/read"),
    (r"/eventtypes/management/values$", "Microsoft.Insights/eventtypes/management/values/read"),
    (r"/availabilitystatuses/current$", "Microsoft.ResourceHealth/availabilityStatuses/read"),
]


def error(status_code, code, message, headers=None):
    return status_code, headers or {}, {"error": {"code": code, "message": message}}


def forbidden(path, method):
    action = next(a for pattern, a in ACTION_BY_ROUTE if re.search(pattern, path.lower()))
    scope = re.split(r"/providers/microsoft\.(?:insights|resourcehealth)", path, flags=re.I)[0]
    scope = re.sub(
        r"/(virtualMachines|virtualMachineScaleSets)/([^/]+)/(runCommand|instanceView|virtualMachines)$",
        r"/\1/\2",
        scope,
        flags=re.I,
    )
    scope = re.sub(
        r"/providers/Microsoft.Compute/(virtualMachines|virtualMachineScaleSets)$",
        "",
        scope,
        flags=re.I,
    )
    return error(
        403,
        "AuthorizationFailed",
        f"The client '{CALLER}' with object id '{CALLER}' does not have authorization to perform action '{action}' over scope '{scope}' or the scope is invalid. If access was recently granted, please refresh your credentials.",
    )


THROTTLE = "The request is being throttled as the limit has been reached for operation type - 'Read'. For more information, see https://aka.ms/srpthrottlinglimits. Please try again later."


def guest_output(scenario, check):
    from pathlib import Path

    name = "broken" if scenario == "guest-broken" else "healthy"
    return (Path(__file__).parent / "guest" / f"{check}-{name}.txt").read_text()


CHECK_OF_SCRIPT = {}  # filled by render_vm.py: script text -> check name


def answer(scenario, method, url, body=None):
    """(status, headers, json body) of ARM for one request, in `scenario` ('healthy', 'broken', 'forbidden', ...)."""
    path = re.sub(r"^https?://[^/]+", "", url.split("?")[0])
    query = url.split("?", 1)[1] if "?" in url else ""
    low = path.lower()
    if scenario == "throttled":
        return error(429, "TooManyRequests", THROTTLE, {"Retry-After": "0"})
    if scenario == "forbidden":
        return forbidden(path, method)
    m = re.match(
        r"/subscriptions/([^/]+)/resourcegroups/([^/]+)/providers/microsoft\.compute/(virtualmachines|virtualmachinescalesets)(?:/([^/]+))?(?:/(instanceview|virtualmachines|runcommand))?$",
        low,
    )
    if m:
        sub, group, plural, name, sub_resource = m.groups()
        if group not in GROUP_SPELLING:
            return error(
                404, "ResourceGroupNotFound", f"Resource group '{group}' could not be found."
            )
        table = VMS if plural == "virtualmachines" else VMSS
        kind = "virtualMachines" if plural == "virtualmachines" else "virtualMachineScaleSets"
        if name is None:  # list in the group
            names = [n for n, v in table.items() if v["group"] == group]
            make = vm_resource if plural == "virtualmachines" else vmss_resource
            return 200, {}, {"value": [make(n) for n in names]}
        real = next((n for n in table if n.lower() == name), None)
        if real is None or table[real]["group"] != group:
            return error(
                404,
                "ResourceNotFound",
                f"The Resource 'Microsoft.Compute/{kind}/{name_as_asked(name, url)}' under resource group '{GROUP_SPELLING.get(group, group)}' was not found. For more details please go to https://aka.ms/ARMResourceNotFoundFix",
            )
        if plural == "virtualmachines":
            if sub_resource == "runcommand":
                if scenario == "runcommand-forbidden":
                    return forbidden(path, method)
                check = CHECK_OF_SCRIPT[
                    json.loads(body)["script"][0]
                    if isinstance(json.loads(body)["script"], list)
                    else json.loads(body)["script"]
                ]
                return 200, {}, run_command_body(guest_output(scenario, check))
            return 200, {}, vm_resource(real, expand="instanceview" in query.lower())
        # scale set
        if sub_resource == "instanceview":
            if scenario == "instance-view-forbidden":
                return forbidden(path, method)
            return 200, {}, vmss_instance_view(real)
        if sub_resource == "virtualmachines":
            if scenario == "instances-forbidden":
                return forbidden(path, method)
            return 200, {}, {"value": vmss_instances(real)}
        return 200, {}, vmss_resource(real)
    if low.endswith("/microsoft.insights/autoscalesettings"):
        group = re.match(r"/subscriptions/[^/]+/resourcegroups/([^/]+)/", low).group(1)
        return 200, {}, {"value": autoscale_resources(group)}
    if low.endswith("/providers/microsoft.resourcehealth/availabilitystatuses/current"):
        rid_ = path[: -len("/providers/Microsoft.ResourceHealth/availabilityStatuses/current")]
        return (
            200,
            {},
            health_body(
                rid_,
                "eastus",
                "Available" if scenario == "healthy" else "Unavailable",
                "virtual machine" if "/virtualmachines/" in low else "scale set",
            ),
        )
    if low.endswith("/providers/microsoft.insights/metrics"):
        rid_ = path[: -len("/providers/Microsoft.Insights/metrics")]
        names = re.search(r"metricnames=([^&]+)", query)
        from urllib.parse import unquote

        metric = unquote(names.group(1)).replace("+", " ")
        aggs = [
            a.strip().lower()
            for a in unquote(re.search(r"aggregation=([^&]+)", query).group(1)).split(",")
        ]
        vm = "/virtualmachines/" in low
        broken = scenario in ("broken", "no-data")
        missing = scenario == "no-data" and metric in ("Available Memory Bytes", "Disk Read Bytes")
        return (
            200,
            {},
            metrics_body(
                rid_,
                "Microsoft.Compute/virtualMachines"
                if vm
                else "Microsoft.Compute/virtualMachineScaleSets",
                "eastus",
                metric,
                aggs,
                broken,
                missing,
            ),
        )
    if low.endswith("/providers/microsoft.insights/eventtypes/management/values"):
        rid_ = re.search(
            r"resourceId eq '([^']+)'", __import__("urllib.parse").parse.unquote(query)
        ).group(1)
        group = re.match(r"/subscriptions/[^/]+/resourcegroups/([^/]+)/", rid_.lower()).group(1)
        return 200, {}, {"value": events_for(rid_, group, scenario == "broken")}
    return error(404, "NotFound", f"No route for {method} {path}")


def name_as_asked(lowered, url):
    m = re.search(r"/(?:virtualMachines|virtualMachineScaleSets)/([^/?]+)", url, re.I)
    return m.group(1) if m else lowered
