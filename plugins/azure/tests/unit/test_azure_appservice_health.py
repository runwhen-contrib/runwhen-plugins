"""azure-appservice-health against the stub scenarios and hand-made az replies: facts, bounds, every
failure as a code, the permissions each task declares, and that nothing reads a secret."""

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from azure_appservice_health import arm, ops, site, usage
from azure_appservice_health.errors import RwError
from azure_appservice_health.target import target
from conftest import SETTINGS, SUB, StubAz, load_script
from runwhen_capability.testing import make_context, run_main

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "azure-appservice-health"
STUB = PLUGIN / "tests" / "stub" / "scenarios"
NOW = datetime(2026, 11, 4, 4, 0, 0, tzinfo=UTC)
PROD, DEV = "rg-orders-prod", "rg-orders-dev"
API, WEB, FUNC_PROD, FUNC_DEV = (
    "app-orders-api",
    "app-orders-web",
    "func-orders-jobs",
    "func-orders-ingest",
)
PLAN_PROD, PLAN_DEV = "plan-orders-prod", "plan-orders-dev"
GROUP = {
    API: PROD,
    FUNC_PROD: PROD,
    PLAN_PROD: PROD,
    WEB: DEV,
    FUNC_DEV: DEV,
    PLAN_DEV: DEV,
    "nope": DEV,
}
SETTINGS_ALL = {
    **SETTINGS,
    "operationTimeout": "300s",
    "http5xxThreshold": 50,
    "http4xxThreshold": 100,
    "responseTimeThresholdSeconds": 5,
    "cpuThresholdPercent": 80,
    "memoryThresholdPercent": 80,
    "diskQueueThreshold": 10,
}


def web_app(name, group=None):
    """The target of a web app run, as the SDK builds it from the resource path."""
    group = group or GROUP[name]
    return {
        "path": f"azure/subscriptions/{SUB}/resourcegroups/{group}/web-apps/{name}",
        "type": "azure/web-app",
        "subscription": SUB,
        "resourcegroup": group,
        "name": name,
    }


def app_plan(name, group=None):
    group = group or GROUP[name]
    return {
        "path": f"azure/subscriptions/{SUB}/resourcegroups/{group}/app-service-plans/{name}",
        "type": "azure/app-service-plan",
        "subscription": SUB,
        "resourcegroup": group,
        "name": name,
    }


def site_id(name, group=None):
    return f"/subscriptions/{SUB}/resourceGroups/{group or GROUP[name]}/providers/Microsoft.Web/sites/{name}"


def plan_id(name, group=None):
    return f"/subscriptions/{SUB}/resourceGroups/{group or GROUP[name]}/providers/Microsoft.Web/serverfarms/{name}"


def get_argv(path, api="2024-04-01"):
    return ("rest", "--method", "get", "--url", f"{path}?api-version={api}")


# task -> (script, output name, kind of target, default scenario targets (healthy, broken))
TASKS = {
    "app-state": ("app_state", "appState", "app"),
    "plan-state": ("plan_state", "planState", "either"),
    "autoscale": ("autoscale", "autoscale", "either"),
    "resource-health": ("resource_health", "health", "either"),
    "configuration": ("configuration", "configuration", "app"),
    "deployments": ("deployments", "deployments", "app"),
    "http-metrics": ("http_metrics", "http", "app"),
    "function-metrics": ("function_metrics", "functions", "func"),
    "plan-metrics": ("plan_metrics", "planMetrics", "either"),
}
TARGETS = {  # task kind -> (healthy target, broken target)
    "app": (web_app(API), web_app(WEB)),
    "func": (web_app(FUNC_PROD), web_app(FUNC_DEV)),
    "either": (app_plan(PLAN_PROD), app_plan(PLAN_DEV)),
}


@pytest.fixture
def run_task():
    """run_task(script, resource=..., scenario=... | runner=..., dry_run=..., **inputs): the script's main
    under the real v13 context with az answered by the stub scenario appservice-<scenario>, or by a
    runner of the test's own. Returns the envelope, the errors and the az calls made."""

    def run(
        script, *, resource, scenario=None, runner=None, settings=None, dry_run=False, **inputs
    ):
        main = load_script(script, "azure-appservice-health").main
        with make_context(
            resource=resource,
            settings={**SETTINGS_ALL, **(settings or {})},
            dry_run=dry_run,
            now=NOW,
        ) as tc:
            fake = runner or StubAz(f"appservice-{scenario}", None)
            tc.ctx.run = fake
            envelope = run_main(main, tc, **inputs)
            return SimpleNamespace(envelope=envelope, errors=list(tc.errors), calls=fake.calls)

    return run


@pytest.fixture
def go(run_task):
    """go(task, scenario, resource=None, **kw): a read task against the scenario appservice-<scenario>;
    the target is the scenario's healthy or broken object of the task's kind unless one is given."""

    def run(task, scenario, resource=None, **kw):
        script, _, kind = TASKS[task]
        if resource is None:
            resource = TARGETS[kind][0 if scenario == "healthy" else 1]
        return run_task(script, resource=resource, scenario=scenario, **kw)

    return run


def out(run, task):
    return run.envelope["outputs"][TASKS[task][1]]


def doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def tasks_by_name():
    return {t["name"]: t for t in doc()["tasks"]}


def schema_of(output):
    for task in doc()["tasks"]:
        if output in task["outputs"]:
            return json.loads((SKILL / task["outputs"][output]["schema"]).read_text())
    raise KeyError(output)


# --- schemas ----------------------------------------------------------------------------------


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize("scenario", ["healthy", "broken"])
def test_every_output_validates_against_its_schema(go, task, scenario):
    run = go(task, scenario)
    assert run.envelope["status"] in ("ok", "partial"), run.errors
    jsonschema.validate(out(run, task), schema_of(TASKS[task][1]))


def test_every_output_schema_is_valid_2020_12_names_no_verdict_and_is_closed():
    for task in doc()["tasks"]:
        for name, spec in task["outputs"].items():
            schema = json.loads((SKILL / spec["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert not {"severity", "title", "nextSteps", "status", "errors"} & set(
                schema["properties"]
            )
            assert schema["additionalProperties"] is False, name


class Seq:
    """Like Replies, but an argv answered by a list is answered by its entries in turn (the last one
    again after that): the state of an object before and after a write."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.timeouts = []

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False
        key = tuple(argv[1:-2])
        self.calls.append(list(key))
        self.timeouts.append(timeout)
        reply = self.answers[key]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        rc, stdout, stderr = reply
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)


def scenario_file(scenario, name):
    return (STUB / f"appservice-{scenario}" / name).read_text()


def site_json(name, **props):
    body = json.loads(
        scenario_file("healthy" if GROUP[name] == PROD else "broken", f"site-{name}.json")
    )
    body["properties"].update(props)
    return json.dumps(body)


def plan_json(name, capacity=None, sku=None):
    body = json.loads(
        scenario_file("healthy" if GROUP[name] == PROD else "broken", f"plan-{name}.json")
    )
    if capacity is not None:
        body["sku"]["capacity"] = body["properties"]["numberOfWorkers"] = capacity
    if sku is not None:
        body["sku"]["name"] = body["sku"]["size"] = sku
    return json.dumps(body)


# --- app-state ----------------------------------------------------------------------------------


def test_app_state_of_a_running_app(go):
    run = go("app-state", "healthy")
    state = out(run, "app-state")
    assert (state["state"], state["running"], state["availabilityState"], state["limited"]) == (
        "Running",
        True,
        "Normal",
        False,
    )
    assert (state["usageState"], state["quotaExceeded"], state["isFunctionApp"]) == (
        "Normal",
        False,
        False,
    )
    assert (state["planName"], state["planResourceGroup"]) == (PLAN_PROD, PROD)
    assert run.calls == [
        ["rest", "--method", "get", "--url", f"{site_id(API)}?api-version=2024-04-01"]
    ]


def test_app_state_of_a_stopped_limited_app(go):
    state = out(go("app-state", "broken"), "app-state")
    assert (state["state"], state["running"]) == ("Stopped", False)
    assert (state["availabilityState"], state["limited"]) == ("Limited", True)


def test_a_function_app_is_a_web_app_of_kind_functionapp(go):
    state = out(go("app-state", "healthy", resource=web_app(FUNC_PROD)), "app-state")
    assert (state["isFunctionApp"], state["kind"], state["isLinux"]) == (
        True,
        "functionapp,linux",
        True,
    )


@pytest.mark.parametrize("missing", ["state", "properties"])
def test_a_site_without_a_state_is_an_error_not_a_running_app(run_task, missing):
    body = json.loads(site_json(API))
    if missing == "properties":
        del body["properties"]
    else:
        del body["properties"]["state"]
    runner = Seq({get_argv(site_id(API)): (0, json.dumps(body), "")})
    run = run_task("app_state", resource=web_app(API), runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_a_site_answering_for_another_app_is_internal(run_task):
    runner = Seq({get_argv(site_id(API)): (0, site_json(WEB), "")})
    run = run_task("app_state", resource=web_app(API), runner=runner)
    assert run.errors[0]["code"] == "internal" and "another app" in run.errors[0]["message"]


def test_an_app_without_a_plan_has_no_plan_name(run_task):
    body = json.loads(site_json(API))
    del body["properties"]["serverFarmId"]
    runner = Seq({get_argv(site_id(API)): (0, json.dumps(body), "")})
    state = run_task("app_state", resource=web_app(API), runner=runner).envelope["outputs"][
        "appState"
    ]
    assert state["planName"] is None and state["planResourceGroup"] is None


# --- plan-state, autoscale, plan-metrics: a plan target or the plan of a web app --------------------


def test_plan_state_of_a_plan(go):
    run = go("plan-state", "healthy")
    state = out(run, "plan-state")
    assert (state["planStatus"], state["ready"], state["instanceCount"]) == ("Ready", True, 2)
    assert state["sku"] == {
        "name": "P1v3",
        "tier": "PremiumV3",
        "size": "P1v3",
        "family": "Pv3",
        "capacity": 2,
    }
    assert (state["maximumNumberOfWorkers"], state["numberOfSites"]) == (30, 2)
    assert run.calls == [
        ["rest", "--method", "get", "--url", f"{plan_id(PLAN_PROD)}?api-version=2024-04-01"]
    ]


def test_plan_state_of_a_web_app_target_reads_the_plan_the_app_runs_on(go):
    run = go("plan-state", "broken", resource=web_app(WEB))
    assert out(run, "plan-state")["plan"] == PLAN_DEV
    assert [c[4].split("?")[0] for c in run.calls] == [site_id(WEB), plan_id(PLAN_DEV)]


def test_the_plan_of_an_app_may_be_in_another_resource_group(run_task):
    other = plan_id(PLAN_DEV, "rg-elsewhere")
    runner = Seq(
        {
            get_argv(site_id(WEB)): (0, site_json(WEB, serverFarmId=other), ""),
            get_argv(other): (0, plan_json(PLAN_DEV).replace(plan_id(PLAN_DEV), other), ""),
        }
    )
    run = run_task("plan_state", resource=web_app(WEB), runner=runner)
    assert run.envelope["outputs"]["planState"]["plan"] == PLAN_DEV
    assert run.calls[-1][4].startswith(other)


def test_an_app_that_runs_on_no_plan_has_no_plan_to_read(run_task):
    body = json.loads(site_json(WEB))
    del body["properties"]["serverFarmId"]
    runner = Seq({get_argv(site_id(WEB)): (0, json.dumps(body), "")})
    run = run_task("plan_state", resource=web_app(WEB), runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"


def test_a_plan_without_a_status_is_an_error_not_a_ready_plan(run_task):
    body = json.loads(plan_json(PLAN_PROD))
    del body["properties"]["status"]
    runner = Seq({get_argv(plan_id(PLAN_PROD)): (0, json.dumps(body), "")})
    run = run_task("plan_state", resource=app_plan(PLAN_PROD), runner=runner)
    assert run.errors[0]["code"] == "internal"


def test_autoscale_reports_the_settings_that_target_the_plan(go):
    healthy = out(go("autoscale", "healthy"), "autoscale")
    assert (healthy["configured"], healthy["enabled"], healthy["instanceCount"]) == (True, True, 2)
    (setting,) = healthy["settings"]
    assert setting["profiles"] == [
        {
            "name": "Default",
            "minimum": 2,
            "maximum": 5,
            "default": 2,
            "ruleCount": 2,
            "scheduled": False,
            "atMaximum": False,
        }
    ]
    assert (
        out(go("autoscale", "broken"), "autoscale")["settings"][0]["profiles"][0]["atMaximum"]
        is True
    )


def test_autoscale_never_returns_a_notification_address(go):
    text = json.dumps(out(go("autoscale", "healthy"), "autoscale"))
    assert "customEmails" not in text and "@" not in text
    assert out(go("autoscale", "healthy"), "autoscale")["settings"][0]["notificationCount"] == 1


def autoscale_runner(rows, next_link=None):
    first = {"value": rows}
    if next_link:
        first["nextLink"] = next_link
    return Seq(
        {
            get_argv(plan_id(PLAN_PROD)): (0, plan_json(PLAN_PROD), ""),
            get_argv(
                f"/subscriptions/{SUB}/providers/Microsoft.Insights/autoscalesettings",
                "2022-10-01",
            ): (
                0,
                json.dumps(first),
                "",
            ),
        }
    )


def test_a_plan_without_an_autoscale_setting_is_not_configured(run_task):
    other = [
        {
            "name": "x",
            "properties": {"targetResourceUri": plan_id(PLAN_DEV), "profiles": [], "enabled": True},
        }
    ]
    result = run_task("autoscale", resource=app_plan(PLAN_PROD), runner=autoscale_runner(other))
    data = result.envelope["outputs"]["autoscale"]
    assert (data["configured"], data["enabled"], data["total"], data["listComplete"]) == (
        False,
        False,
        0,
        True,
    )


def test_an_autoscale_list_cut_short_that_holds_nothing_says_unknown_not_false(run_task):
    pages = ["https://management.azure.com/next?api-version=2022-10-01"] * 6
    runner = autoscale_runner([], next_link=pages[0])
    runner.answers[("rest", "--method", "get", "--url", pages[0])] = (
        0,
        json.dumps({"value": [], "nextLink": pages[0]}),
        "",
    )
    data = run_task("autoscale", resource=app_plan(PLAN_PROD), runner=runner).envelope["outputs"][
        "autoscale"
    ]
    assert (data["configured"], data["listComplete"]) == (None, False)
    assert (
        len(runner.calls) == 2 + arm.MAX_PAGES - 1
    )  # the plan, the first page, then the pages followed


def test_a_disabled_setting_is_configured_but_not_enabled(run_task):
    row = {
        "name": "a",
        "properties": {
            "targetResourceUri": plan_id(PLAN_PROD).upper(),
            "enabled": False,
            "profiles": [
                {
                    "name": "p",
                    "capacity": {"minimum": "1", "maximum": "x", "default": "1"},
                    "rules": [],
                }
            ],
        },
    }
    data = run_task(
        "autoscale", resource=app_plan(PLAN_PROD), runner=autoscale_runner([row])
    ).envelope["outputs"]["autoscale"]
    assert (data["configured"], data["enabled"]) == (True, False)
    assert data["settings"][0]["profiles"][0]["maximum"] is None  # not a number: unknown, not zero
    assert data["settings"][0]["profiles"][0]["atMaximum"] is None


# --- resource-health ------------------------------------------------------------------------------


def test_resource_health_of_an_app_and_a_plan(go):
    healthy = out(go("resource-health", "healthy"), "resource-health")
    assert (healthy["resourceType"], healthy["availabilityState"], healthy["healthy"]) == (
        "app-service-plan",
        "Available",
        True,
    )
    app = out(go("resource-health", "healthy", resource=web_app(API)), "resource-health")
    assert (app["resourceType"], app["healthy"], app["serviceImpactingEvents"]) == (
        "web-app",
        True,
        0,
    )
    broken = out(go("resource-health", "broken", resource=web_app(WEB)), "resource-health")
    assert (broken["availabilityState"], broken["healthy"], broken["reasonType"]) == (
        "Degraded",
        False,
        "Unplanned",
    )
    assert broken["serviceImpactingEvents"] == 1


def test_resource_health_reads_the_object_first_and_registers_nothing(go):
    run = go("resource-health", "healthy", resource=web_app(API))
    assert [c[4].split("?")[0] for c in run.calls] == [
        site_id(API),
        f"{site_id(API)}/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
    ]
    assert run.calls[1][4].endswith("?api-version=2022-10-01")
    assert not any("register" in " ".join(c) for c in run.calls)


def test_a_reply_without_a_state_is_an_error_not_a_healthy_app(run_task):
    health = f"{site_id(API)}/providers/Microsoft.ResourceHealth/availabilityStatuses/current"
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(health, "2022-10-01"): (0, "{}", ""),
        }
    )
    run = run_task("resource_health", resource=web_app(API), runner=runner)
    assert run.errors[0]["code"] == "internal"


def test_an_unregistered_resource_health_provider_is_a_dependency_not_an_error_to_fix_by_writing(
    run_task,
):
    health = f"{site_id(API)}/providers/Microsoft.ResourceHealth/availabilityStatuses/current"
    err = "ERROR: (MissingSubscriptionRegistration) The subscription is not registered to use namespace 'Microsoft.ResourceHealth'.\n"
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(health, "2022-10-01"): (1, "", err),
        }
    )
    run = run_task("resource_health", resource=web_app(API), runner=runner)
    assert run.errors[0]["code"] == "dependency" and len(runner.calls) == 2


# --- configuration --------------------------------------------------------------------------------


def test_configuration_of_a_well_configured_and_a_badly_configured_app(go):
    good = out(go("configuration", "healthy"), "configuration")
    assert (good["httpsOnly"], good["minTlsVersion"], good["ftpsState"], good["alwaysOn"]) == (
        True,
        "1.2",
        "FtpsOnly",
        True,
    )
    assert good["identity"] == {
        "type": "SystemAssigned",
        "systemAssigned": True,
        "userAssignedCount": 0,
    }
    assert (good["healthCheckPath"], good["runtime"], good["configRead"]) == (
        "/healthz",
        "NODE|20-lts",
        True,
    )
    bad = out(go("configuration", "broken"), "configuration")
    assert (bad["httpsOnly"], bad["minTlsVersion"], bad["ftpsState"], bad["alwaysOn"]) == (
        False,
        "1.0",
        "AllAllowed",
        False,
    )
    assert (
        bad["identity"]["type"] == "None"
        and bad["remoteDebuggingEnabled"] is True
        and bad["healthCheckPath"] is None
    )


def test_the_identity_is_a_type_and_a_count_never_an_id(run_task):
    both = {
        "type": "SystemAssigned, UserAssigned",
        "tenantId": "t-SENTINEL",
        "principalId": "p-SENTINEL",
        "userAssignedIdentities": {
            "/subscriptions/x/identities/a": {
                "principalId": "u-SENTINEL",
                "clientId": "c-SENTINEL",
            },
            "/subscriptions/x/identities/b": {},
        },
    }
    body = json.loads(site_json(API))
    body["identity"] = both
    runner = Seq(
        {
            get_argv(site_id(API)): (0, json.dumps(body), ""),
            get_argv(site_id(API) + "/config/web"): (
                0,
                scenario_file("healthy", f"config-{API}.json"),
                "",
            ),
        }
    )
    data = run_task("configuration", resource=web_app(API), runner=runner).envelope["outputs"][
        "configuration"
    ]
    assert data["identity"] == {
        "type": "SystemAssigned, UserAssigned",
        "systemAssigned": True,
        "userAssignedCount": 2,
    }
    assert "SENTINEL" not in json.dumps(data)


def test_nothing_a_site_or_its_web_config_carries_beyond_the_facts_leaves(run_task):
    """The objects hold app settings, connection strings, the machine key, storage mounts and the
    publishing user name where an API puts them; a task returns the named facts and nothing else."""
    body = json.loads(site_json(API))
    body["properties"]["siteConfig"].update(
        appSettings=[{"name": "DB", "value": "APPSETTING-SENTINEL"}],
        connectionStrings=[
            {"name": "c", "connectionString": "CONNSTRING-SENTINEL", "type": "SQLAzure"}
        ],
        machineKey={"validationKey": "MACHINEKEY-SENTINEL"},
    )
    cfg = json.loads(scenario_file("healthy", f"config-{API}.json"))
    cfg["properties"].update(
        appSettings=[{"name": "K", "value": "APPSETTING-SENTINEL"}],
        connectionStrings=[{"name": "c", "connectionString": "CONNSTRING-SENTINEL"}],
        machineKey={"validationKey": "MACHINEKEY-SENTINEL"},
        azureStorageAccounts={"mount": {"accessKey": "STORAGEKEY-SENTINEL", "accountName": "stub"}},
        publishingUsername="PUBLISHUSER-SENTINEL",
    )
    answers = {
        get_argv(site_id(API)): (0, json.dumps(body), ""),
        get_argv(site_id(API) + "/config/web"): (0, json.dumps(cfg), ""),
    }
    run = run_task("configuration", resource=web_app(API), runner=Seq(answers))
    run_state = run_task("app_state", resource=web_app(API), runner=Seq(answers))
    for result in (run, run_state):
        assert "SENTINEL" not in json.dumps(result.envelope), result.envelope


def test_a_web_config_that_cannot_be_read_leaves_the_site_facts_and_a_scoped_error(run_task):
    run = run_task("configuration", resource=web_app(WEB), scenario="partial")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "config")]
    data = run.envelope["outputs"]["configuration"]
    assert data["configRead"] is False and data["httpsOnly"] is False  # from the site object
    assert data["alwaysOn"] is None and data["minTlsVersion"] is None  # unknown, not off


# --- deployments ------------------------------------------------------------------------------------


def test_deployments_of_a_healthy_app(go):
    data = out(go("deployments", "healthy"), "deployments")
    assert data["productionState"] == "Running" and data["slots"]["total"] == 1
    assert data["slots"]["items"][0]["name"] == "staging"  # ARM names it "<app>/staging"
    deployments = data["deployments"]
    assert (deployments["total"], deployments["failedInWindow"], deployments["inProgress"]) == (
        2,
        0,
        0,
    )
    assert deployments["latest"]["status"] == "Success" and deployments["activeDeployment"][
        "id"
    ].startswith("5d4c3b")


def test_a_failed_and_an_unfinished_deployment_are_reported_and_the_old_one_is_outside_the_window(
    go,
):
    data = out(go("deployments", "broken"), "deployments")["deployments"]
    assert data["failedInWindow"] == 1 and data["inProgress"] == 1
    assert [d["status"] for d in data["items"]] == ["Building", "Failed", "Success"]  # newest first
    assert data["listed"] == 4 and data["total"] == 3  # the record of 10-27 is older than 7d
    assert data["latest"]["endTime"] is None
    wide = out(go("deployments", "broken", window="30d"), "deployments")["deployments"]
    assert wide["total"] == 4


def test_an_unknown_deployment_status_code_is_named_not_guessed(run_task):
    row = {
        "name": "x",
        "properties": {"id": "x", "status": 9, "start_time": "2026-11-04T01:00:00Z"},
    }
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(site_id(API) + "/slots"): (0, '{"value": []}', ""),
            get_argv(site_id(API) + "/deployments"): (0, json.dumps({"value": [row]}), ""),
        }
    )
    data = run_task("deployments", resource=web_app(API), runner=runner).envelope["outputs"][
        "deployments"
    ]
    assert data["deployments"]["items"][0]["status"] == "Unknown(9)"


def one_deployment(run_task, message):
    row = {
        "name": "x",
        "properties": {
            "id": "x",
            "status": 4,
            "message": message,
            "start_time": "2026-11-04T01:00:00Z",
        },
    }
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(site_id(API) + "/slots"): (0, '{"value": []}', ""),
            get_argv(site_id(API) + "/deployments"): (0, json.dumps({"value": [row]}), ""),
        }
    )
    run = run_task("deployments", resource=web_app(API), runner=runner)
    return run.envelope["outputs"]["deployments"]["deployments"]["items"][0]


def test_a_deployment_message_is_cut_at_256_characters(run_task):
    item = one_deployment(run_task, "m" * 3000)
    assert len(item["message"]) == 256 and item["truncated"] is True
    assert one_deployment(run_task, "m" * 256)["truncated"] is False


def test_a_deployment_message_has_its_emails_and_url_credentials_masked(run_task):
    item = one_deployment(
        run_task,
        "fix by bob@example.com via https://deploy:ghp_TOKEN123@git.example/org/repo.git, cc ops.team+x@corp.example.org",
    )
    assert item["message"] == (
        "fix by [email] via https://[credentials]@git.example/org/repo.git, cc [email]"
    )
    assert "TOKEN123" not in item["message"] and "deploy:" not in item["message"]
    assert one_deployment(run_task, "Release 2026.11.03 #42")["message"] == "Release 2026.11.03 #42"


def test_an_app_with_no_deployment_records_has_no_data_not_a_clean_deploy(run_task):
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(site_id(API) + "/slots"): (0, '{"value": []}', ""),
            get_argv(site_id(API) + "/deployments"): (0, '{"value": []}', ""),
        }
    )
    data = run_task("deployments", resource=web_app(API), runner=runner).envelope["outputs"][
        "deployments"
    ]["deployments"]
    assert (data["listed"], data["total"], data["latest"], data["activeDeployment"]) == (
        0,
        0,
        None,
        None,
    )


def test_slots_that_cannot_be_read_leave_the_deployments_and_a_scoped_error(run_task):
    run = run_task("deployments", resource=web_app(WEB), scenario="partial")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "slots")]
    data = run.envelope["outputs"]["deployments"]
    assert data["slots"] is None and data["deployments"]["failedInWindow"] == 1


def test_when_neither_list_can_be_read_the_task_fails(run_task):
    err = 'ERROR: Forbidden({"error":{"code":"AuthorizationFailed","message":"no"}})\n'
    runner = Seq(
        {
            get_argv(site_id(API)): (0, site_json(API), ""),
            get_argv(site_id(API) + "/slots"): (1, "", err),
            get_argv(site_id(API) + "/deployments"): (1, "", err),
        }
    )
    run = run_task("deployments", resource=web_app(API), runner=runner)
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert run.errors[0]["code"] == "permission_denied"


@pytest.mark.parametrize("bad", ["0s", "31d", "soon"])
def test_a_bad_window_is_invalid_input_before_any_az_call(go, bad):
    run = go("deployments", "healthy", window=bad)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


# --- metrics ----------------------------------------------------------------------------------------


def test_http_metrics_of_a_healthy_app_cross_nothing(go):
    data = out(go("http-metrics", "healthy"), "http-metrics")
    for field in ("http5xx", "http4xx", "responseTime"):
        assert data[field]["crossed"] is False and data[field]["withData"] == 24
    assert data["requests"]["total"] == 32940.0 and "threshold" not in data["requests"]
    assert (data["windowSeconds"], data["interval"]) == (86400, "PT1H")


def test_http_metrics_of_a_failing_app_show_the_spike_as_crossings_and_a_total(go):
    data = out(go("http-metrics", "broken"), "http-metrics")
    assert (data["http5xx"]["crossings"], data["http5xx"]["max"], data["http5xx"]["last"]) == (
        3,
        240.0,
        2.0,
    )
    assert data["http5xx"]["total"] == 717.0 and data["http5xx"]["crossed"] is True
    assert (data["responseTime"]["crossed"], data["responseTime"]["max"]) == (True, 7.8)  # seconds
    assert len(data["http5xx"]["points"]) == 24 and data["http5xx"]["pointsOmitted"] == 0
    assert data["responseTime"]["points"][19]["maximum"] == 21.5


def test_the_thresholds_are_settings(go):
    data = out(
        go(
            "http-metrics",
            "broken",
            settings={"http5xxThreshold": 1000, "responseTimeThresholdSeconds": 30},
        ),
        "http-metrics",
    )
    assert data["http5xx"]["crossed"] is False and data["responseTime"]["crossed"] is False
    assert data["http4xx"]["crossed"] is True  # its threshold is still 100


def test_the_metric_calls_name_the_metric_the_aggregation_and_the_object(go):
    run = go("http-metrics", "healthy")
    assert [c[6] for c in run.calls[1:]] == ["Requests", "Http5xx", "Http4xx", "HttpResponseTime"]
    assert all(c[4] == site_id(API) for c in run.calls[1:])
    assert run.calls[4][7:10] == ["--aggregation", "Average", "Maximum"]


def test_missing_grains_are_counted_never_read_as_zero_or_healthy(go):
    data = out(go("function-metrics", "broken"), "function-metrics")
    executions = data["executions"]
    assert (executions["grains"], executions["withData"], executions["missing"]) == (24, 21, 3)
    assert executions["lastGrainMissing"] is True  # `last` is an older interval's value
    assert (
        executions["total"] == 8435.0 and executions["min"] == 0.0
    )  # two real zero-execution grains
    assert data["httpServerErrors"]["crossed"] is True


def test_function_metrics_of_a_function_app(go):
    data = out(go("function-metrics", "healthy"), "function-metrics")
    assert data["isFunctionApp"] is True and data["executions"]["total"] == 23040.0
    assert data["httpServerErrors"]["crossed"] is False


def test_function_metrics_on_a_web_app_that_is_not_a_function_app_is_invalid_input(go):
    run = go("function-metrics", "healthy", resource=web_app(API))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert len(run.calls) == 1  # the site, no metric


def test_plan_metrics_of_a_starved_plan_and_of_the_plan_of_an_app(go):
    data = out(go("plan-metrics", "broken"), "plan-metrics")
    assert (data["cpu"]["crossed"], data["memory"]["crossed"], data["diskQueue"]["crossings"]) == (
        True,
        True,
        1,
    )
    assert data["cpu"]["points"][12]["maximum"] == 99.0
    via_app = go("plan-metrics", "broken", resource=web_app(WEB))
    assert out(via_app, "plan-metrics") == data
    assert via_app.calls[0][4].split("?")[0] == site_id(WEB)  # the app first, then its plan


def test_a_metric_that_cannot_be_read_is_null_with_its_own_error_and_the_others_stand(run_task):
    run = run_task("http_metrics", resource=web_app(WEB), scenario="partial")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "http4xx")]
    data = run.envelope["outputs"]["http"]
    assert data["http4xx"] is None and data["http5xx"]["crossed"] is True


def test_when_every_metric_fails_the_task_fails_with_the_first_code(run_task):
    err = "ERROR: (TooManyRequests) The request is being throttled.\nCode: TooManyRequests\n"

    def runner(argv, **kw):
        key = argv[1:-2]
        if key[0] == "rest":
            return subprocess.CompletedProcess(argv, 0, site_json(API), "")
        return subprocess.CompletedProcess(argv, 1, "", err)

    runner.calls = []
    run = run_task("http_metrics", resource=web_app(API), runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "rate_limited"
    assert run.envelope["outputs"] == {}


def test_the_time_budget_stops_further_metric_calls_and_reports_the_timeout(run_task):
    class Slow:
        """Every metric call takes the whole budget on the clock."""

        def __init__(self):
            self.clock = 0.0
            self.calls = []

        def __call__(self, argv, **kw):
            key = tuple(argv[1:-2])
            self.calls.append(list(key))
            if key[0] == "rest":
                return subprocess.CompletedProcess(argv, 0, site_json(API), "")
            self.clock += 200.0
            return subprocess.CompletedProcess(
                argv, 0, scenario_file("healthy", f"metrics-{API}-{key[6]}.json"), ""
            )

    slow = Slow()
    main = load_script("http_metrics", "azure-appservice-health").main
    with make_context(resource=web_app(API), settings=SETTINGS_ALL, now=NOW) as tc:
        tc.ctx.run = slow
        # read_set takes the clock from usage.time.monotonic
        import azure_appservice_health.usage as u

        real = u.time.monotonic
        u.time.monotonic = lambda: slow.clock
        try:
            envelope = run_main(main, tc)
        finally:
            u.time.monotonic = real
        errors = list(tc.errors)
    data = envelope["outputs"]["http"]
    # 200s per call: the second starts at 200s (260s with its own allowance, inside 330s), the third would not fit
    assert data["requests"] is not None and data["http5xx"] is not None
    assert data["http4xx"] is None and data["responseTime"] is None
    assert envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [
        ("timeout", "http4xx"),
        ("timeout", "responseTime"),
    ]
    assert len(slow.calls) == 3  # the site, then two metrics


# --- the read tasks fail with the code, never an empty result ------------------------------------------


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize(
    "scenario,code",
    [("forbidden", "permission_denied"), ("throttled", "rate_limited"), ("not-found", "not_found")],
)
def test_a_refused_read_fails_with_the_code_and_an_empty_result_is_never_returned(
    go, task, scenario, code
):
    script, output, kind = TASKS[task]
    resource = {"app": web_app, "func": web_app, "either": app_plan}[kind](
        "nope"
        if scenario == "not-found"
        else (FUNC_DEV if kind == "func" else (WEB if kind == "app" else PLAN_DEV))
    )
    run = go(task, scenario, resource=resource)
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert [e["code"] for e in run.errors] == [code]
    assert len(run.calls) == 1  # the object was the first and only thing asked


def test_the_forbidden_text_names_the_action_to_grant(go):
    run = go("app-state", "forbidden", resource=web_app(WEB))
    assert "Microsoft.Web/sites/read" in run.errors[0]["message"]


@pytest.mark.parametrize("task", TASKS)
def test_a_wrong_kind_of_target_is_invalid_input_before_any_az_call(go, task):
    script, _, kind = TASKS[task]
    wrong = (
        app_plan(PLAN_PROD)
        if kind in ("app", "func")
        else {
            "type": "azure/keyvault",
            "name": "kv-x",
            "subscription": SUB,
            "resourcegroup": PROD,
            "path": "p",
        }
    )
    run = go(task, "healthy", resource=wrong)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize("name", ["-x", "a/b", "a b", "x" * 61, "a;b"])
def test_a_name_az_could_read_as_an_option_or_a_path_is_refused(go, name):
    resource = {**web_app(API), "name": name}
    run = go("app-state", "healthy", resource=resource)
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


def test_a_target_without_a_subscription_or_group_is_invalid_input():
    for field in ("subscription", "resourcegroup"):
        with pytest.raises(RwError) as caught:
            target({**web_app(API), field: ""})
        assert caught.value.code == "invalid_input"
    with pytest.raises(RwError):
        target({**web_app(API), "resourcegroup": "-g"})


@pytest.mark.parametrize("bad", [0, 10001, True, "5"])
def test_a_bad_max_items_is_invalid_input_before_any_az_call(go, bad):
    run = go("app-state", "healthy", settings={"maxItems": bad})
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "setting,bad",
    [
        ("http5xxThreshold", -1),
        ("cpuThresholdPercent", 101),
        ("responseTimeThresholdSeconds", True),
        ("diskQueueThreshold", "x"),
    ],
)
def test_a_bad_threshold_is_invalid_input_before_any_az_call(go, setting, bad):
    task = (
        "plan-metrics"
        if setting in ("cpuThresholdPercent", "diskQueueThreshold")
        else "http-metrics"
    )
    run = go(task, "healthy", settings={setting: bad})
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


def test_max_items_cuts_a_list_and_says_how_many_were_left_out(go):
    data = out(go("deployments", "broken", window="30d", settings={"maxItems": 2}), "deployments")[
        "deployments"
    ]
    assert (len(data["items"]), data["total"], data["omitted"]) == (2, 4, 2)


# --- the write tasks ----------------------------------------------------------------------------------

RESTART = ("webapp", "restart", "-g", PROD, "-n", API, "--subscription", SUB)
STOP = ("webapp", "stop", "-g", PROD, "-n", API, "--subscription", SUB)
START_WEB = ("webapp", "start", "-g", DEV, "-n", WEB, "--subscription", SUB)
SWAP = (
    "webapp",
    "deployment",
    "slot",
    "swap",
    "-g",
    PROD,
    "-n",
    API,
    "--slot",
    "staging",
    "--target-slot",
    "production",
    "--subscription",
    SUB,
)
UPDATE = ("appservice", "plan", "update", "-g", PROD, "-n", PLAN_PROD, "--subscription", SUB)
SLOTS = get_argv(site_id(API) + "/slots")
SITE = get_argv(site_id(API))
PLAN = get_argv(plan_id(PLAN_PROD))


def is_write(call):
    return call[0] in ("webapp", "appservice") and call[1] != "list"


@pytest.mark.parametrize(
    "script,verb", [("restart", "restart"), ("start", "start"), ("stop", "stop")]
)
def test_a_dry_run_reads_the_app_and_sends_nothing(run_task, script, verb):
    runner = Seq({SITE: (0, site_json(API), "")})
    run = run_task(script, resource=web_app(API), runner=runner, dry_run=True)
    assert run.envelope["status"] == "ok" and not any(is_write(c) for c in run.calls)
    power = run.envelope["outputs"]["power"]
    assert (power["applied"], power["dryRun"], power["action"], power["after"]) == (
        False,
        True,
        verb,
        None,
    )
    assert power["before"] == {"state": "Running", "availabilityState": "Normal"}
    assert power["reachedExpected"] is None


def test_what_a_dry_run_would_change(run_task):
    stopped = Seq({get_argv(site_id(WEB)): (0, site_json(WEB), "")})
    start = run_task("start", resource=web_app(WEB), runner=stopped, dry_run=True)
    assert start.envelope["outputs"]["power"]["wouldChange"] is True
    stop = run_task(
        "stop",
        resource=web_app(WEB),
        runner=Seq({get_argv(site_id(WEB)): (0, site_json(WEB), "")}),
        dry_run=True,
    )
    assert stop.envelope["outputs"]["power"]["wouldChange"] is False  # already stopped
    running = Seq({SITE: (0, site_json(API), "")})
    restart = run_task("restart", resource=web_app(API), runner=running, dry_run=True)
    assert restart.envelope["outputs"]["power"]["wouldChange"] is True


def test_restart_sends_the_call_and_reads_the_app_again(run_task):
    runner = Seq(
        {
            SITE: [(0, site_json(API), ""), (0, site_json(API, state="Running"), "")],
            RESTART: (0, "", ""),
        }
    )
    run = run_task("restart", resource=web_app(API), runner=runner)
    power = run.envelope["outputs"]["power"]
    assert run.envelope["status"] == "ok" and power["applied"] is True
    assert (power["reachedExpected"], power["after"]["state"]) == (True, "Running")
    assert runner.calls == [list(SITE), list(RESTART), list(SITE)]
    assert runner.timeouts[1] == 300.0  # operationTimeout, not commandTimeout


def test_stop_reports_whether_the_app_reached_the_state(run_task):
    runner = Seq(
        {
            SITE: [(0, site_json(API), ""), (0, site_json(API, state="Stopped"), "")],
            STOP: (0, "", ""),
        }
    )
    power = run_task("stop", resource=web_app(API), runner=runner).envelope["outputs"]["power"]
    assert (power["expectedState"], power["reachedExpected"], power["after"]["state"]) == (
        "Stopped",
        True,
        "Stopped",
    )
    lagging = Seq({SITE: [(0, site_json(API), ""), (0, site_json(API), "")], STOP: (0, "", "")})
    power = run_task("stop", resource=web_app(API), runner=lagging).envelope["outputs"]["power"]
    assert (
        power["applied"] is True and power["reachedExpected"] is False
    )  # not a failure: Azure may lag


def test_an_app_that_cannot_be_read_afterwards_keeps_the_applied_result_and_a_scoped_error(
    run_task,
):
    err = 'ERROR: Too Many Requests({"error":{"code":"TooManyRequests"}})\n'
    runner = Seq({SITE: [(0, site_json(API), ""), (1, "", err)], RESTART: (0, "", "")})
    run = run_task("restart", resource=web_app(API), runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("rate_limited", "after")]
    power = run.envelope["outputs"]["power"]
    assert power["applied"] is True and power["after"] is None and power["reachedExpected"] is None


@pytest.mark.parametrize("script", ["restart", "start", "stop"])
def test_a_refused_write_fails_with_the_code_and_says_it_was_not_applied(run_task, script):
    run = run_task(script, resource=web_app(WEB), scenario="write-forbidden")
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert [e["code"] for e in run.errors] == ["permission_denied"]
    assert f"Microsoft.Web/sites/{script}/action" in run.errors[0]["message"]
    assert [is_write(c) for c in run.calls] == [
        False,
        True,
    ]  # the read worked, the write was refused


def test_a_write_that_outlasts_operation_timeout_says_it_may_still_be_running(run_task):
    def runner(argv, **kw):
        if argv[1] == "rest":
            return subprocess.CompletedProcess(argv, 0, site_json(API), "")
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    runner.calls = []
    run = run_task("restart", resource=web_app(API), runner=runner)
    assert run.errors[0]["code"] == "timeout" and "may still be running" in run.errors[0]["message"]


@pytest.mark.parametrize("bad", ["0s", "601s", "soon"])
def test_a_bad_operation_timeout_is_invalid_input_before_any_az_call(run_task, bad):
    run = run_task(
        "restart", resource=web_app(API), runner=Seq({}), settings={"operationTimeout": bad}
    )
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize("script", ["restart", "start", "stop", "swap_slot"])
def test_the_app_writes_refuse_a_plan_target(run_task, script):
    extra = {"source_slot": "staging"} if script == "swap_slot" else {}
    run = run_task(script, resource=app_plan(PLAN_PROD), runner=Seq({}), **extra)
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


# swap-slot


def slots_json(*pairs):
    return json.dumps(
        {
            "value": [
                {"name": f"{API}/{n}", "properties": {"state": st, "availabilityState": "Normal"}}
                for n, st in pairs
            ]
        }
    )


def test_swap_dry_run_checks_both_slots_and_sends_nothing(run_task):
    runner = Seq(
        {SITE: (0, site_json(API), ""), SLOTS: (0, slots_json(("staging", "Running")), "")}
    )
    run = run_task(
        "swap_slot", resource=web_app(API), runner=runner, dry_run=True, source_slot="staging"
    )
    swap = run.envelope["outputs"]["swap"]
    assert (swap["applied"], swap["dryRun"], swap["sourceSlot"], swap["targetSlot"]) == (
        False,
        True,
        "staging",
        "production",
    )
    quiet = {
        "state": "Running",
        "availabilityState": "Normal",
        "swapInProgress": False,
        "targetSwapSlot": None,
    }
    assert swap["before"] == {"source": quiet, "target": quiet}
    assert swap["after"] is None and not any(is_write(c) for c in run.calls)


def test_swap_sends_the_swap_and_reads_the_slots_again(run_task):
    runner = Seq(
        {
            SITE: (0, site_json(API), ""),
            SLOTS: [
                (0, slots_json(("staging", "Running")), ""),
                (0, slots_json(("staging", "Running")), ""),
            ],
            SWAP: (0, "", ""),
        }
    )
    run = run_task("swap_slot", resource=web_app(API), runner=runner, source_slot="staging")
    swap = run.envelope["outputs"]["swap"]
    assert run.envelope["status"] == "ok" and swap["applied"] is True
    assert (
        swap["after"]["source"]["state"] == "Running"
        and swap["after"]["target"]["state"] == "Running"
    )
    assert list(SWAP) in runner.calls and runner.timeouts[runner.calls.index(list(SWAP))] == 300.0


def test_swapping_a_slot_that_does_not_exist_is_not_found_before_any_write(run_task):
    runner = Seq(
        {SITE: (0, site_json(API), ""), SLOTS: (0, slots_json(("staging", "Running")), "")}
    )
    run = run_task("swap_slot", resource=web_app(API), runner=runner, source_slot="nope")
    assert run.errors[0]["code"] == "not_found" and "'nope'" in run.errors[0]["message"]
    assert not any(is_write(c) for c in run.calls)
    run = run_task(
        "swap_slot",
        resource=web_app(API),
        runner=Seq(
            {SITE: (0, site_json(API), ""), SLOTS: (0, slots_json(("staging", "Running")), "")}
        ),
        source_slot="staging",
        target_slot="canary",
    )
    assert run.errors[0]["code"] == "not_found"


@pytest.mark.parametrize(
    "source,target_slot",
    [
        ("staging", "staging"),
        ("production", "production"),
        ("production", "staging"),
        ("", "production"),
        ("a/b", "production"),
        ("-x", "production"),
        ("staging", "a b"),
    ],
)
def test_a_swap_that_makes_no_sense_is_invalid_input_before_any_az_call(
    run_task, source, target_slot
):
    run = run_task(
        "swap_slot",
        resource=web_app(API),
        runner=Seq({}),
        source_slot=source,
        target_slot=target_slot,
    )
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


def test_a_slot_list_too_long_to_check_is_not_taken_for_a_missing_slot(run_task):
    page = {"value": [], "nextLink": "https://management.azure.com/next"}
    answers = {
        SITE: (0, site_json(API), ""),
        SLOTS: (0, json.dumps(page), ""),
        ("rest", "--method", "get", "--url", "https://management.azure.com/next"): (
            0,
            json.dumps(page),
            "",
        ),
    }
    run = run_task("swap_slot", resource=web_app(API), runner=Seq(answers), source_slot="staging")
    assert run.errors[0]["code"] == "internal"


def test_a_refused_swap_fails_with_the_action_to_grant(run_task):
    run = run_task(
        "swap_slot", resource=web_app(WEB), scenario="write-forbidden", source_slot="staging"
    )
    assert run.errors[0]["code"] == "permission_denied"
    assert "Microsoft.Web/sites/slots/slotsswap/action" in run.errors[0]["message"]


# scale


def test_scale_needs_a_value_and_checks_it_before_any_az_call(run_task):
    for kw in (
        {},
        {"instances": 0},
        {"instances": -2},
        {"instances": True},
        {"sku": "P1 v3"},
        {"sku": "1"},
        {"sku": "--set"},
    ):
        run = run_task("scale", resource=app_plan(PLAN_PROD), runner=Seq({}), **kw)
        assert run.calls == [] and run.errors[0]["code"] == "invalid_input", kw


def test_scale_refuses_a_web_app_target(run_task):
    run = run_task("scale", resource=web_app(API), runner=Seq({}), instances=3)
    assert run.calls == [] and run.errors[0]["code"] == "invalid_input"


AUTOSCALE = get_argv(
    f"/subscriptions/{SUB}/providers/Microsoft.Insights/autoscalesettings", "2022-10-01"
)


def autoscale_reply(rows=None, enabled=True, low="2", high="5", plan=PLAN_PROD):
    """The subscription's autoscale list with one setting for `plan` (or the given rows)."""
    if rows is None:
        rows = [
            {
                "name": "auto",
                "properties": {
                    "enabled": enabled,
                    "targetResourceUri": plan_id(plan),
                    "profiles": [
                        {
                            "name": "Default",
                            "capacity": {"minimum": low, "maximum": high, "default": low},
                        }
                    ],
                },
            }
        ]
    return (0, json.dumps({"value": rows}), "")


def scale_runner(plans, update=None, autoscale=None):
    answers = {PLAN: plans, AUTOSCALE: autoscale or autoscale_reply()}
    if update:
        answers[update] = (0, "{}", "")
    return Seq(answers)


def test_scale_dry_run_reports_what_would_change_for_every_app_on_the_plan(run_task):
    runner = scale_runner((0, plan_json(PLAN_PROD), ""))
    run = run_task(
        "scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=True, instances=4, sku="P2v3"
    )
    scale = run.envelope["outputs"]["scale"]
    assert (scale["applied"], scale["noChange"], scale["numberOfSites"]) == (False, False, 2)
    assert scale["requested"] == {"instances": 4, "sku": "P2v3"}
    assert scale["before"] == {
        "skuName": "P1v3",
        "tier": "PremiumV3",
        "instanceCount": 2,
        "planStatus": "Ready",
    }
    assert scale["autoscale"] == {"enabled": True, "minimum": 2, "maximum": 5}
    assert scale["autoscaleOverridden"] is False
    assert runner.calls == [list(PLAN), list(AUTOSCALE)]


def test_scale_sends_the_values_and_reads_the_plan_again(run_task):
    update = (*UPDATE, "--number-of-workers", "4", "--sku", "P2v3")
    runner = scale_runner(
        [(0, plan_json(PLAN_PROD), ""), (0, plan_json(PLAN_PROD, capacity=4, sku="P2v3"), "")],
        update,
    )
    run = run_task("scale", resource=app_plan(PLAN_PROD), runner=runner, instances=4, sku="P2v3")
    scale = run.envelope["outputs"]["scale"]
    assert (
        run.envelope["status"] == "ok"
        and scale["applied"] is True
        and scale["reachedRequested"] is True
    )
    assert scale["after"]["instanceCount"] == 4 and scale["after"]["skuName"] == "P2v3"
    assert runner.calls == [list(PLAN), list(AUTOSCALE), list(update), list(PLAN)]
    assert runner.timeouts[2] == 300.0


def test_scale_with_only_instances_sends_only_the_worker_count(run_task):
    update = (*UPDATE, "--number-of-workers", "3")
    runner = scale_runner([(0, plan_json(PLAN_PROD), ""), (0, plan_json(PLAN_PROD), "")], update)
    scale = run_task("scale", resource=app_plan(PLAN_PROD), runner=runner, instances=3).envelope[
        "outputs"
    ]["scale"]
    assert (
        scale["reachedRequested"] is False
    )  # the plan still reads 2: Azure may lag, not a failure
    assert list(update) in runner.calls


def test_scale_to_the_current_values_is_a_no_change(run_task):
    runner = scale_runner((0, plan_json(PLAN_PROD), ""))
    scale = run_task(
        "scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=True, instances=2, sku="p1V3"
    ).envelope["outputs"]["scale"]
    assert scale["noChange"] is True


def test_scale_above_what_the_sku_allows_is_invalid_input_before_the_write(run_task):
    runner = Seq({get_argv(plan_id(PLAN_DEV)): (0, plan_json(PLAN_DEV), "")})
    run = run_task("scale", resource=app_plan(PLAN_DEV), runner=runner, instances=11)
    assert run.errors[0]["code"] == "invalid_input" and "10" in run.errors[0]["message"]
    assert not any(is_write(c) for c in run.calls)


@pytest.mark.parametrize("dry", [True, False])
def test_scale_outside_an_enabled_autoscale_setting_is_refused_unless_overridden(run_task, dry):
    """An enabled setting moves the plan back within its bounds, so the scale would not hold: refused
    in the dry run too, before any write."""
    for wanted in (1, 6):
        runner = scale_runner((0, plan_json(PLAN_PROD), ""))
        run = run_task(
            "scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=dry, instances=wanted
        )
        assert run.errors[0]["code"] == "invalid_input", wanted
        assert (
            "autoscale" in run.errors[0]["message"]
            and "overrideAutoscale" in run.errors[0]["message"]
        )
        assert not any(is_write(c) for c in run.calls)
    # inside the bounds, and with only a sku (no count asked for), nothing is refused
    for kw in ({"instances": 2}, {"instances": 5}, {"sku": "P2v3"}):
        runner = scale_runner((0, plan_json(PLAN_PROD), ""))
        run = run_task("scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=True, **kw)
        assert run.envelope["status"] == "ok", kw


def test_override_autoscale_lets_the_request_through_and_says_so(run_task):
    update = (*UPDATE, "--number-of-workers", "8")
    runner = scale_runner(
        [(0, plan_json(PLAN_PROD), ""), (0, plan_json(PLAN_PROD, capacity=8), "")], update
    )
    run = run_task(
        "scale", resource=app_plan(PLAN_PROD), runner=runner, instances=8, override_autoscale=True
    )
    scale = run.envelope["outputs"]["scale"]
    assert run.envelope["status"] == "ok" and scale["applied"] is True
    assert scale["autoscaleOverridden"] is True and scale["autoscale"]["maximum"] == 5


def test_a_disabled_or_absent_autoscale_setting_does_not_hold_a_scale_back(run_task):
    cases = (
        (autoscale_reply(enabled=False), False),
        (autoscale_reply(rows=[]), False),
        (autoscale_reply(plan=PLAN_DEV), False),  # the setting is another plan's
    )
    for reply, enabled in cases:
        runner = scale_runner((0, plan_json(PLAN_PROD), ""), autoscale=reply)
        run = run_task(
            "scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=True, instances=20
        )
        scale = run.envelope["outputs"]["scale"]
        assert run.envelope["status"] == "ok" and scale["autoscaleOverridden"] is False
        assert scale["autoscale"] == {"enabled": enabled, "minimum": None, "maximum": None}


def test_the_autoscale_bounds_are_the_widest_envelope_of_the_enabled_profiles(run_task):
    row = {
        "name": "auto",
        "properties": {
            "enabled": True,
            "targetResourceUri": plan_id(PLAN_PROD),
            "profiles": [
                {"name": "day", "capacity": {"minimum": "3", "maximum": "6", "default": "3"}},
                {"name": "night", "capacity": {"minimum": "1", "maximum": "2", "default": "1"}},
            ],
        },
    }
    runner = scale_runner((0, plan_json(PLAN_PROD), ""), autoscale=autoscale_reply(rows=[row]))
    run = run_task("scale", resource=app_plan(PLAN_PROD), runner=runner, dry_run=True, instances=6)
    assert run.envelope["outputs"]["scale"]["autoscale"] == {
        "enabled": True,
        "minimum": 1,
        "maximum": 6,
    }


def test_an_autoscale_list_that_cannot_be_read_stops_the_scale_before_any_write(run_task):
    err = 'ERROR: Forbidden({"error":{"code":"AuthorizationFailed","message":"no"}})\n'
    runner = scale_runner((0, plan_json(PLAN_PROD), ""), autoscale=(1, "", err))
    run = run_task("scale", resource=app_plan(PLAN_PROD), runner=runner, instances=3)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "permission_denied"
    assert not any(is_write(c) for c in run.calls)


def test_a_refused_scale_fails_with_the_action_to_grant(run_task):
    run = run_task(
        "scale", resource=app_plan(PLAN_DEV), scenario="write-forbidden", instances=3, sku="S3"
    )
    assert (
        run.errors[0]["code"] == "permission_denied"
        and "Microsoft.Web/serverfarms/write" in run.errors[0]["message"]
    )


def test_the_dev_plan_scenario_refuses_a_count_beyond_its_autoscale_maximum(run_task):
    run = run_task(
        "scale", resource=app_plan(PLAN_DEV), scenario="broken", dry_run=True, instances=5
    )
    assert run.errors[0]["code"] == "invalid_input" and "2 to 3" in run.errors[0]["message"]
    override = run_task(
        "scale",
        resource=app_plan(PLAN_DEV),
        scenario="broken",
        dry_run=True,
        instances=5,
        override_autoscale=True,
    )
    assert override.envelope["outputs"]["scale"]["autoscale"] == {
        "enabled": True,
        "minimum": 2,
        "maximum": 3,
    }


# --- recorded scenarios of the writes: applied against the stateless stub ---------------------------------


def test_every_write_of_the_healthy_scenario_is_a_no_op_the_stub_can_answer(run_task):
    for script, resource, kw in (
        ("restart", web_app(API), {}),
        ("start", web_app(API), {}),
        ("swap_slot", web_app(API), {"source_slot": "staging"}),
        ("scale", app_plan(PLAN_PROD), {"instances": 2, "sku": "P1v3"}),
    ):
        run = run_task(script, resource=resource, scenario="healthy", **kw)
        assert run.envelope["status"] == "ok", (script, run.errors)
        (output,) = run.envelope["outputs"].values()
        assert output["applied"] is True
    stopped = run_task("stop", resource=web_app(WEB), scenario="broken")
    assert stopped.envelope["outputs"]["power"]["reachedExpected"] is True


# --- permissions are tested, not guessed ------------------------------------------------------------------


def action_of(call):
    """The ARM action a recorded az call needs."""
    if call[:3] == ["monitor", "metrics", "list"]:
        return "Microsoft.Insights/metrics/read"
    if call[:3] == ["appservice", "plan", "update"]:
        return "Microsoft.Web/serverfarms/write"
    if call[:2] in (["webapp", "restart"], ["webapp", "start"], ["webapp", "stop"]):
        return f"Microsoft.Web/sites/{call[1]}/action"
    if call[:4] == ["webapp", "deployment", "slot", "swap"]:
        return "Microsoft.Web/sites/slots/slotsswap/action"
    if call[0] == "rest":
        assert call[1:3] == ["--method", "get"], call
        url = call[4].split("?")[0]
        for tail, action in (
            (
                "/providers/Microsoft.ResourceHealth/availabilityStatuses/current",
                "Microsoft.ResourceHealth/availabilityStatuses/read",
            ),
            (
                "/providers/Microsoft.Insights/autoscalesettings",
                "Microsoft.Insights/autoscalesettings/read",
            ),
            ("/config/web", "Microsoft.Web/sites/config/read"),
            ("/slots", "Microsoft.Web/sites/slots/read"),
            ("/deployments", "Microsoft.Web/sites/deployments/read"),
        ):
            if url.endswith(tail):
                return action
        if "/providers/Microsoft.Web/sites/" in url:
            return "Microsoft.Web/sites/read"
        if "/providers/Microsoft.Web/serverfarms/" in url:
            return "Microsoft.Web/serverfarms/read"
    raise AssertionError(f"no action known for {call}")


def every_run(run_task):
    """(task name, calls) of every task against every scenario and target kind that exercises it."""
    results = []
    reads = {name: script for name, (script, _, _) in TASKS.items()}
    for task, script in reads.items():
        kind = TASKS[task][2]
        targets = {
            "app": [web_app(API), web_app(WEB)],
            "func": [web_app(FUNC_PROD)],
            "either": [app_plan(PLAN_PROD), app_plan(PLAN_DEV), web_app(API), web_app(WEB)],
        }[kind]
        for resource in targets:
            scenario = "healthy" if GROUP[resource["name"]] == PROD else "broken"
            results.append((task, run_task(script, resource=resource, scenario=scenario).calls))
        if kind == "app":
            results.append(
                (task, run_task(script, resource=web_app(WEB), scenario="partial").calls)
            )
    for task, script, resource, scenario, kw in (
        ("restart", "restart", web_app(API), "healthy", {}),
        ("start", "start", web_app(API), "healthy", {}),
        ("stop", "stop", web_app(WEB), "broken", {}),
        ("swap-slot", "swap_slot", web_app(API), "healthy", {"source_slot": "staging"}),
        ("scale", "scale", app_plan(PLAN_PROD), "healthy", {"instances": 2, "sku": "P1v3"}),
    ):
        results.append((task, run_task(script, resource=resource, scenario=scenario, **kw).calls))
    return results


def test_the_az_calls_of_each_task_are_covered_by_its_declared_permissions_and_each_is_used(
    run_task,
):
    used = {}
    for task, calls in every_run(run_task):
        used.setdefault(task, set()).update(action_of(c) for c in calls)
    for name, spec in tasks_by_name().items():
        declared = {p["action"] for p in spec["permissions"]}
        assert used[name] <= declared, (name, used[name] - declared)
        assert declared <= used[name], (name, "declared but never used", declared - used[name])


def test_no_task_reads_an_app_setting_a_connection_string_or_a_credential(run_task):
    banned = (
        "appsettings",
        "connection-string",
        "connectionstrings",
        "publish",
        "publishing",
        "credentials",
        "listkeys",
        "config/list",
        "list-publishing",
        "ftp",
    )
    for task, calls in every_run(run_task):
        for call in calls:
            text = " ".join(call).lower()
            assert not any(word in text for word in banned), (task, call)
            assert call[:2] not in (
                ["webapp", "show"],
                ["functionapp", "show"],
                ["webapp", "config"],
                ["functionapp", "config"],
            ), call
            if call[0] == "rest":
                assert (
                    call[1:3] == ["--method", "get"]
                    and "--body" not in call
                    and "--headers" not in call
                )
            assert "--debug" not in call and "--verbose" not in call


def test_the_skills_code_names_no_call_that_returns_a_secret():
    import ast

    banned = (
        "appsettings",
        "connectionstrings",
        "list_publishing",
        "publishxml",
        "publishingcredentials",
        "listkeys",
        "config/list",
        "az webapp show",
        "az functionapp show",
    )
    for path in sorted((SKILL / "lib").rglob("*.py")) + sorted((SKILL / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert not any(word in node.value.lower() for word in banned), (
                    path.name,
                    node.value,
                )


# --- the declared tasks, the types and the skill's own documents ---------------------------------------------

WRITES = {"restart", "start", "stop", "swap-slot", "scale"}


def seconds(text):
    return float(text.rstrip("s"))


def test_every_task_declares_readonly_and_a_write_declares_its_effects():
    tasks = tasks_by_name()
    assert set(tasks) == {*TASKS, *WRITES}
    for name, task in tasks.items():
        assert isinstance(task["readOnly"], bool)
        if name in WRITES:
            assert task["readOnly"] is False and task["dryRun"] is True
            assert isinstance(task["destructive"], bool) and isinstance(task["idempotent"], bool)
            assert task["effects"] and all(isinstance(e, str) and e for e in task["effects"])
        else:
            assert task["readOnly"] is True
            assert not {"effects", "destructive", "idempotent", "dryRun"} & set(task), name


def test_the_writes_flags_say_what_they_are():
    tasks = tasks_by_name()
    flags = {n: (tasks[n]["destructive"], tasks[n]["idempotent"]) for n in WRITES}
    assert flags == {
        "restart": (False, False),  # each call restarts again
        "start": (False, True),
        "stop": (True, True),  # takes the app offline
        "swap-slot": (False, False),  # swapping again swaps back
        "scale": (False, True),  # absolute values
    }


def test_the_writes_permissions_are_the_actions_of_the_call_and_of_its_read():
    tasks = tasks_by_name()
    actions = {n: {p["action"] for p in tasks[n]["permissions"]} for n in WRITES}
    assert actions["restart"] == {"Microsoft.Web/sites/read", "Microsoft.Web/sites/restart/action"}
    assert actions["scale"] == {
        "Microsoft.Web/serverfarms/read",
        "Microsoft.Web/serverfarms/write",
        "Microsoft.Insights/autoscalesettings/read",
    }
    assert "Microsoft.Web/sites/slots/slotsswap/action" in actions["swap-slot"]


def test_no_read_task_declares_a_write_or_an_action_and_no_task_a_data_action():
    for name, task in tasks_by_name().items():
        for permission in task["permissions"]:
            # the autoscale list is the subscription's: a setting can be in any resource group
            scoped = permission["action"] == "Microsoft.Insights/autoscalesettings/read"
            assert set(permission) == ({"action", "scope"} if scoped else {"action"})
            assert permission.get("scope", "subscription" if scoped else None) == (
                "subscription" if scoped else None
            )
            if name not in WRITES:
                assert permission["action"].endswith("/read"), (name, permission)


def test_the_default_settings_in_the_tests_match_the_declared_defaults():
    for name, spec in doc()["settings"].items():
        assert SETTINGS_ALL[name] == spec["default"], name
    assert set(doc()["settings"]) <= set(SETTINGS_ALL)


def test_a_task_timeout_covers_its_az_calls(run_task):
    """A read task: commandTimeout per call. A write task: the longest operationTimeout the setting
    allows, the plain reads at commandTimeout and every list at its page budget."""
    most = {}
    for task, calls in every_run(run_task):
        most[task] = max(most.get(task, 0), len(calls))
    for name, task in tasks_by_name().items():
        timeout = seconds(task.get("timeout", doc()["defaults"]["timeout"]))
        if name in WRITES:
            gets, lists = {"swap-slot": (2, 2), "scale": (2, 1)}.get(name, (2, 0))
            assert most[name] == gets + lists + 1, name  # the reads, plus the one write
            worst = ops.MAX_OPERATION + 60 * gets + arm.PAGE_BUDGET * lists
            assert timeout >= worst, (name, timeout, worst)
        else:
            assert timeout >= 60 * most[name], (name, timeout, most[name])
    assert seconds(doc()["defaults"]["timeout"]) >= 180


def test_operation_timeout_is_capped_and_the_cap_is_documented():
    assert ops.MAX_OPERATION == 600.0
    assert "at most 600s" in doc()["settings"]["operationTimeout"]["description"]
    assert ops.operation_timeout(SimpleNamespace(operation_timeout="600s")) == 600.0
    with pytest.raises(RwError):
        ops.operation_timeout(SimpleNamespace(operation_timeout="601s"))


def test_the_metric_tasks_budget_leaves_a_margin_inside_their_timeout():
    for name in ("http-metrics", "function-metrics", "plan-metrics"):
        assert seconds(tasks_by_name()[name]["timeout"]) >= usage.BUDGET + 30


def test_input_defaults_are_declared_and_the_required_ones_have_none():
    tasks = tasks_by_name()
    assert tasks["swap-slot"]["inputs"]["targetSlot"]["default"] == "production"
    assert "default" not in tasks["swap-slot"]["inputs"]["sourceSlot"]
    assert (
        tasks["scale"]["inputs"]["instances"]["required"] is False
        and tasks["scale"]["inputs"]["sku"]["required"] is False
    )
    assert tasks["deployments"]["inputs"]["window"]["default"] == "7d"
    for name in ("http-metrics", "function-metrics", "plan-metrics"):
        assert tasks[name]["inputs"]["window"]["default"] == "24h"


def test_the_target_types_are_children_of_the_resource_group_in_the_types_file():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["app-service-plan"] == {
        "parent": "resourcegroup",
        "plural": "app-service-plans",
        "native": {"provider": "Microsoft.Web", "type": "serverFarms"},
    }
    assert types["web-app"] == {
        "parent": "resourcegroup",
        "plural": "web-apps",
        "native": {"provider": "Microsoft.Web", "type": "sites"},
    }


def test_the_skill_applies_to_exactly_the_two_types():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == "azure-appservice-health" and len(front["description"]) <= 1024
    assert front["runwhen"]["appliesTo"] == {"types": ["azure/web-app", "azure/app-service-plan"]}
    assert len(text.splitlines()) <= 150
    for section in (
        "## When to use",
        "## What each task tells you",
        "## How to read the result",
        "## What to try next",
    ):
        assert section in text
    for name in tasks_by_name():
        assert f"`{name}`" in text, name


def test_the_skill_says_it_never_reads_app_settings_or_connection_strings():
    text = " ".join((SKILL / "SKILL.md").read_text().split())
    assert "never" in text and "app setting" in text and "connection string" in text
    for task in doc()["tasks"]:
        if task["name"] in ("app-state", "configuration"):
            assert "never read" in " ".join(task["description"].split())


def test_sibling_skills_named_in_the_skill_exist():
    text = (SKILL / "SKILL.md").read_text()
    for plugin, skill, task in re.findall(r"`(azure|kubernetes):([a-z0-9-]+)/([a-z0-9-]+)`", text):
        tasks = yaml.safe_load(
            (PLUGIN.parent / plugin / "skills" / skill / "tasks.yaml").read_text()
        )["tasks"]
        assert task in {t["name"] for t in tasks}, (plugin, skill, task)
    for plugin, skill in re.findall(r"`(azure|kubernetes):([a-z0-9-]+)`", text):
        assert (PLUGIN.parent / plugin / "skills" / skill).is_dir()


def test_nothing_in_the_outputs_names_a_verdict_a_severity_or_next_steps(run_task):
    banned = re.compile(r"severity|nextstep|next_step|\"title\"|\"issue", re.I)
    for task, script in ((t, s[0]) for t, s in TASKS.items()):
        for scenario, resource in (
            ("healthy", TARGETS[TASKS[task][2]][0]),
            ("broken", TARGETS[TASKS[task][2]][1]),
        ):
            run = run_task(script, resource=resource, scenario=scenario)
            assert not banned.search(json.dumps(run.envelope["outputs"])), task


def test_fixture_expectations_name_only_tasks_that_exist():
    for fixture in SKILL.glob("tests/*/*.yaml"):
        assert fixture.parent.name in tasks_by_name(), fixture


# --- review fixes: runtime, swap state, pagination, flags --------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("NODE|20-lts", "NODE|20-lts"),
        ("PYTHON|3.12", "PYTHON|3.12"),
        ("DOTNETCORE|8.0", "DOTNETCORE|8.0"),
        (
            "COMPOSE|dmVyc2lvbjogJzMnCnNlcnZpY2VzOgogIGRiOgogICAgZW52aXJvbm1lbnQ6IFNFQ1JFVD0xMjM=",
            "COMPOSE|",
        ),
        ("compose|abc", "COMPOSE|"),
        ("KUBE|YXBpVmVyc2lvbjogdjEKa2luZDogU2VjcmV0", "KUBE|"),
        ("DOCKER|registry.example.com/team/app:1.4", "DOCKER|registry.example.com/team/app:1.4"),
        (
            "DOCKER|user:pass@registry.example.com/team/app:1.4",
            "DOCKER|registry.example.com/team/app:1.4",
        ),
        (
            "DOCKER|https://user:pass@registry.example.com/app:1",
            "DOCKER|https://registry.example.com/app:1",
        ),
        ("DOCKER|token@registry.example.com/app", "DOCKER|registry.example.com/app"),
        ("DOCKER|nginx@sha256:0123abcd", "DOCKER|nginx@sha256:0123abcd"),
        (
            "DOCKER|registry.example.com/app@sha256:0123abcd",
            "DOCKER|registry.example.com/app@sha256:0123abcd",
        ),
        ("", None),
        (None, None),
    ],
)
def test_the_runtime_is_the_stack_or_an_image_without_credentials(value, expected):
    assert site.runtime(value) == expected


def test_a_runtime_is_cut_at_256_characters_and_a_secret_in_a_compose_file_never_leaves(run_task):
    assert len(site.runtime("DOCKER|" + "a" * 500)) == 256
    cfg = json.loads(scenario_file("healthy", f"config-{API}.json"))
    cfg["properties"]["linuxFxVersion"] = "COMPOSE|" + "U0VDUkVULVNFTlRJTkVM" * 5
    answers = {
        get_argv(site_id(API)): (0, site_json(API), ""),
        get_argv(site_id(API) + "/config/web"): (0, json.dumps(cfg), ""),
    }
    data = run_task("configuration", resource=web_app(API), runner=Seq(answers))
    assert data.envelope["outputs"]["configuration"]["runtime"] == "COMPOSE|"
    assert "U0VDUkVULVNFTlRJTkVM" not in json.dumps(data.envelope)


def test_limited_is_true_only_for_the_limited_availability_state(run_task):
    for state, limited in (("Normal", False), ("Limited", True), ("DisasterRecoveryMode", False)):
        runner = Seq({SITE: (0, site_json(API, availabilityState=state), "")})
        data = run_task("app_state", resource=web_app(API), runner=runner).envelope["outputs"][
            "appState"
        ]
        assert (data["availabilityState"], data["limited"]) == (state, limited)


def test_at_maximum_is_given_for_an_enabled_setting_only(run_task):
    def row(name, enabled):
        return {
            "name": name,
            "properties": {
                "enabled": enabled,
                "targetResourceUri": plan_id(PLAN_PROD),
                "profiles": [
                    {"name": "p", "capacity": {"minimum": "1", "maximum": "2", "default": "1"}}
                ],
            },
        }

    runner = autoscale_runner([row("on", True), row("off", False)])
    data = run_task("autoscale", resource=app_plan(PLAN_PROD), runner=runner).envelope["outputs"][
        "autoscale"
    ]
    by_name = {s["name"]: s["profiles"][0]["atMaximum"] for s in data["settings"]}
    assert by_name == {"on": True, "off": None}  # the plan runs 2 instances: at the maximum of 2


def test_the_dev_plans_setting_is_found_though_it_is_not_in_the_plans_resource_group(go):
    run = go("autoscale", "broken")
    assert run.calls[-1][4] == (
        f"/subscriptions/{SUB}/providers/Microsoft.Insights/autoscalesettings?api-version=2022-10-01"
    )
    data = out(run, "autoscale")
    assert data["configured"] is True and data["settings"][0]["name"] == "autoscale-plan-orders-dev"
    # the stub's list also holds the prod plan's setting and a VMSS's: only the plan's own is returned
    assert data["total"] == 1


# swap state


def swap_runner(site_props=None, slot_props=None):
    slots = json.loads(slots_json(("staging", "Running")))
    slots["value"][0]["properties"].update(slot_props or {})
    return Seq(
        {SITE: (0, site_json(API, **(site_props or {})), ""), SLOTS: (0, json.dumps(slots), "")}
    )


@pytest.mark.parametrize(
    "site_props,slot_props",
    [
        (
            {"slotSwapStatus": {"sourceSlotName": "staging", "destinationSlotName": "production"}},
            {},
        ),
        ({"targetSwapSlot": "staging"}, {}),
        ({}, {"slotSwapStatus": {"timestampUtc": "2026-11-04T03:41:12Z"}}),
        ({}, {"targetSwapSlot": "production"}),
    ],
)
@pytest.mark.parametrize("dry", [True, False])
def test_a_swap_under_way_is_refused_in_a_dry_run_and_a_real_one(
    run_task, site_props, slot_props, dry
):
    runner = swap_runner(site_props, slot_props)
    run = run_task(
        "swap_slot", resource=web_app(API), runner=runner, dry_run=dry, source_slot="staging"
    )
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert "already under way" in run.errors[0]["message"]
    assert not any(is_write(c) for c in run.calls)


def test_the_before_state_of_a_swap_carries_the_swap_facts(run_task):
    run = run_task(
        "swap_slot",
        resource=web_app(API),
        runner=swap_runner(),
        dry_run=True,
        source_slot="staging",
    )
    before = run.envelope["outputs"]["swap"]["before"]
    for side in ("source", "target"):
        assert before[side]["swapInProgress"] is False and before[side]["targetSwapSlot"] is None


def test_the_swap_busy_scenario_is_refused(run_task):
    run = run_task(
        "swap_slot",
        resource=web_app(API),
        scenario="swap-busy",
        dry_run=True,
        source_slot="staging",
    )
    assert run.errors[0]["code"] == "invalid_input"


def test_the_skill_does_not_call_swap_its_own_inverse():
    text = " ".join((SKILL / "SKILL.md").read_text().split()) + " ".join(
        t["description"] for t in doc()["tasks"]
    )
    assert "own inverse" not in text and "swaps back" not in text


# pagination


def test_a_list_stops_asking_for_pages_when_the_budget_cannot_fit_another_call(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(arm.time, "monotonic", lambda: clock["now"])
    pages = []

    class Ctx:
        settings = SimpleNamespace(command_timeout="60s")

        def run(self, argv, **kw):
            pages.append(argv[-3])
            clock["now"] += 50.0
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"value": [{"name": "r"}], "nextLink": "https://example/next"}),
                "",
            )

    rows, complete = arm.get_list(Ctx(), "/subscriptions/x/things")
    # a page at 0s, one at 50s (50 + 60 fits in 120s), none at 100s (100 + 60 does not)
    assert len(pages) == 2 and complete is False and len(rows) == 2


def test_a_list_that_ends_inside_the_budget_is_complete(monkeypatch):
    class Ctx:
        settings = SimpleNamespace(command_timeout="60s")

        def run(self, argv, **kw):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"value": [{"name": "r"}]}), "")

    assert arm.get_list(Ctx(), "/x") == ([{"name": "r"}], True)


def test_the_first_page_is_always_asked_even_with_a_tiny_budget():
    class Ctx:
        settings = SimpleNamespace(command_timeout="60s")

        def run(self, argv, **kw):
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"value": [{"name": "r"}], "nextLink": "n"}), ""
            )

    rows, complete = arm.get_list(Ctx(), "/x", budget=1.0)
    assert len(rows) == 1 and complete is False
