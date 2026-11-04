"""Write the App Service stub scenarios, with the pinned az CLI's own code wherever it shapes the output.

The azure-appservice-health skill reads App Service through `az rest --method get` (an ARM object comes
back as ARM sent it, printed by az), Azure Monitor through `az monitor metrics list`, and writes through
`az webapp ...` and `az appservice plan update`. The stub's canned files must look like what the pinned az
prints, formatting and error texts included (lane F plan, Appendix C7), so:

  - every `az rest` reply is produced by running the real `az rest` against a local HTTP server that
    answers with the ARM body (`--skip-authorization-header`; the plain http URL is the only difference
    from the skill's relative ARM id), and the error texts (`ERROR: Not Found(...)`, `Forbidden(...)`,
    `Too Many Requests(...)`) are az's own stderr for a 404, 403 and 429;
  - the discover listings (`az webapp list`, `az functionapp list`, `az appservice plan list`) go through
    the CLI's own `list_webapp`, `list_function_app` and `list_app_service_plans` and its JSON
    serialization, with the SDK client replaced by the ARM bodies deserialized into azure-mgmt-web models;
    `az appservice plan update` prints the plan the same way;
  - `az monitor metrics list` is an AAZ command whose output is the service reply passed through the
    schema in `azure/cli/command_modules/monitor/aaz/latest/monitor/metrics/_list.py`: the reply here is
    checked against that schema's field names before it is written, and printed with knack's JSON settings
    (`indent=2, sort_keys=True`);
  - the write commands (`az webapp restart|start|stop`, `... slot swap`) return nothing; their error texts
    are the azure-core `HttpResponseError` form the CLI prints for SDK-backed commands.

    docker run --rm --network none -v "$PWD/capture:/capture:ro" -v "$OUT:/out" \
        --entrypoint /opt/az/bin/python rwp-runtime-azure:dev -I /capture/appservice.py /out

`$OUT` is plugins/azure/tests/stub/scenarios. The script rewrites the appservice-* scenario directories and
adds the App Service listings to the `healthy` and `group-forbidden` scenarios (azure-base/discover). It needs
no network and no login; the web server binds 127.0.0.1 inside the container. Every identifier is the
stub's own (README.md, test_fixture_hygiene.py)."""

import copy
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest import mock

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
TENANT = "c0ffee00-1234-4abc-8def-0123456789ab"
PRINCIPAL = "9d2c5a10-77b4-4f3e-8a1c-5e6f7a8b9c0d"
EVENT = "8a1b2c3d-0000-4000-8000-000000000001"
RG = {"dev": "rg-orders-dev", "prod": "rg-orders-prod"}
LOC = {"dev": ("westeurope", "West Europe"), "prod": ("eastus", "East US")}
DOC = "https://learn.microsoft.com/rest/api/appservice"
REF = {
    "site": f"{DOC}/web-apps/get",
    "config": f"{DOC}/web-apps/get-configuration",
    "slots": f"{DOC}/web-apps/list-slots",
    "deployments": f"{DOC}/web-apps/list-deployments",
    "plan": f"{DOC}/app-service-plans/get",
    "autoscale": "https://learn.microsoft.com/rest/api/monitor/autoscale-settings/list-by-subscription",
    "health": "https://learn.microsoft.com/rest/api/resourcehealth/availability-statuses/get-by-resource",
    "metrics": "https://learn.microsoft.com/cli/azure/monitor/metrics#az-monitor-metrics-list",
    "restart": "https://learn.microsoft.com/cli/azure/webapp#az-webapp-restart",
    "start": "https://learn.microsoft.com/cli/azure/webapp#az-webapp-start",
    "stop": "https://learn.microsoft.com/cli/azure/webapp#az-webapp-stop",
    "swap": "https://learn.microsoft.com/cli/azure/webapp/deployment/slot#az-webapp-deployment-slot-swap",
    "update": "https://learn.microsoft.com/cli/azure/appservice/plan#az-appservice-plan-update",
    "plan-list": "https://learn.microsoft.com/cli/azure/appservice/plan#az-appservice-plan-list",
    "webapp-list": "https://learn.microsoft.com/cli/azure/webapp#az-webapp-list",
    "functionapp-list": "https://learn.microsoft.com/cli/azure/functionapp#az-functionapp-list",
}

# ---------------------------------------------------------------------------------------------------
# the world: two plans, two web apps and two function apps in two resource groups
# ---------------------------------------------------------------------------------------------------
PLANS = {
    "plan-orders-prod": dict(
        env="prod", sku=("P1v3", "PremiumV3", "P1v3", "Pv3", 2), kind="linux", sites=2, max=30
    ),
    "plan-orders-dev": dict(
        env="dev", sku=("S2", "Standard", "S2", "S", 3), kind="app", sites=2, max=10
    ),
}
APPS = {
    "app-orders-api": dict(env="prod", kind="app,linux", plan="plan-orders-prod"),
    "func-orders-jobs": dict(env="prod", kind="functionapp,linux", plan="plan-orders-prod"),
    "app-orders-web": dict(env="dev", kind="app", plan="plan-orders-dev"),
    "func-orders-ingest": dict(env="dev", kind="functionapp", plan="plan-orders-dev"),
}
BROKEN = {
    "app-orders-web",
    "func-orders-ingest",
    "plan-orders-dev",
}  # the objects with something wrong


def group_of(name):
    return RG[(APPS.get(name) or PLANS[name])["env"]]


def site_path(name):
    return (
        f"/subscriptions/{SUB}/resourceGroups/{group_of(name)}/providers/Microsoft.Web/sites/{name}"
    )


def plan_path(name):
    return f"/subscriptions/{SUB}/resourceGroups/{group_of(name)}/providers/Microsoft.Web/serverfarms/{name}"


def site_body(name):
    app = APPS[name]
    env, bad = app["env"], name in BROKEN
    plan = PLANS[app["plan"]]
    location = LOC[env][1]
    func = "functionapp" in app["kind"]
    linux = "linux" in app["kind"]
    stopped = name == "app-orders-web"
    props = {
        "name": name,
        "state": "Stopped" if stopped else "Running",
        "hostNames": [f"{name}.azurewebsites.net"],
        "webSpace": f"{group_of(name)}-{location.replace(' ', '')}webspace"
        + ("-Linux" if linux else ""),
        "selfLink": f"https://waws-prod-stub-001.api.azurewebsites.windows.net:454/subscriptions/{SUB}/webspaces/"
        f"{group_of(name)}-{location.replace(' ', '')}webspace/sites/{name}",
        "repositorySiteName": name,
        "owner": None,
        "usageState": "Normal",
        "enabled": True,
        "adminEnabled": True,
        "afdEnabled": False,
        "enabledHostNames": [f"{name}.azurewebsites.net", f"{name}.scm.azurewebsites.net"],
        "siteProperties": {
            "metadata": None,
            "properties": [{"name": "LinuxFxVersion", "value": ""}],
            "appSettings": None,
        },
        "availabilityState": "Limited" if stopped else "Normal",
        "sslCertificates": None,
        "csrs": [],
        "cers": None,
        "siteMode": None,
        "hostNameSslStates": [
            {
                "name": f"{name}.azurewebsites.net",
                "sslState": "Disabled",
                "ipBasedSslState": "NotConfigured",
                "hostType": "Standard",
            },
            {
                "name": f"{name}.scm.azurewebsites.net",
                "sslState": "Disabled",
                "ipBasedSslState": "NotConfigured",
                "hostType": "Repository",
            },
        ],
        "computeMode": None,
        "serverFarm": None,
        "serverFarmId": plan_path(app["plan"]),
        "reserved": linux,
        "isXenon": False,
        "hyperV": False,
        "lastModifiedTimeUtc": "2026-11-03T14:07:31.4166667" if bad else "2026-10-30T09:12:44.31",
        "storageRecoveryDefaultState": "Running",
        "contentAvailabilityState": "Normal",
        "runtimeAvailabilityState": "Normal",
        "dnsConfiguration": {},
        "vnetRouteAllEnabled": False,
        "vnetImagePullEnabled": False,
        "vnetContentShareEnabled": False,
        "siteConfig": {
            "numberOfWorkers": 1,
            "defaultDocuments": None,
            "netFrameworkVersion": None if linux else "v4.0",
            "linuxFxVersion": "NODE|20-lts"
            if linux and not func
            else ("DOTNET-ISOLATED|8.0" if linux else ""),
            "windowsFxVersion": None,
            "alwaysOn": None,
            "appSettings": None,
            "connectionStrings": None,
            "machineKey": None,
        },
        "deploymentId": name,
        "trafficManagerHostNames": None,
        "scmSiteAlsoStopped": False,
        "targetSwapSlot": None,
        "hostingEnvironmentProfile": None,
        "clusterSettings": None,
        "siteDisabledReason": None,
        "clientAffinityEnabled": not func and not linux,
        "clientCertEnabled": False,
        "clientCertMode": "Required",
        "hostNamesDisabled": False,
        "ipMode": "IPv4",
        "inboundIpAddress": "203.0.113.10",
        "possibleInboundIpAddresses": "203.0.113.10",
        "outboundIpAddresses": "203.0.113.20,203.0.113.21",
        "possibleOutboundIpAddresses": "203.0.113.20,203.0.113.21,203.0.113.22",
        "containerSize": 1536 if func else 0,
        "dailyMemoryTimeQuota": 0,
        "suspendedTill": None,
        "maxNumberOfWorkers": None,
        "homeStamp": "waws-prod-stub-001",
        "cloningInfo": None,
        "resourceGroup": group_of(name),
        "isDefaultContainer": None,
        "defaultHostName": f"{name}.azurewebsites.net",
        "slotSwapStatus": None,
        "httpsOnly": not bad,
        "redundancyMode": "None",
        "inProgressOperationId": None,
        "geoDistributions": None,
        "privateEndpointConnections": [],
        "publicNetworkAccess": None,
        "storageAccountRequired": False,
        "virtualNetworkSubnetId": None,
        "keyVaultReferenceIdentity": "SystemAssigned",
        "sku": plan["sku"][1],
    }
    body = {
        "id": site_path(name),
        "name": name,
        "type": "Microsoft.Web/sites",
        "kind": app["kind"],
        "location": location,
        "tags": {"env": app["env"], "team": "orders"},
        "properties": props,
    }
    if not bad:
        body["identity"] = {"type": "SystemAssigned", "tenantId": TENANT, "principalId": PRINCIPAL}
    return body


def config_body(name):
    app = APPS[name]
    bad = name in BROKEN
    func = "functionapp" in app["kind"]
    linux = "linux" in app["kind"]
    return {
        "id": site_path(name) + "/config/web",
        "name": name,
        "type": "Microsoft.Web/sites/config",
        "location": LOC[app["env"]][1],
        "tags": {"env": app["env"], "team": "orders"},
        "properties": {
            "numberOfWorkers": 1,
            "defaultDocuments": ["Default.htm", "index.html"],
            "netFrameworkVersion": None if linux else "v4.0",
            "linuxFxVersion": ("DOTNET-ISOLATED|8.0" if func else "NODE|20-lts") if linux else "",
            "windowsFxVersion": None,
            "requestTracingEnabled": False,
            "remoteDebuggingEnabled": bad,
            "remoteDebuggingVersion": "VS2022",
            "httpLoggingEnabled": False,
            "logsDirectorySizeLimit": 35,
            "detailedErrorLoggingEnabled": False,
            "publishingUsername": f"${name}",
            "appSettings": None,
            "metadata": None,
            "connectionStrings": None,
            "machineKey": None,
            "handlerMappings": None,
            "documentRoot": None,
            "scmType": "None",
            "use32BitWorkerProcess": bad,
            "webSocketsEnabled": False,
            "alwaysOn": False if (bad or func) else True,
            "managedPipelineMode": "Integrated",
            "virtualApplications": [
                {"virtualPath": "/", "physicalPath": "site\\wwwroot", "preloadEnabled": False}
            ],
            "loadBalancing": "LeastRequests",
            "autoHealEnabled": False,
            "vnetRouteAllEnabled": False,
            "ipSecurityRestrictions": [
                {
                    "ipAddress": "Any",
                    "action": "Allow",
                    "priority": 2147483647,
                    "name": "Allow all",
                    "description": "Allow all access",
                }
            ],
            "scmIpSecurityRestrictions": [
                {
                    "ipAddress": "Any",
                    "action": "Allow",
                    "priority": 2147483647,
                    "name": "Allow all",
                    "description": "Allow all access",
                }
            ],
            "scmIpSecurityRestrictionsUseMain": False,
            "http20Enabled": not bad,
            "minTlsVersion": "1.0" if bad else "1.2",
            "scmMinTlsVersion": "1.0" if bad else "1.2",
            "ftpsState": "AllAllowed" if bad else "FtpsOnly",
            "preWarmedInstanceCount": 0,
            "healthCheckPath": None if bad else "/healthz",
            "functionsRuntimeScaleMonitoringEnabled": False,
            "azureStorageAccounts": {},
        },
    }


def plan_body(name):
    plan = PLANS[name]
    sku, tier, size, family, capacity = plan["sku"]
    bad = name in BROKEN
    env = plan["env"]
    return {
        "id": plan_path(name),
        "name": name,
        "type": "Microsoft.Web/serverfarms",
        "kind": plan["kind"],
        "location": LOC[env][1],
        "tags": {"env": env, "team": "orders"},
        "sku": {"name": sku, "tier": tier, "size": size, "family": family, "capacity": capacity},
        "properties": {
            "serverFarmId": 41873 if bad else 41872,
            "name": name,
            "workerSize": "Default",
            "workerSizeId": 0,
            "workerTierName": None,
            "numberOfWorkers": capacity,
            "currentWorkerSize": "Default",
            "currentWorkerSizeId": 0,
            "currentNumberOfWorkers": capacity,
            "status": "Ready",
            "webSpace": f"{RG[env]}-{LOC[env][1].replace(' ', '')}webspace"
            + ("-Linux" if plan["kind"] == "linux" else ""),
            "subscription": SUB,
            "adminSiteName": None,
            "hostingEnvironmentProfile": None,
            "maximumNumberOfWorkers": plan["max"],
            "geoRegion": LOC[env][1],
            "perSiteScaling": False,
            "elasticScaleEnabled": False,
            "maximumElasticWorkerCount": plan["max"],
            "numberOfSites": plan["sites"],
            "isSpot": False,
            "resourceGroup": RG[env],
            "reserved": plan["kind"] == "linux",
            "isXenon": False,
            "hyperV": False,
            "targetWorkerCount": 0,
            "targetWorkerSizeId": 0,
            "provisioningState": "Succeeded",
            "zoneRedundant": False,
        },
    }


def slots_body(name):
    bad = name in BROKEN
    app = APPS[name]
    row = {
        "id": site_path(name) + "/slots/staging",
        "name": f"{name}/staging",
        "type": "Microsoft.Web/sites/slots",
        "kind": app["kind"],
        "location": LOC[app["env"]][1],
        "properties": {
            "name": f"{name}/staging",
            "state": "Stopped" if bad else "Running",
            "availabilityState": "Normal",
            "enabled": True,
            "defaultHostName": f"{name}-staging.azurewebsites.net",
            "lastModifiedTimeUtc": "2026-11-03T13:58:02.1",
            "serverFarmId": plan_path(app["plan"]),
            "slotSwapStatus": None,
            "targetSwapSlot": None,
        },
    }
    return {"value": [row]}


def deployments_body(name):
    base = site_path(name) + "/deployments/"

    def rec(sha, status, start, end, active, message, deployer="GitHub"):
        return {
            "id": base + sha,
            "name": sha,
            "type": "Microsoft.Web/sites/deployments",
            "properties": {
                "id": sha,
                "status": status,
                "author": "Deploy Bot",
                "deployer": deployer,
                "message": message,
                "start_time": start,
                "end_time": end,
                "active": active,
                "details": None,
            },
        }

    if name in BROKEN:
        rows = [
            rec(
                "c3f1d9a07b2e4a55b8f6e1d2c0a9b8776655aa44",
                3,
                "2026-11-03T14:02:11.5Z",
                "2026-11-03T14:05:40.2Z",
                False,
                "Merge pull request: change order retry policy",
            ),
            rec(
                "8e2b7d4f9c1a4036a5d8e7f6b5c4d3a2f1e0b9c8",
                4,
                "2026-11-02T09:30:00.1Z",
                "2026-11-02T09:33:12.8Z",
                True,
                "Bump base image to node 20.18",
            ),
            rec(
                "1a2b3c4d5e6f40718293a4b5c6d7e8f901234567",
                4,
                "2026-10-27T16:12:44.9Z",
                "2026-10-27T16:15:01.0Z",
                False,
                "Fix health endpoint",
            ),
            rec(
                "0f9e8d7c6b5a4938271605f4e3d2c1b0a9f8e7d6",
                1,
                "2026-11-03T14:20:00.0Z",
                None,
                False,
                "Rebuild after retry-policy revert",
                "ZipDeploy",
            ),
        ]
    else:
        rows = [
            rec(
                "5d4c3b2a190807f6e5d4c3b2a1908f7e6d5c4b3a",
                4,
                "2026-11-03T08:15:20.3Z",
                "2026-11-03T08:17:02.6Z",
                True,
                "Release 2026.11.03",
            ),
            rec(
                "a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e",
                4,
                "2026-10-29T10:01:10.0Z",
                "2026-10-29T10:02:48.4Z",
                False,
                "Release 2026.10.29",
            ),
        ]
    return {"value": rows}


def autoscale_setting(group, plan):
    """The autoscale setting of one plan, as a resource of `group` (a setting can be in any group)."""
    env = PLANS[plan]["env"]
    low, high, default = ("2", "3", "2") if env == "dev" else ("2", "5", "2")
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Insights/autoscalesettings/autoscale-{plan}",
        "name": f"autoscale-{plan}",
        "type": "Microsoft.Insights/autoscaleSettings",
        "location": LOC[env][0],
        "properties": {
            "name": f"autoscale-{plan}",
            "enabled": True,
            "targetResourceUri": plan_path(plan),
            "targetResourceLocation": LOC[env][0],
            "profiles": [
                {
                    "name": "Default",
                    "capacity": {"minimum": low, "maximum": high, "default": default},
                    "rules": [
                        {
                            "metricTrigger": {
                                "metricName": "CpuPercentage",
                                "metricResourceUri": plan_path(plan),
                                "timeGrain": "PT1M",
                                "statistic": "Average",
                                "timeWindow": "PT10M",
                                "timeAggregation": "Average",
                                "operator": "GreaterThan",
                                "threshold": 70,
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
                                "metricName": "CpuPercentage",
                                "metricResourceUri": plan_path(plan),
                                "timeGrain": "PT1M",
                                "statistic": "Average",
                                "timeWindow": "PT10M",
                                "timeAggregation": "Average",
                                "operator": "LessThan",
                                "threshold": 30,
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
            "predictiveAutoscalePolicy": {"scaleMode": "Disabled"},
        },
    }


def autoscale_body():
    """The autoscale settings of the subscription: the prod plan's (in its own group), the dev plan's
    (kept in RG-Platform, not in the plan's group) and one that targets something that is no plan."""
    other = autoscale_setting("RG-Platform", "plan-orders-prod")
    other["id"] = other["id"].replace("autoscale-plan-orders-prod", "autoscale-vmss-batch")
    other["name"] = "autoscale-vmss-batch"
    other["properties"]["name"] = "autoscale-vmss-batch"
    other["properties"]["targetResourceUri"] = (
        f"/subscriptions/{SUB}/resourceGroups/RG-Platform/providers/Microsoft.Compute/virtualMachineScaleSets/vmss-batch"
    )
    return {
        "value": [
            autoscale_setting(RG["prod"], "plan-orders-prod"),
            autoscale_setting("RG-Platform", "plan-orders-dev"),
            other,
        ]
    }


def health_body(name, kind):
    path = (
        site_path(name) if kind == "site" else plan_path(name)
    ) + "/providers/Microsoft.ResourceHealth/availabilityStatuses/current"
    env = (APPS.get(name) or PLANS[name])["env"]
    noun = "App Service app" if kind == "site" else "App Service plan"
    props = {
        "availabilityState": "Available",
        "title": "Available",
        "summary": f"There aren't any known Azure platform problems affecting this {noun}.",
        "detailedStatus": "",
        "reasonType": "",
        "context": "",
        "category": "",
        "reasonChronicity": "Persistent",
        "occurredTime": "2026-10-29T08:00:00Z",
        "reportedTime": "2026-11-04T00:30:00.0000000Z",
        "serviceImpactingEvents": [],
    }
    if name in BROKEN:
        props.update(
            availabilityState="Degraded",
            title="Degraded",
            summary=f"We are investigating reduced availability for {noun}s in West Europe. Some requests may fail or take longer than usual.",
            detailedStatus="Reduced availability (platform event 7XQ1-9Z0).",
            reasonType="Unplanned",
            context="Azure initiated",
            category="Unplanned",
            reasonChronicity="Transient",
            occurredTime="2026-11-03T21:40:00Z",
            reportedTime="2026-11-03T21:44:12.1250000Z",
            serviceImpactingEvents=[
                {
                    "eventStartTime": "2026-11-03T21:40:00Z",
                    "eventStatusLastModifiedTime": "2026-11-04T00:10:00Z",
                    "correlationId": EVENT,
                    "status": {"value": "Active"},
                    "incidentProperties": {
                        "title": f"{noun} - West Europe - Reduced availability",
                        "service": "App Service",
                        "region": "West Europe",
                        "incidentType": "Incident",
                    },
                }
            ],
        )
    return {
        "id": path,
        "location": LOC[env][0],
        "name": "current",
        "properties": props,
        "type": "Microsoft.ResourceHealth/AvailabilityStatuses",
    }


# ---------------------------------------------------------------------------------------------------
# metrics: 24 hourly grains ending 2026-11-04T03:00Z (the recordings are made on 2026-11-04)
# ---------------------------------------------------------------------------------------------------
GRAINS = [f"2026-11-{3 + (h + 4) // 24:02d}T{(h + 4) % 24:02d}:00:00Z" for h in range(24)]
DISPLAY = {
    "Requests": ("Requests", "Count"),
    "Http5xx": ("Http Server Errors", "Count"),
    "Http4xx": ("Http 4xx", "Count"),
    "HttpResponseTime": ("Response Time", "Seconds"),
    "FunctionExecutionCount": ("Function Execution Count", "Count"),
    "FunctionExecutionUnits": ("Function Execution Units", "Count"),
    "CpuPercentage": ("CPU Percentage", "Percent"),
    "MemoryPercentage": ("Memory Percentage", "Percent"),
    "DiskQueueLength": ("Disk Queue Length", "Count"),
}


def series(name, metric, aggs):
    """The data points of one metric of one object: {grain: {aggregation: value | None}}."""
    bad = name in BROKEN
    spike = {19, 20, 21}  # three hours of trouble
    rows = []
    for i in range(24):
        v = {}
        if metric == "Requests":
            v["total"] = (
                1200.0 + 15 * i if not bad else 380.0 + (10 * i if i not in spike else -120)
            )
        elif metric == "Http5xx":
            v["total"] = 0.0 if not bad else (210.0 + 15 * (i - 19) if i in spike else 2.0)
        elif metric == "Http4xx":
            v["total"] = 4.0 + (i % 3) if not bad else (140.0 if i in spike else 9.0)
        elif metric == "HttpResponseTime":
            avg, mx = (
                (0.21 + 0.01 * (i % 4), 0.9)
                if not bad
                else ((7.8, 21.5) if i in spike else (1.2, 3.1))
            )
            v["average"], v["maximum"] = avg, mx
        elif metric == "FunctionExecutionCount":
            v["total"] = 960.0 if not bad else (0.0 if i in (7, 8) else 410.0 + 3 * i)
            if bad and i in (11, 12, 23):
                v = {}  # Azure Monitor had no datapoint for these grains
        elif metric == "FunctionExecutionUnits":
            v["total"] = 52000000.0 if not bad else 71000000.0 + 40000 * i
            if bad and i in (11, 12, 23):
                v = {}
        elif metric == "CpuPercentage":
            avg, mx = (
                (22.0 + (i % 5), 41.0) if not bad else (91.0 + (i % 4) if i >= 10 else 64.0, 99.0)
            )
            v["average"], v["maximum"] = avg, mx
        elif metric == "MemoryPercentage":
            avg, mx = (46.0 + (i % 3), 52.0) if not bad else (86.0 + (i % 3), 93.0)
            v["average"], v["maximum"] = avg, mx
        elif metric == "DiskQueueLength":
            v["average"] = 0.1 if not bad else (14.0 if i == 20 else 0.4)
        point = {"timeStamp": GRAINS[i]}
        for key, value in v.items():
            if key in {a.lower() for a in aggs}:
                point[key] = round(value, 4)
        rows.append(point)
    return rows


AAZ_FIELDS = {  # azure/cli/command_modules/monitor/aaz/latest/monitor/metrics/_list.py, _build_schema_on_200
    "top": {"cost", "interval", "namespace", "resourceregion", "timespan", "value"},
    "value": {
        "displayDescription",
        "errorCode",
        "errorMessage",
        "id",
        "name",
        "timeseries",
        "type",
        "unit",
    },
    "data": {"average", "count", "maximum", "minimum", "timeStamp", "total"},
}


def metrics_body(resource_id, name, metric, aggs, namespace):
    display, unit = DISPLAY[metric]
    body = {
        "cost": 59,
        "timespan": f"{GRAINS[0]}/2026-11-04T04:00:00Z",
        "interval": "PT1H",
        "value": [
            {
                "id": f"{resource_id}/providers/Microsoft.Insights/metrics/{metric}",
                "type": "Microsoft.Insights/metrics",
                "name": {"value": metric, "localizedValue": display},
                "displayDescription": display,
                "unit": unit,
                "timeseries": [{"metadatavalues": [], "data": series(name, metric, aggs)}],
                "errorCode": "Success",
            }
        ],
        "namespace": namespace,
        "resourceregion": LOC[(APPS.get(name) or PLANS[name])["env"]][0],
    }
    assert set(body) <= AAZ_FIELDS["top"]
    for entry in body["value"]:
        assert set(entry) <= AAZ_FIELDS["value"]
        for point in entry["timeseries"][0]["data"]:
            assert set(point) <= AAZ_FIELDS["data"], point
    return body


# ---------------------------------------------------------------------------------------------------
# running the real az
# ---------------------------------------------------------------------------------------------------
class Fake(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):  # noqa: N802
        code, body = self.routes[self.path.split("?")[0]]
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Rest:
    """`az rest` against a local server: the reply text exactly as az prints it."""

    def __init__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = dict(
            os.environ,
            AZURE_CONFIG_DIR="/tmp/appservice-capture-azure",
            AZURE_CORE_COLLECT_TELEMETRY="false",
        )
        self.cache = {}

    def run(self, code, body):
        text = json.dumps(body, separators=(",", ":")) if not isinstance(body, str) else body
        key = hashlib.sha256(f"{code}{text}".encode()).hexdigest()[:16]
        if key not in self.cache:
            Fake.routes[f"/{key}"] = (code, text)
            port = self.server.server_address[1]
            done = subprocess.run(
                ["az", "rest", "--method", "get", "--url", f"http://127.0.0.1:{port}/{key}?api-version=2024-04-01",
                 "--skip-authorization-header", "--output", "json"],
                capture_output=True, text=True, env=self.env, check=False,
            )  # fmt: skip
            self.cache[key] = (done.returncode, done.stdout, done.stderr)
        return self.cache[key]

    def ok(self, body):
        rc, out, err = self.run(200, body)
        assert rc == 0 and out and not err, (rc, err)
        return out

    def error(self, code, body):
        rc, out, err = self.run(code, body)
        assert rc == 1 and not out and err.startswith("ERROR: "), (rc, err)
        return err


def cli_json(models):
    """What `az ... --output json` prints for an SDK model or a list of them (as capture/render.py)."""
    from azure.cli.core.commands import AzCliCommandInvoker
    from azure.cli.core.commands.transform import _add_resource_group
    from azure.cli.core.util import todict

    result = todict(models, AzCliCommandInvoker.remove_additional_prop_layer)
    _add_resource_group(result)
    return json.dumps(result, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def listings(group):
    """(plans, web apps, function apps) of a group as the CLI's own list functions print them."""
    from azure.cli.command_modules.appservice import custom
    from azure.mgmt.web import models as wm

    sites = [wm.Site(site_body(n)) for n in APPS if group_of(n).lower() == group.lower()]
    plans = [wm.AppServicePlan(plan_body(n)) for n in PLANS if group_of(n).lower() == group.lower()]
    client = SimpleNamespace(
        web_apps=SimpleNamespace(list_by_resource_group=lambda g: list(sites)),
        app_service_plans=SimpleNamespace(
            list_by_resource_group=lambda g: [copy.deepcopy(p) for p in plans]
        ),
    )
    cmd = SimpleNamespace(cli_ctx=None)
    with mock.patch.object(custom, "web_client_factory", lambda *a, **k: client):
        webapps = custom.list_webapp(cmd, group)
        functions = custom.list_function_app(cmd, group)
        listed_plans = custom.list_app_service_plans(cmd, group)
    return cli_json(listed_plans), cli_json(webapps), cli_json(functions)


def plan_update_output(name):
    from azure.mgmt.web import models as wm

    return cli_json(wm.AppServicePlan(plan_body(name)))


# ---------------------------------------------------------------------------------------------------
# error texts
# ---------------------------------------------------------------------------------------------------
def denied(action, scope):
    msg = (
        f"The client '{PRINCIPAL}' with object id '{PRINCIPAL}' does not have authorization to perform action "
        f"'{action}' over scope '{scope}' or the scope is invalid. If access was recently granted, please refresh your credentials."
    )
    return f"ERROR: (AuthorizationFailed) {msg}\nCode: AuthorizationFailed\nMessage: {msg}\n"


def throttled_text():
    msg = (
        "The request is being throttled as the limit has been reached for operation type - 'Read'."
    )
    return f"ERROR: (TooManyRequests) {msg} For more information, see https://aka.ms/srpthrottlinglimits. Please try again later.\nCode: TooManyRequests\nMessage: {msg}\n"


def rest_denied(action, scope):
    return {
        "error": {
            "code": "AuthorizationFailed",
            "message": f"The client '{PRINCIPAL}' with object id '{PRINCIPAL}' does not have authorization to perform action '{action}' over scope '{scope}' or the scope is invalid.",
        }
    }


def rest_not_found(provider_type, name, group):
    return {
        "error": {
            "code": "ResourceNotFound",
            "message": f"The Resource '{provider_type}/{name}' under resource group '{group}' was not found. For more details please go to https://aka.ms/ARMResourceNotFoundFix",
        }
    }


def rest_throttled():
    return {
        "error": {
            "code": "TooManyRequests",
            "message": "Number of 'read' requests for subscription exceeded the limit. Please retry later.",
        }
    }


# ---------------------------------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------------------------------
class Scenario:
    def __init__(self, name, header, out):
        self.name, self.header, self.dir = name, header, os.path.join(out, name)
        self.calls, self.files = [], {}

    def add(self, argv, source, *, stdout=None, filename=None, stderr=None, rc=0):
        call = {"argv": argv}
        if stdout is not None:
            self.files[filename] = stdout
            call["stdout"] = filename
        if stderr is not None:
            call["stderr"] = stderr
        call["source"] = source
        call["rc"] = rc
        self.calls.append(call)

    def write(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        for filename, text in self.files.items():
            with open(os.path.join(self.dir, filename), "w", encoding="utf-8") as handle:
                handle.write(text)
        write_calls(os.path.join(self.dir, "calls.yaml"), self.header, self.calls)


def write_calls(path, header, calls):
    lines = [f"# {line}" for line in header]
    lines.append(
        "# Format: JSON written as YAML; `<ts>` in an argv entry matches a timestamp (RFC 3339 UTC, whole seconds). See ../../README.md."
    )
    body = ",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in calls)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + '\n{\n  "calls": [\n' + body + "\n  ]\n}\n")


def rest_argv(path, api="2024-04-01"):
    return ["rest", "--method", "get", "--url", f"{path}?api-version={api}"]


def metric_argv(resource_id, metric, aggs):
    return ["monitor", "metrics", "list", "--resource", resource_id, "--metrics", metric, "--aggregation", *aggs,
            "--interval", "PT1H", "--start-time", "<ts>", "--end-time", "<ts>", "--subscription", SUB]  # fmt: skip


WEB_METRICS = (
    ("Requests", ["Total"]),
    ("Http5xx", ["Total"]),
    ("Http4xx", ["Total"]),
    ("HttpResponseTime", ["Average", "Maximum"]),
)
FUNC_METRICS = (
    ("FunctionExecutionCount", ["Total"]),
    ("FunctionExecutionUnits", ["Total"]),
    ("Http5xx", ["Total"]),
)
PLAN_METRICS = (
    ("CpuPercentage", ["Average", "Maximum"]),
    ("MemoryPercentage", ["Average", "Maximum"]),
    ("DiskQueueLength", ["Average"]),
)
GROUP_ARGS = lambda name: ["-g", group_of(name), "-n", name, "--subscription", SUB]  # noqa: E731


def reads(sc, rest, names, *, with_metrics=True):
    """Every read the skill makes of the given objects, answered with their data."""
    for name in names:
        if name in APPS:
            sc.add(
                rest_argv(site_path(name)),
                REF["site"],
                stdout=rest.ok(site_body(name)),
                filename=f"site-{name}.json",
            )
            sc.add(
                rest_argv(site_path(name) + "/config/web"),
                REF["config"],
                stdout=rest.ok(config_body(name)),
                filename=f"config-{name}.json",
            )
            if "functionapp" not in APPS[name]["kind"]:
                sc.add(
                    rest_argv(site_path(name) + "/slots"),
                    REF["slots"],
                    stdout=rest.ok(slots_body(name)),
                    filename=f"slots-{name}.json",
                )
                sc.add(
                    rest_argv(site_path(name) + "/deployments"),
                    REF["deployments"],
                    stdout=rest.ok(deployments_body(name)),
                    filename=f"deployments-{name}.json",
                )
            else:
                sc.add(
                    rest_argv(site_path(name) + "/slots"),
                    REF["slots"],
                    stdout=rest.ok({"value": []}),
                    filename=f"slots-{name}.json",
                )
                sc.add(
                    rest_argv(site_path(name) + "/deployments"),
                    REF["deployments"],
                    stdout=rest.ok({"value": []}),
                    filename=f"deployments-{name}.json",
                )
            sc.add(rest_argv(site_path(name) + "/providers/Microsoft.ResourceHealth/availabilityStatuses/current", "2022-10-01"), REF["health"],
                   stdout=rest.ok(health_body(name, "site")), filename=f"health-{name}.json")  # fmt: skip
            if with_metrics:
                specs = FUNC_METRICS if "functionapp" in APPS[name]["kind"] else WEB_METRICS
                for metric, aggs in specs:
                    sc.add(metric_argv(site_path(name), metric, aggs), REF["metrics"],
                           stdout=json.dumps(metrics_body(site_path(name), name, metric, aggs, "Microsoft.Web/sites"), indent=2, sort_keys=True) + "\n",
                           filename=f"metrics-{name}-{metric}.json")  # fmt: skip
        else:
            sc.add(
                rest_argv(plan_path(name)),
                REF["plan"],
                stdout=rest.ok(plan_body(name)),
                filename=f"plan-{name}.json",
            )
            sc.add(rest_argv(plan_path(name) + "/providers/Microsoft.ResourceHealth/availabilityStatuses/current", "2022-10-01"), REF["health"],
                   stdout=rest.ok(health_body(name, "plan")), filename=f"health-plan-{name}.json")  # fmt: skip
            if with_metrics:
                for metric, aggs in PLAN_METRICS:
                    sc.add(metric_argv(plan_path(name), metric, aggs), REF["metrics"],
                           stdout=json.dumps(metrics_body(plan_path(name), name, metric, aggs, "Microsoft.Web/serverfarms"), indent=2, sort_keys=True) + "\n",
                           filename=f"metrics-{name}-{metric}.json")  # fmt: skip
    if any(n in PLANS for n in names):
        autoscale_call(sc, rest)


def autoscale_call(sc, rest):
    sc.add(rest_argv(f"/subscriptions/{SUB}/providers/Microsoft.Insights/autoscalesettings", "2022-10-01"), REF["autoscale"],
           stdout=rest.ok(autoscale_body()), filename="autoscale-subscription.json")  # fmt: skip


def writes(sc, names, rest):
    for name in names:
        if name in APPS:
            for verb in ("restart", "start", "stop"):
                sc.add(["webapp", verb, *GROUP_ARGS(name)], REF[verb])
            sc.add(
                [
                    "webapp",
                    "deployment",
                    "slot",
                    "swap",
                    "-g",
                    group_of(name),
                    "-n",
                    name,
                    "--slot",
                    "staging",
                    "--target-slot",
                    "production",
                    "--subscription",
                    SUB,
                ],
                REF["swap"],
            )
        else:
            plan = PLANS[name]
            argv = [
                "appservice",
                "plan",
                "update",
                "-g",
                group_of(name),
                "-n",
                name,
                "--subscription",
                SUB,
            ]
            sc.add(argv + ["--number-of-workers", str(plan["sku"][4]), "--sku", plan["sku"][0]], REF["update"],
                   stdout=plan_update_output(name), filename=f"plan-update-{name}.json")  # fmt: skip


def build(out):
    rest = Rest()
    prod = ["app-orders-api", "func-orders-jobs", "plan-orders-prod"]
    dev = ["app-orders-web", "func-orders-ingest", "plan-orders-dev"]

    sc = Scenario("appservice-healthy", [
        "appservice-healthy: the prod web app app-orders-api, the function app func-orders-jobs and plan-orders-prod are fine.",
        "Writes are accepted and change nothing (the stub is stateless), so a recorded write finds the object as it was.",
    ], out)  # fmt: skip
    reads(sc, rest, prod)
    writes(sc, ["app-orders-api", "plan-orders-prod"], rest)
    sc.write()

    sc = Scenario("appservice-broken", [
        "appservice-broken: app-orders-web is stopped with a failed deployment, 5xx and weak TLS settings; func-orders-ingest has missing",
        "grains and a 5xx spike; plan-orders-dev is starved (CPU, memory) at its autoscale maximum. Resource Health is Degraded.",
    ], out)  # fmt: skip
    reads(sc, rest, dev)
    writes(sc, ["app-orders-web"], rest)
    sc.write()

    for name, header, make in (
        (
            "appservice-forbidden",
            "no role assignment: every ARM call is AuthorizationFailed.",
            "forbidden",
        ),
        ("appservice-throttled", "ARM answers 429 to every call.", "throttled"),
    ):
        sc = Scenario(
            name, [f"{name}: {header}", "Targets app-orders-web and plan-orders-dev."], out
        )
        for obj in ("app-orders-web", "func-orders-ingest", "plan-orders-dev"):
            path = site_path(obj) if obj in APPS else plan_path(obj)
            action = "Microsoft.Web/sites/read" if obj in APPS else "Microsoft.Web/serverfarms/read"
            if make == "forbidden":
                err = rest.error(403, rest_denied(action, path))
            else:
                err = rest.error(429, rest_throttled())
            sc.add(rest_argv(path), REF["site"] if obj in APPS else REF["plan"], stderr=err, rc=1)
        sc.write()

    sc = Scenario(
        "appservice-not-found",
        [
            "appservice-not-found: the web app `nope` and the plan `nope` do not exist in rg-orders-dev."
        ],
        out,
    )
    group = RG["dev"]
    for kind, typ, ref in (
        ("sites", "Microsoft.Web/sites", "site"),
        ("serverfarms", "Microsoft.Web/serverfarms", "plan"),
    ):
        path = f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Web/{kind}/nope"
        sc.add(
            rest_argv(path),
            REF[ref],
            stderr=rest.error(404, rest_not_found(typ, "nope", group)),
            rc=1,
        )
    sc.write()

    # the write is refused, the read is not: Reader without the write action
    sc = Scenario(
        "appservice-write-forbidden",
        [
            "appservice-write-forbidden: Reader on rg-orders-dev: the reads work, every write is AuthorizationFailed."
        ],
        out,
    )
    app, plan = "app-orders-web", "plan-orders-dev"
    sc.add(
        rest_argv(site_path(app)),
        REF["site"],
        stdout=rest.ok(site_body(app)),
        filename=f"site-{app}.json",
    )
    sc.add(
        rest_argv(site_path(app) + "/slots"),
        REF["slots"],
        stdout=rest.ok(slots_body(app)),
        filename=f"slots-{app}.json",
    )
    sc.add(
        rest_argv(plan_path(plan)),
        REF["plan"],
        stdout=rest.ok(plan_body(plan)),
        filename=f"plan-{plan}.json",
    )
    autoscale_call(sc, rest)
    for verb in ("restart", "start", "stop"):
        scope = site_path(app)
        sc.add(
            ["webapp", verb, *GROUP_ARGS(app)],
            REF[verb],
            stderr=denied(f"Microsoft.Web/sites/{verb}/action", scope),
            rc=1,
        )
    sc.add(["webapp", "deployment", "slot", "swap", "-g", RG["dev"], "-n", app, "--slot", "staging", "--target-slot", "production", "--subscription", SUB], REF["swap"],
           stderr=denied("Microsoft.Web/sites/slots/slotsswap/action", site_path(app) + "/slots/staging"), rc=1)  # fmt: skip
    sc.add(["appservice", "plan", "update", "-g", RG["dev"], "-n", plan, "--subscription", SUB, "--number-of-workers", "3", "--sku", "S3"], REF["update"],
           stderr=denied("Microsoft.Web/serverfarms/write", plan_path(plan)), rc=1)  # fmt: skip
    sc.write()

    # a swap with preview is under way on the prod app: slotSwapStatus is set on the site
    sc = Scenario(
        "appservice-swap-busy",
        ["appservice-swap-busy: app-orders-api has a slot swap under way (slotSwapStatus is set on the site and the slot)."],
        out,
    )  # fmt: skip
    busy = site_body("app-orders-api")
    busy["properties"]["slotSwapStatus"] = {
        "timestampUtc": "2026-11-04T03:41:12.5Z",
        "sourceSlotName": "staging",
        "destinationSlotName": "production",
    }
    sc.add(
        rest_argv(site_path("app-orders-api")),
        REF["site"],
        stdout=rest.ok(busy),
        filename="site-app-orders-api.json",
    )
    busy_slots = slots_body("app-orders-api")
    busy_slots["value"][0]["properties"]["slotSwapStatus"] = busy["properties"]["slotSwapStatus"]
    busy_slots["value"][0]["properties"]["targetSwapSlot"] = "production"
    sc.add(
        rest_argv(site_path("app-orders-api") + "/slots"),
        REF["slots"],
        stdout=rest.ok(busy_slots),
        filename="slots-app-orders-api.json",
    )
    sc.write()

    # partial: the site is readable, parts of it are not
    sc = Scenario(
        "appservice-partial",
        [
            "appservice-partial: app-orders-web is readable, but its web configuration, its slots and the Http4xx metric are refused."
        ],
        out,
    )
    sc.add(
        rest_argv(site_path(app)),
        REF["site"],
        stdout=rest.ok(site_body(app)),
        filename=f"site-{app}.json",
    )
    sc.add(
        rest_argv(site_path(app) + "/config/web"),
        REF["config"],
        stderr=rest.error(
            403, rest_denied("Microsoft.Web/sites/config/read", site_path(app) + "/config/web")
        ),
        rc=1,
    )
    sc.add(
        rest_argv(site_path(app) + "/slots"),
        REF["slots"],
        stderr=rest.error(
            403, rest_denied("Microsoft.Web/sites/slots/read", site_path(app) + "/slots")
        ),
        rc=1,
    )
    sc.add(
        rest_argv(site_path(app) + "/deployments"),
        REF["deployments"],
        stdout=rest.ok(deployments_body(app)),
        filename=f"deployments-{app}.json",
    )
    for metric, aggs in WEB_METRICS:
        if metric == "Http4xx":
            sc.add(
                metric_argv(site_path(app), metric, aggs),
                REF["metrics"],
                stderr=denied("Microsoft.Insights/metrics/read", site_path(app)),
                rc=1,
            )
        else:
            sc.add(metric_argv(site_path(app), metric, aggs), REF["metrics"],
                   stdout=json.dumps(metrics_body(site_path(app), app, metric, aggs, "Microsoft.Web/sites"), indent=2, sort_keys=True) + "\n",
                   filename=f"metrics-{app}-{metric}.json")  # fmt: skip
    sc.write()

    discover(out, rest)


BEGIN = "    # --- azure-appservice-health: captured from the pinned az by capture/appservice.py; do not edit by hand ---\n"
END = "    # --- end azure-appservice-health ---\n"
MINE = (["appservice", "plan"], ["webapp", "list"], ["functionapp", "list"])


def discover(out, rest):
    """azure-base/discover's plan, web app and function app listings, put into the scenarios it records
    against between two marker comments at the top of the call list (the stub drops `#` lines), the
    way capture/splice_aks.py does for the AKS listings: a second run replaces the block and touches
    nothing else. Every group's listings work, also in group-forbidden, where only the Key Vault
    listing of rg-orders-dev is refused."""
    groups = [RG["dev"], RG["prod"], "RG-Platform"]
    for name in ("healthy", "group-forbidden"):
        path = os.path.join(out, name, "calls.yaml")
        old = open(path, encoding="utf-8").read()
        if BEGIN in old:  # the previous block goes
            old = old[: old.index(BEGIN)] + old[old.index(END) + len(END) :]
        # lines of mine from before the markers existed
        old = "".join(
            line
            for line in old.splitlines(True)
            if not (
                line.startswith("    {")
                and json.loads(line.rstrip().rstrip(","))["argv"][:2] in MINE
            )
        )
        # the last call of the list has no comma; one that lost its successors keeps the old one
        kept = old.splitlines(True)
        last = max(i for i, line in enumerate(kept) if line.startswith("    {"))
        kept[last] = kept[last].rstrip().rstrip(",") + "\n"
        old = "".join(kept)
        lines = []
        for group in groups:
            slug = group.lower()
            plans, webapps, functions = listings(group)
            for argv, ref, text_out in (
                (["appservice", "plan", "list", "-g", group, "--subscription", SUB], "plan-list", plans),
                (["webapp", "list", "-g", group, "--subscription", SUB], "webapp-list", webapps),
                (["functionapp", "list", "-g", group, "--subscription", SUB], "functionapp-list", functions),
            ):  # fmt: skip
                filename = f"{ref}-{slug}.json"
                with open(os.path.join(out, name, filename), "w", encoding="utf-8") as handle:
                    handle.write(text_out)
                call = {"argv": argv, "stdout": filename, "source": REF[ref], "rc": 0}
                lines.append("    " + json.dumps(call, ensure_ascii=False) + ",\n")
        opening = '  "calls": [\n'
        at = old.index(opening) + len(opening)
        new = old[:at] + BEGIN + "".join(lines) + END + old[at:]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(new)


if __name__ == "__main__":
    build(sys.argv[1])
