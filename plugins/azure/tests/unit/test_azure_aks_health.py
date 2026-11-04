"""azure-aks-health against the stub scenarios and hand-made az replies: facts, bounds, every
failure as a code, and the permissions each task declares."""

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from azure_aks_health import activity, node_pools, version_support
from azure_aks_health.az import classify_error
from azure_aks_health.cluster import read_scan_limit, read_thresholds
from azure_aks_health.cluster_status import diagnostics
from azure_aks_health.common import read_settings
from conftest import SETTINGS, SUB, Replies, cluster

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "azure-aks-health"
STUB = PLUGIN / "tests" / "stub" / "scenarios"
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
PROD, DEV = "aks-orders-prod", "aks-orders-dev"
PROD_ID = f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.ContainerService/managedClusters/{PROD}"
DEV_ID = f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/Microsoft.ContainerService/managedClusters/{DEV}"
SHOW_DEV = ("aks", "show", "--name", DEV, "-g", "rg-orders-dev", "--subscription", SUB)
SHOW_PROD = ("aks", "show", "--name", PROD, "-g", "rg-orders-prod", "--subscription", SUB)

TASKS = {
    "resource-health": ("resource_health", "health"),
    "cluster-status": ("cluster_status", "cluster"),
    "node-pools": ("node_pools", "nodePools"),
    "version-support": ("version_support", "version"),
    "activity-failures": ("activity_failures", "activity"),
    "node-metrics": ("node_metrics", "metrics"),
}


def stub_json(scenario, name):
    return (STUB / scenario / name).read_text()


@pytest.fixture
def go(run_script):
    """go(task, scenario, cluster name or None for the scenario's default, **inputs)"""

    def run(task, scenario, name=None, **kw):
        script, _ = TASKS[task]
        name = name or (PROD if scenario == "healthy" else DEV)
        return run_script(
            "azure-aks-health", script, scenario=scenario, resource=cluster(name), now=NOW, **kw
        )

    return run


def out(run, task):
    return run.envelope["outputs"][TASKS[task][1]]


# --- schemas ----------------------------------------------------------------------------------


def doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def schema_of(task):
    ref = next(t for t in doc()["tasks"] if t["name"] == task)["outputs"][TASKS[task][1]]["schema"]
    return json.loads((SKILL / ref).read_text())


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize("scenario", ["healthy", "broken"])
def test_every_output_validates_against_its_schema(go, task, scenario):
    run = go(task, scenario)
    assert run.envelope["status"] == "ok", run.errors
    jsonschema.validate(out(run, task), schema_of(task))


@pytest.mark.parametrize("task", ["cluster-status", "node-pools", "node-metrics"])
def test_a_stopped_cluster_validates_too(go, task):
    run = go(task, "stopped")
    assert run.envelope["status"] == "ok", run.errors
    jsonschema.validate(out(run, task), schema_of(task))


@pytest.mark.parametrize("task", TASKS)
def test_each_output_schema_is_valid_2020_12_and_names_no_verdict(task):
    schema = schema_of(task)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert not {"severity", "title", "nextSteps", "status", "errors"} & set(schema["properties"])
    assert schema["additionalProperties"] is False


# --- resource-health ----------------------------------------------------------------------------


def test_resource_health_healthy(go):
    h = out(go("resource-health", "healthy"), "resource-health")
    assert (h["cluster"], h["availabilityState"], h["healthy"]) == (PROD, "Available", True)
    assert h["serviceImpactingEvents"] == 0 and h["reasonType"] is None


def test_resource_health_degraded(go):
    h = out(go("resource-health", "broken"), "resource-health")
    assert (h["availabilityState"], h["healthy"], h["reasonType"]) == (
        "Degraded",
        False,
        "Unplanned",
    )
    assert h["reasonChronicity"] == "Transient" and h["serviceImpactingEvents"] == 1
    assert "API server" in h["summary"]


def test_resource_health_asks_for_the_clusters_current_status_and_registers_nothing(go):
    run = go("resource-health", "broken")
    assert [c[:2] for c in run.calls] == [["aks", "show"], ["rest", "--method"]]
    assert run.calls[1] == [
        "rest", "--method", "get", "--url",
        f"{DEV_ID}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
    ]  # fmt: skip


def test_a_reply_without_a_state_is_an_error_not_a_healthy_cluster(go):
    runner = Replies({SHOW_DEV: (0, stub_json("broken", "aks-show.json"), "")})
    runner.answers[
        (
            "rest",
            "--method",
            "get",
            "--url",
            f"{DEV_ID}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
        )  # fmt: skip
    ] = (0, json.dumps({"properties": {}}), "")
    run = go("resource-health", "broken", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_an_unregistered_resource_provider_is_a_dependency_error(go):
    url = f"{DEV_ID}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01"
    runner = Replies(
        {
            SHOW_DEV: (0, stub_json("broken", "aks-show.json"), ""),
            ("rest", "--method", "get", "--url", url): (
                1,
                "",
                "ERROR: (MissingSubscriptionRegistration) The subscription is not registered to use namespace 'Microsoft.ResourceHealth'.\n",
            ),
        }
    )
    run = go("resource-health", "broken", runner=runner)
    assert [e["code"] for e in run.errors] == ["dependency"]


# --- failures, for every task ---------------------------------------------------------------------


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize(
    "scenario,name,code",
    [
        ("forbidden", DEV, "permission_denied"),
        ("throttled", DEV, "rate_limited"),
        ("not-found", "nope", "not_found"),
    ],
)
def test_a_task_that_cannot_look_fails_with_the_code_and_no_outputs(go, task, scenario, name, code):
    run = go(task, scenario, name)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.errors] == [code]
    assert run.envelope["outputs"] == {}


def test_the_permission_denied_message_names_the_missing_action(go):
    run = go("cluster-status", "forbidden", DEV)
    assert "Microsoft.ContainerService/managedClusters/read" in run.errors[0]["message"]


@pytest.mark.parametrize("task", TASKS)
def test_a_target_that_is_not_an_aks_cluster_is_invalid_input_before_any_az_call(run_script, task):
    from conftest import SUBSCRIPTION

    run = run_script("azure-aks-health", TASKS[task][0], resource=SUBSCRIPTION, now=NOW)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "change",
    [
        {"subscription": ""},
        {"subscription": "not-a-guid"},
        {"resourcegroup": ""},
        {"resourcegroup": "-g"},
        {"resourcegroup": "a b"},
        {"name": "-n"},
        {"name": "has space"},
        {"name": "a" * 64},
        {"name": "_start"},
    ],
)
def test_a_target_with_a_bad_scope_is_invalid_input_before_any_az_call(run_script, change):
    run = run_script(
        "azure-aks-health", "cluster_status", resource={**cluster(DEV), **change}, now=NOW
    )
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


def test_the_warning_az_prints_on_a_good_command_is_not_an_error(go):
    warning = "WARNING: A new Azure CLI version is available.\nWARNING: Forbidden TooManyRequests\n"
    runner = Replies(
        {
            SHOW_PROD: (0, stub_json("healthy", "aks-show.json"), warning),
            (
                "monitor", "diagnostic-settings", "list", "--resource", PROD_ID, "--subscription", SUB,
            ): (0, stub_json("healthy", "aks-diagnostic-settings.json"), warning),
        }
    )  # fmt: skip
    run = go("cluster-status", "healthy", runner=runner)
    assert run.envelope["status"] == "ok" and run.errors == []


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"id": "x"}),
        "[]",
        json.dumps({"id": DEV_ID.replace("rg-orders-dev", "rg-other"), "name": DEV}),
        json.dumps({"id": DEV_ID.replace("managedClusters", "virtualMachines"), "name": DEV}),
        json.dumps({"id": DEV_ID, "name": "another-cluster"}),
        json.dumps(
            {"id": DEV_ID.replace(SUB, "00000000-0000-4000-8000-000000000001"), "name": DEV}
        ),
    ],
)
def test_a_show_that_answers_for_something_else_is_an_error_not_the_wrong_cluster(go, answer):
    run = go("cluster-status", "broken", runner=Replies({SHOW_DEV: (0, answer, "")}))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_the_group_case_arm_reports_may_differ_from_the_identitys_lower_case(run_script):
    # the identity keeps a group lower case; ARM's id spells it as created
    show = stub_json("healthy", "aks-show.json").replace("rg-orders-prod", "RG-Orders-Prod")
    resource = cluster(PROD)
    runner = Replies(
        {
            SHOW_PROD: (0, show, ""),
            (
                "monitor", "diagnostic-settings", "list", "--resource",
                PROD_ID.replace("rg-orders-prod", "RG-Orders-Prod"), "--subscription", SUB,
            ): (0, "[]", ""),
        }
    )  # fmt: skip
    run = run_script(
        "azure-aks-health", "cluster_status", resource=resource, runner=runner, now=NOW
    )
    assert run.envelope["status"] == "ok", run.errors


def test_every_arm_call_names_the_resource_group_and_the_subscription(go):
    for task in TASKS:
        run = go(task, "healthy")
        shows = [c for c in run.calls if c[:2] == ["aks", "show"]]
        assert len(shows) == 1, task
        assert shows[0][shows[0].index("-g") + 1] == "rg-orders-prod"
        for call in run.calls:
            if call[:2] != ["rest", "--method"]:  # az rest takes the id, which holds both
                assert call[-2:] == ["--subscription", SUB], call


# --- cluster-status -----------------------------------------------------------------------------------


def test_cluster_status_of_a_healthy_cluster(go):
    c = out(go("cluster-status", "healthy"), "cluster-status")
    assert c["cluster"] == PROD and c["resourceGroup"] == "rg-orders-prod"
    assert (c["provisioningState"], c["transitioning"]) == ("Succeeded", False)
    assert (c["powerState"], c["running"]) == ("Running", True)
    assert (c["kubernetesVersion"], c["currentKubernetesVersion"]) == ("1.33.4", "1.33.4")
    assert (c["skuTier"], c["supportPlan"]) == ("Standard", "KubernetesOfficial")
    assert c["nodePoolCount"] == 2 and c["nodeResourceGroup"].startswith("MC_")
    assert (c["privateCluster"], c["authorizedIpRangeCount"]) == (False, 1)
    assert (c["localAccountsDisabled"], c["rbacEnabled"], c["aadManaged"]) == (True, True, True)
    assert (c["networkPlugin"], c["networkPluginMode"], c["networkPolicy"]) == (
        "azure", "overlay", "cilium",
    )  # fmt: skip
    assert (c["autoUpgradeChannel"], c["nodeOsUpgradeChannel"]) == ("patch", "NodeImage")
    assert c["oidcIssuerEnabled"] and c["workloadIdentityEnabled"] and c["managedPrometheus"]
    assert c["containerInsights"] is True and c["identityType"] == "SystemAssigned"
    assert c["diagnostics"] == {
        "settingsCount": 1,
        "names": ["aks-to-law"],
        "logAnalytics": True,
        "enabledLogCategories": ["cluster-autoscaler", "kube-apiserver", "kube-audit-admin"],
        "enabledLogCategoriesOmitted": 0,
    }


def test_cluster_status_of_a_broken_cluster_says_what_is_open(go):
    c = out(go("cluster-status", "broken"), "cluster-status")
    assert (c["provisioningState"], c["transitioning"]) == ("Failed", False)
    assert (c["powerState"], c["running"]) == ("Running", True)
    assert c["currentKubernetesVersion"] == "1.29.9" and c["skuTier"] == "Free"
    assert (c["authorizedIpRangeCount"], c["privateCluster"]) == (0, False)
    assert (c["localAccountsDisabled"], c["aadManaged"], c["azureRbacEnabled"]) == (
        False,
        False,
        False,
    )
    assert (c["autoUpgradeChannel"], c["oidcIssuerEnabled"], c["containerInsights"]) == (
        "none", False, False,
    )  # fmt: skip
    assert c["diagnostics"]["settingsCount"] == 0 and c["diagnostics"]["logAnalytics"] is False


def test_a_stopped_cluster_is_not_running_and_not_failed(go):
    c = out(go("cluster-status", "stopped"), "cluster-status")
    assert (c["provisioningState"], c["powerState"], c["running"]) == (
        "Succeeded",
        "Stopped",
        False,
    )


@pytest.mark.parametrize(
    "state,transitioning",
    [
        ("Succeeded", False),
        ("Failed", False),
        ("Canceled", False),
        ("Upgrading", True),
        ("Updating", True),
        ("Stopping", True),
    ],
)
def test_transitioning_is_true_only_while_an_operation_runs(go, state, transitioning):
    show = json.loads(stub_json("broken", "aks-show.json"))
    show["provisioningState"] = state
    runner = Replies(
        {
            SHOW_DEV: (0, json.dumps(show), ""),
            (
                "monitor",
                "diagnostic-settings",
                "list",
                "--resource",
                DEV_ID,
                "--subscription",
                SUB,
            ): (
                0,
                "[]",
                "",
            ),
        }
    )
    c = out(go("cluster-status", "broken", runner=runner), "cluster-status")
    assert (c["provisioningState"], c["transitioning"]) == (state, transitioning)


def test_diagnostic_settings_that_cannot_be_read_leave_the_rest_and_a_partial_run(go):
    run = go("cluster-status", "monitor-forbidden")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "diagnostics")]
    c = out(run, "cluster-status")
    assert c["diagnostics"] is None and c["provisioningState"] == "Failed"
    jsonschema.validate(c, schema_of("cluster-status"))


def test_cluster_status_returns_no_identity_ids_or_workspace_ids(go):
    text = json.dumps(go("cluster-status", "healthy").envelope["outputs"])
    for leak in (
        "0000-0000",
        "9d2c5a10",
        "workspaces/",
        "principalId",
        "clientId",
        "subscriptions/",
    ):
        assert leak not in text, leak


def test_settings_list_may_be_wrapped_in_value_and_a_non_list_is_an_error():
    wrapped = {
        "value": [
            {
                "name": "a",
                "workspaceId": "x",
                "logs": [{"categoryGroup": "allLogs", "enabled": True}],
            }
        ]
    }
    assert diagnostics(wrapped)["enabledLogCategories"] == ["allLogs"]
    with pytest.raises(Exception, match="no list"):
        diagnostics({"unexpected": 1})


# --- node-pools -------------------------------------------------------------------------------------------


def pools_by_name(run):
    return {p["name"]: p for p in out(run, "node-pools")["pools"]["items"]}


def test_node_pools_of_a_healthy_cluster(go):
    run = go("node-pools", "healthy")
    n = out(run, "node-pools")
    assert (n["poolCount"], n["nodeCount"], n["nodeCountKnown"]) == (2, 7, True)
    assert (n["systemPoolCount"], n["systemNodeCount"]) == (1, 3)
    assert (n["failedPoolCount"], n["failedPools"], n["notRunningPools"]) == (0, [], [])
    assert (n["clusterProvisioningState"], n["clusterPowerState"]) == ("Succeeded", "Running")
    apps = pools_by_name(run)["apps"]
    assert (apps["autoscaleEnabled"], apps["minCount"], apps["maxCount"], apps["count"]) == (
        True,
        2,
        10,
        4,
    )
    assert (apps["atMaxCount"], apps["atMinCount"], apps["versionDiffers"]) == (False, False, False)
    assert apps["poolType"] == "VirtualMachineScaleSets" and apps["availabilityZones"] == [
        "1",
        "2",
        "3",
    ]


def test_node_pools_of_a_broken_cluster(go):
    run = go("node-pools", "broken")
    n = out(run, "node-pools")
    assert (n["poolCount"], n["nodeCount"]) == (3, 14)
    assert (n["failedPoolCount"], n["failedPools"]) == (1, ["batch"])
    assert n["clusterProvisioningState"] == "Failed"
    pools = pools_by_name(run)
    assert pools["apps"]["atMaxCount"] is True and pools["apps"]["atMinCount"] is False
    assert pools["apps"]["versionDiffers"] is True
    assert (pools["apps"]["orchestratorVersion"], pools["apps"]["currentOrchestratorVersion"]) == (
        "1.29.9", "1.28.14",
    )  # fmt: skip
    assert (
        pools["batch"]["scaleSetPriority"] == "Spot"
        and pools["batch"]["provisioningState"] == "Failed"
    )
    # the autoscaler is off on the system pool: no bounds, and the count is not "at" any
    system = pools["system"]
    assert system["autoscaleEnabled"] is False
    assert (system["minCount"], system["maxCount"], system["atMaxCount"], system["atMinCount"]) == (
        None, None, None, None,
    )  # fmt: skip
    assert system["count"] == 1 and out(run, "node-pools")["systemNodeCount"] == 1


def test_a_stopped_cluster_lists_its_pools_as_not_running(go):
    run = go("node-pools", "stopped")
    n = out(run, "node-pools")
    assert n["notRunningPoolCount"] == 3 and sorted(n["notRunningPools"]) == [
        "apps",
        "batch",
        "system",
    ]
    assert (n["clusterPowerState"], n["clusterProvisioningState"]) == ("Stopped", "Succeeded")


def test_max_items_cuts_the_pool_list_and_reports_the_rest(go):
    run = go("node-pools", "broken", settings={"maxItems": 2})
    pools = out(run, "node-pools")["pools"]
    assert (len(pools["items"]), pools["total"], pools["omitted"]) == (2, 3, 1)
    assert out(run, "node-pools")["poolCount"] == 3  # the totals cover all pools


def test_the_autoscaler_bounds_are_read_whatever_the_count():
    base = {"name": "p", "enableAutoScaling": True, "minCount": 1, "maxCount": 3}
    assert node_pools.describe({**base, "count": 3})["atMaxCount"] is True
    assert node_pools.describe({**base, "count": 4})["atMaxCount"] is True  # beyond the bound
    assert node_pools.describe({**base, "count": 1})["atMinCount"] is True
    assert node_pools.describe({**base, "count": 2})["atMaxCount"] is False
    assert node_pools.describe({**base, "count": None})["atMaxCount"] is None
    assert node_pools.describe({**base, "count": 0, "minCount": 0})["atMinCount"] is True
    # an explicit false and a missing flag are the same thing: no autoscaler (the classic
    # `== null` check missed the explicit false)
    for flag in ({"enableAutoScaling": False}, {"enableAutoScaling": None}, {}):
        p = node_pools.describe({"name": "p", "count": 2, **flag})
        assert (p["autoscaleEnabled"], p["atMaxCount"]) == (False, None)


def test_a_pool_without_a_count_is_not_a_zero(go):
    rows = json.loads(stub_json("healthy", "aks-nodepools.json"))
    rows[0]["count"] = None
    runner = Replies(
        {
            SHOW_PROD: (0, stub_json("healthy", "aks-show.json"), ""),
            (
                "aks",
                "nodepool",
                "list",
                "--cluster-name",
                PROD,
                "-g",
                "rg-orders-prod",
                "--subscription",
                SUB,
            ): (
                0,
                json.dumps(rows),
                "",
            ),
        }
    )
    n = out(go("node-pools", "healthy", runner=runner), "node-pools")
    assert n["nodeCountKnown"] is False and n["nodeCount"] == 4  # only the pool that reported


def test_an_empty_pool_list_is_an_error_not_a_cluster_without_pools(go):
    runner = Replies(
        {
            SHOW_PROD: (0, stub_json("healthy", "aks-show.json"), ""),
            (
                "aks",
                "nodepool",
                "list",
                "--cluster-name",
                PROD,
                "-g",
                "rg-orders-prod",
                "--subscription",
                SUB,
            ): (
                0,
                "[]",
                "",
            ),
        }
    )
    run = go("node-pools", "healthy", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_node_pools_asks_for_the_cluster_by_name_and_group(go):
    run = go("node-pools", "healthy")
    assert run.calls[1] == [
        "aks", "nodepool", "list", "--cluster-name", PROD, "-g", "rg-orders-prod", "--subscription", SUB
    ]  # fmt: skip


# --- version-support ---------------------------------------------------------------------------------------


def test_version_support_of_a_supported_cluster(go):
    run = go("version-support", "healthy")
    v = out(run, "version-support")
    assert (v["minor"], v["currentKubernetesVersion"], v["location"]) == (
        "1.33",
        "1.33.4",
        "eastus",
    )
    assert (v["minorSupported"], v["minorIsPreview"], v["patchOffered"]) == (True, False, True)
    assert v["minorSupportPlans"] == ["KubernetesOfficial"] and v["latestPatchOfMinor"] == "1.33.5"
    assert v["planCoversMinor"] is True
    assert (v["defaultMinor"], v["latestStableMinor"], v["minorsBehind"]) == ("1.34", "1.34", 1)
    assert [m["version"] for m in v["supportedMinors"]] == ["1.35", "1.34", "1.33", "1.32"]
    assert v["supportedMinors"][0]["isPreview"] is True
    assert v["upgrades"] == {
        "available": True,
        "versions": [
            {"version": "1.33.5", "isPreview": False},
            {"version": "1.34.1", "isPreview": False},
            {"version": "1.35.0", "isPreview": True},
        ],
        "versionsOmitted": 0,
        "latest": "1.35.0",
        "latestStable": "1.34.1",
    }
    assert run.calls[1:] == [
        ["aks", "get-versions", "--location", "eastus", "--subscription", SUB],
        ["aks", "get-upgrades", "--name", PROD, "-g", "rg-orders-prod", "--subscription", SUB],
    ]


def test_version_support_of_a_cluster_out_of_support(go):
    v = out(go("version-support", "broken"), "version-support")
    assert (v["minor"], v["minorSupported"], v["minorsBehind"]) == ("1.29", False, 3)
    assert (v["minorIsPreview"], v["patchOffered"], v["latestPatchOfMinor"]) == (None, None, None)
    assert v["minorSupportPlans"] == [] and v["planCoversMinor"] is None  # not offered: no plans
    assert v["upgrades"]["versions"] == [{"version": "1.30.14", "isPreview": False}]


def test_the_requested_and_current_versions_are_both_reported(go):
    show = json.loads(stub_json("healthy", "aks-show.json"))
    show["kubernetesVersion"], show["currentKubernetesVersion"] = "1.34.1", "1.33.4"
    runner = Replies(
        {
            SHOW_PROD: (0, json.dumps(show), ""),
            ("aks", "get-versions", "--location", "eastus", "--subscription", SUB): (
                0, stub_json("healthy", "aks-versions.json"), "",
            ),
            ("aks", "get-upgrades", "--name", PROD, "-g", "rg-orders-prod", "--subscription", SUB): (
                0, stub_json("healthy", "aks-upgrades.json"), "",
            ),
        }
    )  # fmt: skip
    v = out(go("version-support", "healthy", runner=runner), "version-support")
    assert (v["kubernetesVersion"], v["currentKubernetesVersion"], v["minor"]) == (
        "1.34.1",
        "1.33.4",
        "1.33",
    )


def test_a_retired_patch_is_not_offered_though_its_minor_is(go):
    show = json.loads(stub_json("healthy", "aks-show.json"))
    show["currentKubernetesVersion"] = "1.33.1"
    runner = Replies(
        {
            SHOW_PROD: (0, json.dumps(show), ""),
            ("aks", "get-versions", "--location", "eastus", "--subscription", SUB): (
                0, stub_json("healthy", "aks-versions.json"), "",
            ),
            ("aks", "get-upgrades", "--name", PROD, "-g", "rg-orders-prod", "--subscription", SUB): (
                0, stub_json("healthy", "aks-upgrades.json"), "",
            ),
        }
    )  # fmt: skip
    v = out(go("version-support", "healthy", runner=runner), "version-support")
    assert (v["minorSupported"], v["patchOffered"]) == (True, False)


def lts_only_replies(plan):
    """A cluster on 1.32 whose minor only AKSLongTermSupport covers, on the given support plan."""
    show = json.loads(stub_json("healthy", "aks-show.json"))
    show["kubernetesVersion"] = show["currentKubernetesVersion"] = "1.32.8"
    show["supportPlan"] = plan
    offered = json.loads(stub_json("healthy", "aks-versions.json"))
    for row in offered["values"]:
        if row["version"] == "1.32":
            row["capabilities"]["supportPlan"] = ["AKSLongTermSupport"]
    return Replies(
        {
            SHOW_PROD: (0, json.dumps(show), ""),
            ("aks", "get-versions", "--location", "eastus", "--subscription", SUB): (
                0,
                json.dumps(offered),
                "",
            ),
            (
                "aks",
                "get-upgrades",
                "--name",
                PROD,
                "-g",
                "rg-orders-prod",
                "--subscription",
                SUB,
            ): (
                0,
                stub_json("healthy", "aks-upgrades.json"),
                "",
            ),
        }
    )


def test_a_minor_only_long_term_support_covers_is_offered_but_not_to_an_official_cluster(go):
    v = out(
        go("version-support", "healthy", runner=lts_only_replies("KubernetesOfficial")),
        "version-support",
    )
    assert (v["minorSupported"], v["minorSupportPlans"]) == (True, ["AKSLongTermSupport"])
    assert (v["supportPlan"], v["planCoversMinor"]) == ("KubernetesOfficial", False)


def test_the_same_minor_is_covered_for_a_cluster_on_long_term_support(go):
    v = out(
        go("version-support", "healthy", runner=lts_only_replies("AKSLongTermSupport")),
        "version-support",
    )
    assert (v["supportPlan"], v["planCoversMinor"]) == ("AKSLongTermSupport", True)


def test_the_plan_is_unknown_when_the_cluster_reports_none(go):
    runner = lts_only_replies("KubernetesOfficial")
    key = SHOW_PROD
    show = json.loads(runner.answers[key][1])
    show["supportPlan"] = None
    runner.answers[key] = (0, json.dumps(show), "")
    v = out(go("version-support", "healthy", runner=runner), "version-support")
    assert v["supportPlan"] is None and v["planCoversMinor"] is None


def versions_replies(scenario, versions=None, upgrades=None):
    name, group = (PROD, "rg-orders-prod") if scenario == "healthy" else (DEV, "rg-orders-dev")
    location = "eastus" if scenario == "healthy" else "westeurope"
    ok = lambda f: (0, stub_json(scenario, f), "")  # noqa: E731
    forbidden = (
        1,
        "",
        "ERROR: (AuthorizationFailed) The client does not have authorization to perform action.\n",
    )
    return Replies(
        {
            ("aks", "show", "--name", name, "-g", group, "--subscription", SUB): ok(
                "aks-show.json"
            ),
            ("aks", "get-versions", "--location", location, "--subscription", SUB): versions
            or ok("aks-versions.json"),
            ("aks", "get-upgrades", "--name", name, "-g", group, "--subscription", SUB): upgrades
            or ok("aks-upgrades.json"),
        }
    ), forbidden


def test_versions_that_cannot_be_read_leave_the_upgrades_and_a_partial_run(go):
    _, forbidden = versions_replies("healthy")
    runner, _ = versions_replies("healthy", versions=forbidden)
    run = go("version-support", "healthy", runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "versions")]
    v = out(run, "version-support")
    assert v["minorSupported"] is None and v["supportedMinors"] == [] and v["minorsBehind"] is None
    assert v["upgrades"]["available"] is True
    jsonschema.validate(v, schema_of("version-support"))


def test_upgrades_that_cannot_be_read_leave_the_versions_and_a_partial_run(go):
    _, forbidden = versions_replies("healthy")
    runner, _ = versions_replies("healthy", upgrades=forbidden)
    run = go("version-support", "healthy", runner=runner)
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "upgrades")]
    v = out(run, "version-support")
    assert v["upgrades"] is None and v["minorSupported"] is True
    jsonschema.validate(v, schema_of("version-support"))


def test_when_both_reads_fail_the_run_fails(go):
    _, forbidden = versions_replies("healthy")
    runner, _ = versions_replies("healthy", versions=forbidden, upgrades=forbidden)
    run = go("version-support", "healthy", runner=runner)
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}


@pytest.mark.parametrize("reply", ["{}", '{"values": []}', '{"values": [{"version": "x"}]}', "[]"])
def test_a_versions_reply_that_lists_nothing_is_an_error_not_no_supported_versions(go, reply):
    runner, _ = versions_replies("healthy", versions=(0, reply, ""))
    run = go("version-support", "healthy", runner=runner)
    assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "versions")]
    assert out(run, "version-support")["minorSupported"] is None


def test_an_upgrade_profile_without_a_control_plane_is_an_error(go):
    runner, _ = versions_replies("healthy", upgrades=(0, "{}", ""))
    run = go("version-support", "healthy", runner=runner)
    assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "upgrades")]


def test_version_keys_order_numerically_not_as_text():
    assert version_support.key("1.9.10") < version_support.key("1.10.2")
    assert version_support.minor_of("1.33.4") == "1.33" and version_support.minor_of("v1") is None
    assert version_support.key("garbage") is None


def test_a_cluster_with_no_version_is_an_error(go):
    show = json.loads(stub_json("healthy", "aks-show.json"))
    show["kubernetesVersion"], show["currentKubernetesVersion"] = None, None
    run = go("version-support", "healthy", runner=Replies({SHOW_PROD: (0, json.dumps(show), "")}))
    assert run.errors[0]["code"] == "internal"


# --- activity-failures --------------------------------------------------------------------------------------


def test_activity_of_a_healthy_cluster_says_none_and_means_it(go):
    a = out(go("activity-failures", "healthy"), "activity-failures")
    assert a["failures"] == {"items": [], "total": 0, "omitted": 0}
    assert (a["eventsScanned"], a["eventsForCluster"], a["eventsForOtherResources"]) == (1, 0, 1)
    assert a["scanCut"] is False and a["windowSeconds"] == 86400


def test_activity_groups_the_failures_of_the_cluster_and_its_pools(go):
    a = out(go("activity-failures", "broken"), "activity-failures")
    assert (a["eventsScanned"], a["eventsForCluster"], a["eventsForOtherResources"]) == (6, 4, 2)
    groups = {(g["operation"], g["target"]): g for g in a["failures"]["items"]}
    assert set(groups) == {
        ("Microsoft.ContainerService/managedClusters/write", "cluster"),
        ("Microsoft.ContainerService/managedClusters/agentPools/write", "agentPools/batch"),
    }
    cluster_write = groups[("Microsoft.ContainerService/managedClusters/write", "cluster")]
    assert (
        cluster_write["count"] == 2 and cluster_write["errorCode"] == "VMExtensionProvisioningError"
    )
    assert (cluster_write["level"], cluster_write["subStatus"]) == ("Error", "Conflict")
    # the event spelled in capitals belongs to the cluster, and is the newest: its text is kept
    assert cluster_write["lastSeen"] == "2026-11-04T02:11:40.9100000Z"
    assert cluster_write["firstSeen"] == "2026-11-03T23:30:00.0000000Z"
    pool = groups[
        ("Microsoft.ContainerService/managedClusters/agentPools/write", "agentPools/batch")
    ]
    assert (pool["count"], pool["errorCode"], pool["subStatus"]) == (
        2,
        "QuotaExceeded",
        "BadRequest",
    )
    assert "quota" in pool["message"] and pool["truncated"] is False
    assert [g["count"] for g in a["failures"]["items"]] == [2, 2]


def test_a_cluster_whose_name_starts_like_this_ones_is_not_this_cluster(go):
    a = out(go("activity-failures", "broken"), "activity-failures")
    assert "aks-orders-dev2" not in json.dumps(a)


def test_activity_asks_for_the_groups_failures_in_the_window_and_selects_no_caller(go):
    run = go("activity-failures", "broken")
    call = run.calls[1]
    assert call[:7] == [
        "monitor",
        "activity-log",
        "list",
        "-g",
        "rg-orders-dev",
        "--status",
        "Failed",
    ]
    assert call[7:11] == [
        "--start-time",
        "2026-11-03T12:00:00Z",
        "--end-time",
        "2026-11-04T12:00:00Z",
    ]
    assert call[11:13] == ["--max-events", "501"]  # activityScanLimit + 1, to see a cut
    selected = call[call.index("--select") + 1 : call.index("--subscription")]
    assert "caller" not in selected and "claims" not in selected and "authorization" not in selected
    assert call[-2:] == ["--subscription", SUB]


def test_the_window_and_the_scan_limit_are_inputs_and_settings(go):
    runner = activity_runner([], limit=10, start="2026-11-04T10:00:00Z")
    run = go(
        "activity-failures",
        "broken",
        window="2h",
        settings={"activityScanLimit": 10},
        runner=runner,
    )
    assert run.envelope["status"] == "ok", run.errors
    call = run.calls[1]
    assert call[call.index("--start-time") + 1] == "2026-11-04T10:00:00Z"
    assert call[call.index("--max-events") + 1] == "11"
    assert (
        out(run, "activity-failures")["scanLimit"] == 10
        and out(run, "activity-failures")["windowSeconds"] == 7200
    )


def events(n, resource=DEV_ID):
    return [
        {
            "eventTimestamp": f"2026-11-04T0{i % 10}:00:00.0000000Z",
            "level": "Error",
            "operationName": {"value": "Microsoft.ContainerService/managedClusters/write"},
            "resourceId": resource,
            "status": {"value": "Failed"},
            "subStatus": {"value": "Conflict"},
            "properties": {"statusMessage": json.dumps({"error": {"code": "E", "message": "m"}})},
        }
        for i in range(n)
    ]


def activity_runner(reply, limit=2, start="2026-11-03T12:00:00Z"):
    end = "2026-11-04T12:00:00Z"
    select = [
        "eventTimestamp",
        "level",
        "operationName",
        "properties",
        "resourceId",
        "status",
        "subStatus",
    ]
    return Replies(
        {
            SHOW_DEV: (0, stub_json("broken", "aks-show.json"), ""),
            (
                "monitor", "activity-log", "list", "-g", "rg-orders-dev", "--status", "Failed",
                "--start-time", start, "--end-time", end, "--max-events", str(limit + 1),
                "--select", *select, "--subscription", SUB,
            ): (0, json.dumps(reply), ""),
        }
    )  # fmt: skip


def test_a_read_that_hit_the_limit_says_the_list_may_be_incomplete(go):
    # three events for another resource fill the limit of two: the cluster's list proves nothing
    other = events(3, resource=DEV_ID.replace(DEV, "someone-else"))
    run = go(
        "activity-failures",
        "broken",
        settings={"activityScanLimit": 2},
        runner=activity_runner(other),
    )
    a = out(run, "activity-failures")
    assert (a["scanCut"], a["eventsScanned"], a["eventsForCluster"]) == (True, 2, 0)
    assert a["failures"]["items"] == []


def test_exactly_the_limit_is_not_a_cut(go):
    run = go(
        "activity-failures",
        "broken",
        settings={"activityScanLimit": 2},
        runner=activity_runner(events(2)),
    )
    a = out(run, "activity-failures")
    assert (a["scanCut"], a["eventsScanned"], a["eventsForCluster"]) == (False, 2, 2)


def test_max_items_cuts_the_groups_and_reports_the_rest(go):
    run = go("activity-failures", "broken", settings={"maxItems": 1})
    failures = out(run, "activity-failures")["failures"]
    assert (len(failures["items"]), failures["total"], failures["omitted"]) == (1, 2, 1)


def test_the_message_is_cut_to_1024_characters(go):
    long = events(1)
    long[0]["properties"]["statusMessage"] = json.dumps(
        {"error": {"code": "E", "message": "x" * 3000}}
    )
    a = out(
        go("activity-failures", "broken", runner=activity_runner(long, limit=500)),
        "activity-failures",
    )
    (group,) = a["failures"]["items"]
    assert len(group["message"]) == 1024 and group["truncated"] is True


def test_the_status_message_is_read_as_json_or_as_text():
    assert activity.reason(
        {"statusMessage": '{"status":"Failed","error":{"code":"C","message":"m"}}'}
    ) == ("C", "m")
    assert activity.reason({"statusMessage": "plain text"}) == (None, "plain text")
    assert activity.reason({"statusMessage": "[1]"}) == (None, "[1]")
    assert activity.reason({"statusMessage": '{"error":"s"}'}) == (None, '{"error":"s"}')
    assert activity.reason(None) == (None, "") and activity.reason({}) == (None, "")


def test_timestamps_with_seven_fraction_digits_and_any_zone_are_read():
    assert activity.moment("2026-11-04T01:12:30.1234567Z") == datetime(
        2026, 11, 4, 1, 12, 30, 123456, tzinfo=UTC
    )
    assert activity.moment("2026-11-04T01:12:30Z") == datetime(2026, 11, 4, 1, 12, 30, tzinfo=UTC)
    assert activity.moment("2026-11-04T03:12:30+02:00") == datetime(
        2026, 11, 4, 1, 12, 30, tzinfo=UTC
    )
    assert activity.moment("yesterday") is None and activity.moment(None) is None


def test_an_event_without_a_readable_time_is_counted_and_has_no_first_or_last_seen(go):
    bad = events(1)
    bad[0]["eventTimestamp"] = "garbage"
    a = out(
        go("activity-failures", "broken", runner=activity_runner(bad, limit=500)),
        "activity-failures",
    )
    (group,) = a["failures"]["items"]
    assert (group["count"], group["firstSeen"], group["lastSeen"]) == (1, None, None)


@pytest.mark.parametrize("reply", [{"value": []}, "x", [1], None])
def test_a_reply_that_is_not_a_list_of_events_is_an_error_not_no_failures(go, reply):
    run = go("activity-failures", "broken", runner=activity_runner(reply, limit=500))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_a_cluster_that_is_stopped_or_gone_does_not_make_the_log_look_clean(go):
    run = go("activity-failures", "not-found", "nope")
    assert run.envelope["status"] == "failed" and len(run.calls) == 1  # show failed: no log read


def test_the_activity_log_refusal_fails_the_task_with_the_action_to_grant(go):
    run = go("activity-failures", "monitor-forbidden")
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "permission_denied"
    assert "Microsoft.Insights/eventtypes/management/values/read" in run.errors[0]["message"]


@pytest.mark.parametrize("bad", ["", "soon", "30s", "31d", "0h", "-1h", "24"])
@pytest.mark.parametrize("task", ["activity-failures", "node-metrics"])
def test_a_bad_window_is_invalid_input_before_any_az_call(go, task, bad):
    run = go(task, "healthy", window=bad)
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


@pytest.mark.parametrize("bad", [0, 5001, True, "5", 1.5])
def test_a_bad_scan_limit_is_invalid_input_before_any_az_call(go, bad):
    run = go("activity-failures", "healthy", settings={"activityScanLimit": bad})
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


# --- node-metrics --------------------------------------------------------------------------------------------


def test_node_metrics_of_a_calm_cluster_cross_nothing(go):
    run = go("node-metrics", "healthy")
    m = out(run, "node-metrics")
    for field, metric in (
        ("cpu", "node_cpu_usage_percentage"),
        ("memory", "node_memory_working_set_percentage"),
        ("disk", "node_disk_usage_percentage"),
    ):
        s = m[field]
        assert s["metric"] == metric and s["hasData"] is True
        assert (s["grains"], s["grainsWithData"], s["grainsMissing"]) == (24, 24, 0)
        assert (s["averageOverThreshold"], s["peakOverThreshold"]) == (False, False)
        assert s["lastGrainMissing"] is False and len(s["points"]) == 24
        assert set(s["points"][0]) == {"timestamp", "value", "maximum"}
    assert m["cpu"]["averageMax"] == 40.0 and m["cpu"]["peakMax"] == 58.0
    assert [c[:3] for c in run.calls[1:]] == [["monitor", "metrics", "list"]] * 3
    assert [c[c.index("--metrics") + 1] for c in run.calls[1:]] == [
        "node_cpu_usage_percentage", "node_memory_working_set_percentage", "node_disk_usage_percentage",
    ]  # fmt: skip
    assert all(
        c[c.index("--aggregation") + 1 : c.index("--interval")] == ["Average", "Maximum"]
        for c in run.calls[1:]
    )


def test_node_metrics_of_a_hot_cluster_count_the_intervals_over_the_thresholds(go):
    m = out(go("node-metrics", "broken"), "node-metrics")
    assert m["cpu"]["grainsAverageOverThreshold"] == 6 and m["cpu"]["averageOverThreshold"] is True
    assert m["cpu"]["averageMax"] == 97.0 and m["cpu"]["peakMax"] == 100.0
    assert m["cpu"]["grainsPeakOverThreshold"] > m["cpu"]["grainsAverageOverThreshold"] - 1
    assert m["memory"]["averageOverThreshold"] is True and m["memory"]["averageLast"] == 92.5
    assert m["disk"]["averageOverThreshold"] is False


def test_intervals_without_a_value_are_counted_and_never_read_as_healthy_or_zero(go):
    d = out(go("node-metrics", "broken"), "node-metrics")["disk"]
    assert (d["grains"], d["grainsWithData"], d["grainsMissing"]) == (24, 22, 2)
    assert d["lastGrainMissing"] is True  # the newest interval has none: averageLast is older
    assert d["hasData"] is True and len(d["points"]) == 22


def test_a_stopped_cluster_has_no_data_and_that_is_not_a_healthy_cluster(go):
    m = out(go("node-metrics", "stopped"), "node-metrics")
    for field in ("cpu", "memory", "disk"):
        s = m[field]
        assert (s["hasData"], s["grainsWithData"], s["grainsMissing"], s["grains"]) == (
            False,
            0,
            24,
            24,
        )
        assert (s["averageMin"], s["averageMax"], s["averageLast"], s["peakMax"]) == (
            None,
            None,
            None,
            None,
        )
        assert (s["averageOverThreshold"], s["peakOverThreshold"], s["points"]) == (
            False,
            False,
            [],
        )
        assert s["lastGrainMissing"] is True


def test_the_thresholds_and_the_interval_are_settings(go):
    base = out(go("node-metrics", "healthy"), "node-metrics")["cpu"]
    assert base["thresholdPercent"] == 80 and base["averageOverThreshold"] is False
    low = go(
        "node-metrics", "healthy", settings={"cpuThresholdPercent": 35, "metricInterval": "PT1H"}
    )
    cpu = out(low, "node-metrics")["cpu"]
    assert cpu["thresholdPercent"] == 35 and cpu["grainsAverageOverThreshold"] > 0
    assert out(low, "node-metrics")["memory"]["thresholdPercent"] == 80  # its own threshold


def test_the_peak_can_cross_while_the_average_does_not(go):
    m = out(go("node-metrics", "healthy", settings={"cpuThresholdPercent": 50}), "node-metrics")[
        "cpu"
    ]
    assert m["averageOverThreshold"] is False and m["peakOverThreshold"] is True


def metric_reply(metric, data, error_code="Success"):
    return json.dumps(
        {
            "value": [
                {
                    "errorCode": error_code,
                    "name": {"value": metric},
                    "timeseries": [{"data": data}] if data is not None else [],
                }
            ]
        }
    )


def metrics_runner(start="2026-11-03T12:00:00Z", **replies):
    """SHOW + one reply per metric field (cpu, memory, disk); a missing one is the stub's."""
    end = "2026-11-04T12:00:00Z"
    answers = {SHOW_DEV: (0, stub_json("broken", "aks-show.json"), "")}
    files = {
        "cpu": "aks-metrics-cpu.json",
        "memory": "aks-metrics-memory.json",
        "disk": "aks-metrics-disk.json",
    }
    names = {
        "cpu": "node_cpu_usage_percentage",
        "memory": "node_memory_working_set_percentage",
        "disk": "node_disk_usage_percentage",
    }
    for field, metric in names.items():
        answers[
            (
                "monitor",
                "metrics",
                "list",
                "--resource",
                DEV_ID,
                "--metrics",
                metric,
                "--aggregation",
                "Average",
                "Maximum",
                "--interval",
                "PT1H",
                "--start-time",
                start,
                "--end-time",
                end,
                "--subscription",
                SUB,
            )  # fmt: skip
        ] = replies.get(field) or (0, stub_json("broken", files[field]), "")
    return Replies(answers)


def test_one_metric_that_cannot_be_read_leaves_the_others_and_a_partial_run(go):
    throttled = (1, "", "ERROR: (TooManyRequests) The request is being throttled.\n")
    run = go("node-metrics", "broken", runner=metrics_runner(memory=throttled))
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("rate_limited", "memory")]
    m = out(run, "node-metrics")
    assert m["memory"] is None and m["cpu"]["hasData"] and m["disk"]["hasData"]
    jsonschema.validate(m, schema_of("node-metrics"))


def test_when_every_metric_fails_the_run_fails(go):
    run = go("node-metrics", "monitor-forbidden")
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert run.errors[0]["code"] == "permission_denied"
    assert "Microsoft.Insights/metrics/read" in run.errors[0]["message"]


def test_an_error_code_from_azure_monitor_is_an_error(go):
    bad = (0, metric_reply("node_cpu_usage_percentage", [], "InvalidSamplingType"), "")
    run = go("node-metrics", "broken", runner=metrics_runner(cpu=bad))
    assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "cpu")]
    assert out(run, "node-metrics")["cpu"] is None


def test_no_datapoints_at_all_is_no_data(go):
    empty = (0, metric_reply("node_cpu_usage_percentage", None), "")
    cpu = out(go("node-metrics", "broken", runner=metrics_runner(cpu=empty)), "node-metrics")["cpu"]
    assert (cpu["hasData"], cpu["grains"], cpu["points"]) == (False, 0, [])


def test_more_than_fifty_intervals_are_cut_but_the_aggregates_cover_them_all(go):
    data = [
        {
            "timeStamp": f"2026-11-0{1 + i // 24}T{i % 24:02d}:00:00Z",
            "average": 10.0,
            "maximum": 20.0,
        }
        for i in range(100)
    ]
    data[3]["average"], data[3]["maximum"] = 90.0, 95.0  # old, outside the newest 50
    runner = metrics_runner(
        start="2026-10-30T12:00:00Z", cpu=(0, metric_reply("node_cpu_usage_percentage", data), "")
    )
    cpu = out(go("node-metrics", "broken", runner=runner, window="5d"), "node-metrics")["cpu"]
    assert len(cpu["points"]) == 50 and cpu["pointsOmitted"] == 50
    assert cpu["averageMax"] == 90.0 and cpu["grainsAverageOverThreshold"] == 1
    assert cpu["peakMax"] == 95.0 and cpu["grainsPeakOverThreshold"] == 1


@pytest.mark.parametrize(
    "setting,value",
    [
        ("cpuThresholdPercent", 101),
        ("memoryThresholdPercent", -1),
        ("diskThresholdPercent", "80"),
        ("cpuThresholdPercent", True),
        ("metricInterval", "PT2H"),
        ("maxItems", 0),
        ("commandTimeout", "soon"),
    ],
)
def test_a_bad_setting_is_invalid_input_before_any_az_call(go, setting, value):
    run = go("node-metrics", "healthy", settings={setting: value})
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


# --- the declared tasks ---------------------------------------------------------------------------------------


def az_actions(call):
    """The RBAC actions one az command needs (Azure's operation names)."""
    head = call[:2]
    if head == ["aks", "show"]:
        return {("action", "Microsoft.ContainerService/managedClusters/read")}
    if call[:3] == ["aks", "nodepool", "list"]:
        return {("action", "Microsoft.ContainerService/managedClusters/agentPools/read")}
    if head == ["aks", "get-versions"]:
        return {("action", "Microsoft.ContainerService/locations/kubernetesVersions/read")}
    if head == ["aks", "get-upgrades"]:
        return {("action", "Microsoft.ContainerService/managedClusters/upgradeProfiles/read")}
    if head == ["monitor", "metrics"]:
        return {("action", "Microsoft.Insights/metrics/read")}
    if head == ["monitor", "diagnostic-settings"]:
        return {("action", "Microsoft.Insights/diagnosticSettings/read")}
    if head == ["monitor", "activity-log"]:
        return {("action", "Microsoft.Insights/eventtypes/management/values/read")}
    if head == ["rest", "--method"]:
        if "ResourceHealth/availabilityStatuses" in call[call.index("--url") + 1]:
            return {("action", "Microsoft.ResourceHealth/availabilityStatuses/read")}
    raise AssertionError(f"no permission mapped for az {call}")


@pytest.mark.parametrize("task", TASKS)
def test_each_task_declares_exactly_the_permissions_its_az_calls_need(go, task):
    run = go(task, "healthy")
    needed = set().union(*(az_actions(c) for c in run.calls))
    declared = next(t for t in doc()["tasks"] if t["name"] == task)["permissions"]
    assert {(k, v) for p in declared for k, v in p.items()} == needed


def test_every_declared_permission_is_a_valid_azure_permission():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "azure-permission.json").read_text())
    for t in doc()["tasks"]:
        for permission in t["permissions"]:
            jsonschema.validate(permission, schema)


def test_the_skill_is_read_only_and_declares_the_azure_runtime_and_credential():
    d = doc()
    assert d["runtime"] == "azure@1" and d["defaults"]["credentials"] == "azure/cli"
    assert set(TASKS) == {t["name"] for t in d["tasks"]}
    assert all(t["readOnly"] is True for t in d["tasks"])
    for name, spec in d["settings"].items():
        assert SETTINGS[name] == spec["default"]
    assert d["settings"]["maxItems"]["default"] == 200
    assert set(d["settings"]) == {
        "maxItems", "commandTimeout", "metricInterval", "cpuThresholdPercent",
        "memoryThresholdPercent", "diskThresholdPercent", "activityScanLimit",
    }  # fmt: skip


def test_the_windows_default_to_a_day_not_to_ten_minutes():
    for name in ("activity-failures", "node-metrics"):
        inputs = next(t for t in doc()["tasks"] if t["name"] == name)["inputs"]
        assert inputs["window"]["default"] == "24h"


def test_no_output_is_named_status_or_errors():
    for t in doc()["tasks"]:
        assert not {"status", "errors"} & set(t["outputs"])


def test_the_skill_applies_to_aks_clusters_and_the_type_exists_below_the_resource_group():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["runwhen"]["appliesTo"]["types"] == ["azure/aks-cluster"]
    assert len(front["description"]) <= 1024 and front["name"] == SKILL.name
    assert len(text.splitlines()) < 150
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["aks-cluster"]["parent"] == "resourcegroup"
    assert types["aks-cluster"]["native"] == {
        "provider": "Microsoft.ContainerService",
        "type": "managedClusters",
    }


def test_the_skill_names_only_kubernetes_skills_that_exist():
    import re

    text = (SKILL / "SKILL.md").read_text()
    for skill, task in re.findall(r"kubernetes:([a-z0-9-]+)/([a-z0-9-]+)", text):
        tasks = yaml.safe_load(
            (PLUGIN.parent / "kubernetes" / "skills" / skill / "tasks.yaml").read_text()
        )
        assert task in {t["name"] for t in tasks["tasks"]}, (skill, task)


def test_read_settings_returns_the_values_as_declared():
    from types import SimpleNamespace

    declared = SimpleNamespace(
        max_items=200,
        command_timeout="60s",
        metric_interval="PT1H",
        cpu_threshold_percent=80,
        memory_threshold_percent=80,
        disk_threshold_percent=80,
        activity_scan_limit=500,
    )
    st, th = read_settings(declared), read_thresholds(declared)
    assert (st.max_items, st.metric_interval) == (200, "PT1H")
    assert (th.cpu_percent, th.memory_percent, th.disk_percent) == (80, 80, 80)
    assert read_scan_limit(declared) == 500


def test_the_classifier_reads_the_text_az_prints_for_a_cluster():
    assert (
        classify_error("ERROR: (ResourceGroupNotFound) Resource group 'x' could not be found.")
        == "not_found"
    )
    assert (
        classify_error(
            "ERROR: (ResourceNotFound) The Resource 'Microsoft.ContainerService/managedClusters/x' was not found"
        )
        == "not_found"
    )
    assert (
        classify_error("ERROR: (AuthorizationFailed) The client does not have authorization")
        == "permission_denied"
    )
    assert classify_error("ERROR: (TooManyRequests) throttled") == "rate_limited"
    assert (
        classify_error("ERROR: (SubscriptionNotFound) The subscription could not be found.")
        == "not_found"
    )


@pytest.mark.parametrize("task", TASKS)
def test_a_task_timeout_fits_its_az_commands_each_running_to_the_command_timeout(go, task):
    d = doc()
    spec = next(t for t in d["tasks"] if t["name"] == task)
    seconds = int(str(spec.get("timeout") or d["defaults"]["timeout"]).rstrip("s"))
    command = int(str(d["settings"]["commandTimeout"]["default"]).rstrip("s"))
    assert seconds >= len(go(task, "healthy").calls) * command, task


def test_stub_files_of_the_scenarios_match_what_the_tests_read():
    # the tests above read these captured files directly; a re-capture that renames one fails here
    for scenario in ("healthy", "broken"):
        for name in (
            "aks-show.json",
            "aks-nodepools.json",
            "aks-versions.json",
            "aks-upgrades.json",
        ):
            assert (STUB / scenario / name).is_file(), (scenario, name)


# --- the capture tooling ------------------------------------------------------------------------------------


def test_splicing_a_capture_twice_changes_nothing_and_touches_only_its_own_block(tmp_path):
    import importlib.util
    import shutil

    spec = importlib.util.spec_from_file_location(
        "splice_aks", PLUGIN / "tests" / "stub" / "capture" / "splice_aks.py"
    )
    splice = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(splice)
    scenarios = tmp_path / "scenarios"
    shutil.copytree(STUB, scenarios)
    captured = tmp_path / "captured"
    for scenario in ("healthy", "stopped"):
        (captured / scenario).mkdir(parents=True)
        lines = [
            line
            for line in (STUB / scenario / "calls.yaml").read_text().splitlines(True)
            if line.startswith("    {") and '"argv": ["aks"' in line
        ]
        (captured / scenario / "calls.aks.jsonl").write_text("".join(lines))
        for path in (STUB / scenario).glob("aks-*.json"):
            shutil.copyfile(path, captured / scenario / path.name)
    before = {p: p.read_bytes() for p in scenarios.rglob("*") if p.is_file()}
    splice.main(captured, scenarios)
    splice.main(captured, scenarios)
    after = {p: p.read_bytes() for p in scenarios.rglob("*") if p.is_file()}
    changed = {p.relative_to(scenarios).as_posix() for p in before if before[p] != after[p]}
    # the block of a shared scenario is replaced by the (here reduced) capture; a scenario of this
    # skill's own is rewritten whole; no second copy of the calls appears in either
    assert changed <= {"healthy/calls.yaml", "stopped/calls.yaml"}
    for scenario in ("healthy", "stopped"):
        text = (scenarios / scenario / "calls.yaml").read_text()
        assert text.count(splice.BEGIN) <= 1 and text.count(splice.END) <= 1
        argvs = [
            line.split('"source"')[0] for line in text.splitlines() if line.startswith("    {")
        ]
        assert len(argvs) == len(set(argvs))
