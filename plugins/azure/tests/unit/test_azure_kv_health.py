"""azure-kv-health against the stub scenarios and hand-made az replies: facts, bounds, every
failure as a code, and the permissions each task declares."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from azure_kv_health import audit, expiry, metrics
from azure_kv_health.common import clip, read_settings
from azure_kv_health.vault import read_thresholds
from conftest import SETTINGS, SUB, Replies, vault

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "azure-kv-health"
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
PROD, DEV = "kv-orders-prod", "kv-orders-dev"
DEV_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/Microsoft.KeyVault/vaults/{DEV}"
)
PROD_ID = (
    f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.KeyVault/vaults/{PROD}"
)
STUB = PLUGIN / "tests" / "stub" / "scenarios"
SHOW_DEV = ("keyvault", "show", "--name", DEV, "-g", "rg-orders-dev", "--subscription", SUB)
SHOW_PROD = ("keyvault", "show", "--name", PROD, "-g", "rg-orders-prod", "--subscription", SUB)

TASKS = {
    "resource-health": ("resource_health", "health"),
    "availability": ("availability", "availability"),
    "configuration": ("configuration", "configuration"),
    "expiring-items": ("expiring_items", "expiry"),
    "audit-failures": ("audit_failures", "audit"),
    "performance": ("performance", "performance"),
}


def stub_json(scenario, name):
    return (STUB / scenario / name).read_text()


@pytest.fixture
def go(run_script):
    """go(task, scenario, vault name or None for the scenario's default, **inputs)"""

    def run(task, scenario, name=None, **kw):
        script, _ = TASKS[task]
        name = name or (PROD if scenario == "healthy" else DEV)
        return run_script(
            "azure-kv-health", script, scenario=scenario, resource=vault(name), now=NOW, **kw
        )

    return run


def out(run, task):
    return run.envelope["outputs"][TASKS[task][1] if task in TASKS else task]


# --- schemas ----------------------------------------------------------------------------------


def doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def schema_of(task):
    ref = next(t for t in doc()["tasks"] if t["name"] == task)["outputs"][TASKS[task][1]]["schema"]
    return json.loads((SKILL / ref).read_text())


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize("scenario", ["healthy", "broken", "no-diagnostics"])
def test_every_output_validates_against_its_schema(go, task, scenario):
    run = go(task, scenario)
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
    run = go("resource-health", "healthy")
    health = out(run, "resource-health")
    assert (health["availabilityState"], health["healthy"]) == ("Available", True)
    assert health["vault"] == PROD and health["serviceImpactingEvents"] == 0
    assert health["reasonType"] is None and health["truncated"] is False
    assert [c[:2] for c in run.calls] == [["keyvault", "show"], ["rest", "--method"]]


def test_resource_health_degraded(go):
    health = out(go("resource-health", "broken"), "resource-health")
    assert (health["availabilityState"], health["healthy"]) == ("Degraded", False)
    assert health["serviceImpactingEvents"] == 1 and health["reasonType"] == "Unplanned"
    assert health["reasonChronicity"] == "Transient"
    assert "reduced availability" in health["summary"]


def test_resource_health_asks_for_the_resource_ids_current_status_and_registers_nothing(go):
    run = go("resource-health", "healthy")
    method, url = run.calls[1][2], run.calls[1][4]
    assert method == "get"
    # a relative ARM id: az rest sends it to the active cloud's Resource Manager endpoint
    assert url == (
        f"{PROD_ID}/providers/Microsoft.ResourceHealth/"
        "availabilityStatuses/current?api-version=2022-10-01"
    )
    assert not any("register" in " ".join(c) for c in run.calls)


def test_resource_health_text_is_cut_at_1024_characters():
    assert clip("x" * 1024) == ("x" * 1024, False)
    text, cut = clip("x" * 2000)
    assert len(text) == 1024 and cut is True and text.endswith("...")
    assert clip(None) == ("", False)


HEALTH_URL = f"{DEV_ID}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01"


def test_a_reply_without_a_state_is_an_error_not_a_healthy_vault(go):
    show = stub_json("broken", "keyvault-show.json")
    runner = Replies(
        {
            SHOW_DEV: (0, show, ""),
            ("rest", "--method", "get", "--url", HEALTH_URL): (0, "{}", ""),
        }
    )
    run = go("resource-health", "broken", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_an_unregistered_resource_provider_is_a_dependency_error(go):
    show = stub_json("broken", "keyvault-show.json")
    err = (
        'ERROR: Bad Request({"error":{"code":"MissingSubscriptionRegistration","message":'
        "\"The subscription is not registered to use namespace 'Microsoft.ResourceHealth'.\"}})\n"
    )
    runner = Replies(
        {
            SHOW_DEV: (0, show, ""),
            ("rest", "--method", "get", "--url", HEALTH_URL): (1, "", err),
        }
    )
    run = go("resource-health", "broken", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "dependency"


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


@pytest.mark.parametrize("task", TASKS)
def test_a_target_that_is_not_a_key_vault_is_invalid_input_before_any_az_call(run_script, task):
    from conftest import SUBSCRIPTION

    run = run_script("azure-kv-health", TASKS[task][0], resource=SUBSCRIPTION, now=NOW)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


def test_a_target_without_a_subscription_is_invalid_input(run_script):
    resource = {"path": "x", "type": "azure/keyvault", "name": DEV}
    run = run_script("azure-kv-health", "configuration", resource=resource, now=NOW)
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


def test_the_warning_az_prints_on_a_good_command_is_not_an_error(go):
    show = stub_json("healthy", "keyvault-show.json")
    warning = "WARNING: A new Azure CLI version is available.\nWARNING: Forbidden TooManyRequests\n"
    runner = Replies({SHOW_PROD: (0, show, warning)})
    run = go("configuration", "healthy", runner=runner)
    assert run.envelope["status"] == "ok" and run.errors == []


# --- availability ----------------------------------------------------------------------------------


def test_availability_healthy(go):
    a = out(go("availability", "healthy"), "availability")
    assert (a["hasData"], a["belowThreshold"], a["grainsBelowThreshold"]) == (True, False, 0)
    assert (a["grains"], a["grainsWithData"], a["grainsMissing"]) == (24, 24, 0)
    assert (a["min"], a["max"], a["last"]) == (100.0, 100.0, 100.0)
    assert len(a["points"]) == 24 and a["pointsOmitted"] == 0


def test_availability_broken_sees_a_dip_that_is_not_the_last_interval(go):
    a = out(go("availability", "broken"), "availability")
    assert a["belowThreshold"] is True and a["grainsBelowThreshold"] == 3
    assert a["min"] == 93.5 and a["last"] == 100.0  # the classic only looked at the last point
    assert (a["grainsWithData"], a["grainsMissing"]) == (23, 1)
    assert 93.5 in [p["value"] for p in a["points"]]


def test_availability_asks_for_the_window_that_ends_at_the_reference_time(go):
    run = go("availability", "healthy", window="6h")
    assert run.calls[1] == [
        "monitor", "metrics", "list", "--resource", PROD_ID, "--metrics", "Availability",
        "--aggregation", "Average", "--interval", "PT1H",
        "--start-time", "2026-11-04T06:00:00Z", "--end-time", "2026-11-04T12:00:00Z",
        "--subscription", SUB,
    ]  # fmt: skip
    assert out(run, "availability")["windowSeconds"] == 21600


def test_the_threshold_and_interval_are_settings(go):
    # PT30M changes the argv, which the broken scenario has no answer for: use the replies helper
    replies = with_metric(metric_reply(AVAIL_BROKEN), interval="PT30M")
    run = go(
        "availability",
        "broken",
        runner=replies,
        settings={"availabilityMinPercent": 95, "metricInterval": "PT30M"},
    )
    a = out(run, "availability")
    assert a["thresholdPercent"] == 95 and a["interval"] == "PT30M"
    assert a["grainsBelowThreshold"] == 1 and "PT30M" in run.calls[1]


AVAIL_BROKEN = json.loads(stub_json("broken", "metrics-availability.json"))["value"][0][
    "timeseries"
][0]["data"]


def metric_reply(data, error_code="Success"):
    return json.dumps(
        {
            "value": [
                {
                    "errorCode": error_code,
                    "name": {"value": "Availability"},
                    "timeseries": [{"data": data}] if data is not None else [],
                }
            ]
        }
    )


def with_metric(reply, name=DEV, interval="PT1H", start="2026-11-03T12:00:00Z"):
    show = stub_json("broken", "keyvault-show.json")
    metrics_argv = (
        "monitor", "metrics", "list", "--resource", DEV_ID, "--metrics", "Availability",
        "--aggregation", "Average", "--interval", interval,
        "--start-time", start, "--end-time", "2026-11-04T12:00:00Z",
        "--subscription", SUB,
    )  # fmt: skip
    return Replies(
        {
            SHOW_DEV: (0, show, ""),
            metrics_argv: (0, reply, ""),
        }
    )


def test_no_datapoints_at_all_is_no_data_and_not_a_healthy_vault(go):
    run = go("availability", "broken", runner=with_metric(metric_reply(None)))
    a = out(run, "availability")
    assert (a["hasData"], a["belowThreshold"], a["min"], a["last"]) == (False, False, None, None)
    assert (a["grains"], a["points"]) == (0, [])


def test_grains_without_a_value_are_counted_and_never_read_as_healthy_or_zero(go):
    data = [
        {"timeStamp": "2026-11-04T01:00:00+00:00"},
        {"timeStamp": "2026-11-04T02:00:00+00:00", "average": 99.0},
    ]
    run = go("availability", "broken", runner=with_metric(metric_reply(data)))
    a = out(run, "availability")
    assert (a["grains"], a["grainsWithData"], a["grainsMissing"]) == (2, 1, 1)
    assert a["min"] == 99.0 and a["belowThreshold"] is True


def test_more_than_fifty_intervals_are_cut_but_the_aggregates_cover_them_all(go):
    data = [
        {"timeStamp": f"2026-11-0{1 + i // 24}T{i % 24:02d}:00:00+00:00", "average": 100.0}
        for i in range(100)
    ]
    data[3]["average"] = 50.0  # old, outside the newest 50
    replies = with_metric(metric_reply(data), start="2026-10-30T12:00:00Z")
    a = out(go("availability", "broken", runner=replies, window="5d"), "availability")
    assert len(a["points"]) == 50 and a["pointsOmitted"] == 50
    assert a["min"] == 50.0 and a["grainsBelowThreshold"] == 1
    assert a["points"][-1]["timestamp"] == data[-1]["timeStamp"]


def test_an_error_code_from_azure_monitor_is_an_error(go):
    run = go("availability", "broken", runner=with_metric(metric_reply([], "InvalidSamplingType")))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


@pytest.mark.parametrize("task", ["availability", "audit-failures", "performance"])
@pytest.mark.parametrize("bad", ["", "soon", "30s", "31d", "0h", "-1h", "24"])
def test_a_bad_window_is_invalid_input_before_any_az_call(go, task, bad):
    run = go(task, "healthy", window=bad)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("availabilityMinPercent", 101),
        ("availabilityMinPercent", True),
        ("metricInterval", "PT2H"),
        ("requestThreshold", -1),
        ("latencyThresholdMs", "fast"),
        ("maxItems", 0),
        ("commandTimeout", "0s"),
    ],
)
def test_a_bad_setting_is_invalid_input_before_any_az_call(go, setting, value):
    run = go("availability", "healthy", settings={setting: value})
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


# --- configuration ----------------------------------------------------------------------------------


def test_configuration_of_a_protected_vault(go):
    c = out(go("configuration", "healthy"), "configuration")
    assert c["softDeleteEnabled"] is True and c["softDeleteRetentionDays"] == 90
    assert c["purgeProtectionEnabled"] is True and c["rbacAuthorization"] is True
    assert (c["publicNetworkAccess"], c["networkAclDefaultAction"]) == ("Disabled", "Deny")
    assert (
        c["ipRuleCount"],
        c["virtualNetworkRuleCount"],
        c["privateEndpointConnectionCount"],
    ) == (
        1,
        0,
        1,
    )
    assert (c["resourceGroup"], c["location"], c["sku"]) == ("rg-orders-prod", "eastus", "standard")


def test_configuration_of_an_exposed_vault(go):
    c = out(go("configuration", "broken"), "configuration")
    assert c["purgeProtectionEnabled"] is False  # null: never enabled
    assert c["publicNetworkAccess"] == "Enabled" and c["networkAclDefaultAction"] is None
    assert c["softDeleteRetentionDays"] == 7 and c["privateEndpointConnectionCount"] == 0


def test_soft_delete_is_off_only_when_azure_says_false_not_when_it_says_nothing(go):
    def vault_with(**props):
        document = json.loads(stub_json("broken", "keyvault-show.json"))
        document["properties"].update(props)
        runner = Replies(
            {
                SHOW_DEV: (
                    0,
                    json.dumps(document),
                    "",
                )
            }
        )
        return out(go("configuration", "broken", runner=runner), "configuration")

    assert vault_with(enableSoftDelete=None)["softDeleteEnabled"] is True
    assert vault_with()["softDeleteEnabled"] is True
    assert vault_with(enableSoftDelete=False)["softDeleteEnabled"] is False
    assert vault_with(enablePurgeProtection=True)["purgeProtectionEnabled"] is True
    assert vault_with(enablePurgeProtection=False)["purgeProtectionEnabled"] is False


def test_configuration_returns_no_access_policy_content(go):
    document = json.loads(stub_json("broken", "keyvault-show.json"))
    document["properties"]["accessPolicies"] = [
        {"objectId": "OBJ", "permissions": {"secrets": ["get"]}}
    ]
    runner = Replies({SHOW_DEV: (0, json.dumps(document), "")})
    c = out(go("configuration", "broken", runner=runner), "configuration")
    assert c["accessPolicyCount"] == 1 and "OBJ" not in json.dumps(c)


# --- expiring-items ---------------------------------------------------------------------------------


def test_expiring_items_healthy_has_nothing_within_the_threshold(go):
    e = out(go("expiring-items", "healthy"), "expiry")
    assert e["items"] == {"items": [], "total": 0, "omitted": 0}
    assert e["scanned"] == {"secrets": 3, "certificates": 1, "keys": 1}
    assert e["noExpiry"] == {"secrets": 1, "certificates": 0, "keys": 1}


def test_expiring_items_broken_lists_expired_and_expiring_soonest_first(go):
    e = out(go("expiring-items", "broken"), "expiry")
    rows = [(i["kind"], i["name"], i["daysRemaining"], i["expired"]) for i in e["items"]["items"]]
    assert rows == [
        ("secret", "db-password", -16, True),  # 2026-10-20 is 15.5 days before NOW: rounded down
        ("secret", "retired-key", 5, False),
        ("certificate", "tls-orders-dev", 7, False),
        ("secret", "smtp-token", 13, False),
        ("key", "signing-key", 25, False),
    ]
    assert e["expired"] == 1 and e["items"]["total"] == 5
    assert next(i for i in e["items"]["items"] if i["name"] == "retired-key")["enabled"] is False
    assert "session-secret" not in json.dumps(e["items"])  # 2027: beyond the threshold


def test_the_threshold_is_an_input(go):
    e = out(go("expiring-items", "broken", threshold_days=10), "expiry")
    assert [i["name"] for i in e["items"]["items"]] == [
        "db-password",
        "retired-key",
        "tls-orders-dev",
    ]
    assert out(go("expiring-items", "broken", threshold_days=0), "expiry")["items"]["total"] == 1


@pytest.mark.parametrize("bad", [-1, 3651, True, "31", 1.5])
def test_a_bad_threshold_is_invalid_input(go, bad):
    run = go("expiring-items", "broken", threshold_days=bad)
    assert run.errors[0]["code"] == "invalid_input"


def test_max_items_cuts_the_list_and_reports_the_rest(go):
    e = out(go("expiring-items", "broken", settings={"maxItems": 2}), "expiry")
    assert len(e["items"]["items"]) == 2 and e["items"]["total"] == 5 and e["items"]["omitted"] == 3


def test_only_names_and_dates_leave_expiring_items(go):
    run = go("expiring-items", "broken")
    text = json.dumps(run.envelope["outputs"])
    for leaked in ("https://", "x509", "contentType", "tags", "thumbprint"):
        assert leaked.lower() not in text.lower()
    assert run.calls[1:] == [
        ["keyvault", "secret", "list", "--vault-name", DEV],
        ["keyvault", "certificate", "list", "--vault-name", DEV],
        ["keyvault", "key", "list", "--vault-name", DEV],
    ]


def lists_with(**failing):
    """Replies for the broken vault where the named kinds fail with the given stderr."""
    show = stub_json("broken", "keyvault-show.json")
    replies = {SHOW_DEV: (0, show, "")}
    for argv, file, kind in (
        (("keyvault", "secret", "list", "--vault-name", DEV), "secrets.json", "secrets"),
        (("keyvault", "certificate", "list", "--vault-name", DEV), "certificates.json", "certificates"),
        (("keyvault", "key", "list", "--vault-name", DEV), "keys.json", "keys"),
    ):  # fmt: skip
        replies[argv] = (
            (1, "", failing[kind]) if kind in failing else (0, stub_json("broken", file), "")
        )
    return Replies(replies)


FORBIDDEN = (
    "ERROR: (Forbidden) Caller is not authorized to perform action on resource.\nCode: Forbidden\n"
)


def test_one_kind_that_cannot_be_listed_is_a_partial_result_with_the_others(go):
    run = go("expiring-items", "broken", runner=lists_with(secrets=FORBIDDEN))
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "secrets")]
    e = out(run, "expiring-items")
    assert e["scanned"] == {"secrets": None, "certificates": 1, "keys": 1}
    assert e["noExpiry"]["secrets"] is None
    assert [i["name"] for i in e["items"]["items"]] == ["tls-orders-dev", "signing-key"]


def test_when_nothing_can_be_listed_the_run_fails_instead_of_reporting_no_expiring_items(go):
    run = go("expiring-items", "data-plane-forbidden")
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert run.errors[0]["code"] == "permission_denied"
    assert "not authorized" in run.errors[0]["message"]


def test_a_vault_firewall_refusal_is_unreachable_not_permission_denied(go):
    firewall = (
        "ERROR: (Forbidden) Client address is not authorized and caller is not a trusted service.\n"
        'InnerError: {"code": "ForbiddenByFirewall"}\n'
    )
    run = go(
        "expiring-items",
        "broken",
        runner=lists_with(secrets=firewall, certificates=firewall, keys=firewall),
    )
    assert run.errors[0]["code"] == "unreachable"


def test_classify_handles_naive_unreadable_and_missing_expiry():
    rows = [
        {"name": "a", "attributes": {"expires": "2026-11-05T00:00:00"}},  # no offset: UTC
        {"name": "b", "attributes": {"expires": "2026-11-05T00:00:00Z"}},
        {"name": "c", "attributes": {"expires": "garbage"}},
        {"name": "d", "attributes": {}},
        {"name": "e"},
    ]
    got = expiry.classify(rows, "secret", NOW, 31)
    assert [i["name"] for i in got.items] == ["a", "b"] and got.scanned == 5 and got.no_expiry == 3
    assert got.unreadable == [{"kind": "secret", "name": "c"}]
    assert got.items[0]["daysRemaining"] == 0 and got.items[0]["expired"] is False


# --- audit-failures ---------------------------------------------------------------------------------


def test_audit_failures_groups_the_failed_operations(go):
    a = out(go("audit-failures", "broken"), "audit")
    assert a["auditLoggingToWorkspace"] is True and a["workspacesQueried"] == 1
    assert a["failureCount"] == 50
    assert [(r["operation"], r["httpStatusCode"], r["count"]) for r in a["failures"]["items"]] == [
        ("SecretGet", 403, 41),
        ("SecretList", 429, 7),
        ("KeyGet", 404, 2),
    ]
    assert a["diagnosticSettings"] == [
        {
            "name": "kv-audit",
            "auditEvent": True,
            "logAnalytics": True,
            "table": "AzureDiagnostics",
            "storageAccount": False,
            "eventHub": False,
        }
    ]


def test_audit_failures_healthy_says_none_and_means_it(go):
    a = out(go("audit-failures", "healthy"), "audit")
    assert a["workspacesQueried"] == 1 and a["failureCount"] == 0
    assert a["failures"] == {"items": [], "total": 0, "omitted": 0}


def test_a_vault_without_audit_logging_to_a_workspace_is_not_reported_as_having_no_failures(go):
    run = go("audit-failures", "no-diagnostics")
    a = out(run, "audit")
    assert run.envelope["status"] == "ok"
    assert a["diagnosticSettings"] == [] and a["auditLoggingToWorkspace"] is False
    assert (a["workspacesQueried"], a["failureCount"], a["failures"]) == (0, None, None)
    assert [c[:2] for c in run.calls] == [["keyvault", "show"], ["monitor", "diagnostic-settings"]]


def test_the_log_query_is_scoped_to_the_vault_and_the_window(go):
    run = go("audit-failures", "broken", window="2h")
    last = run.calls[-1]
    body = json.loads(last[last.index("--body") + 1])
    assert body["timespan"] == "2026-11-04T10:00:00Z/2026-11-04T12:00:00Z"
    assert f'ResourceId =~ "{DEV_ID}"' in body["query"]
    assert "httpStatusCode_d) >= 400" in body["query"] and body["query"].endswith("take 51")
    assert last[last.index("--url") + 1] == (
        "https://api.loganalytics.io/v1/workspaces/0b6c2f1e-3d4a-4e5f-9a7b-1c2d3e4f5a6b/query"
    )


def test_the_settings_list_may_be_wrapped_in_value():
    setting = json.loads(stub_json("broken", "diagnostic-settings.json"))[0]
    assert len(audit.diagnostic_settings({"value": [setting, {**setting, "name": "second"}]})) == 2
    assert audit.diagnostic_settings([setting]) == [setting]
    with pytest.raises(Exception) as caught:
        audit.diagnostic_settings("nope")
    assert caught.value.code == "internal"


def test_a_shared_workspace_is_queried_once(go):
    setting = json.loads(stub_json("broken", "diagnostic-settings.json"))[0]
    twin = json.dumps([setting, {**setting, "name": "second"}])
    replies = {
        SHOW_DEV: (
            0,
            stub_json("broken", "keyvault-show.json"),
            "",
        ),
        ("monitor", "diagnostic-settings", "list", "--resource", DEV_ID, "--subscription", SUB): (
            0,
            twin,
            "",
        ),
        ("monitor", "log-analytics", "workspace", "show", "--ids", setting["workspaceId"]): (
            0,
            stub_json("broken", "workspace.json"),
            "",
        ),
    }

    class Run(Replies):
        def __call__(self, argv, **kw):
            if argv[1] == "cloud":
                self.calls.append(["cloud", "show"])
                return subprocess.CompletedProcess(
                    argv, 0, stub_json("broken", "cloud-show.json"), ""
                )
            if argv[1] == "rest":
                self.calls.append(["rest"])
                return subprocess.CompletedProcess(
                    argv, 0, stub_json("broken", "audit-query.json"), ""
                )
            return super().__call__(argv, **kw)

    runner = Run(replies)
    run = go("audit-failures", "broken", runner=runner)
    assert run.envelope["status"] == "ok" and len(out(run, "audit")["diagnosticSettings"]) == 2
    assert [c[0] for c in run.calls].count("rest") == 1


def test_a_workspace_that_cannot_be_queried_fails_the_run_and_does_not_look_clean(go):
    ws_forbidden = (
        "ERROR: (AuthorizationFailed) The client 'x' does not have authorization to perform action "
        "'Microsoft.OperationalInsights/workspaces/read' over scope 's'.\n"
    )
    settings_json = stub_json("broken", "diagnostic-settings.json")
    workspace = json.loads(settings_json)[0]["workspaceId"]
    replies = {
        SHOW_DEV: (
            0,
            stub_json("broken", "keyvault-show.json"),
            "",
        ),
        ("monitor", "diagnostic-settings", "list", "--resource", DEV_ID, "--subscription", SUB): (
            0,
            settings_json,
            "",
        ),
        ("monitor", "log-analytics", "workspace", "show", "--ids", workspace): (
            1,
            "",
            ws_forbidden,
        ),
        ("cloud", "show"): (0, stub_json("broken", "cloud-show.json"), ""),
    }
    run = go("audit-failures", "broken", runner=Replies(replies))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "permission_denied"


def test_rows_with_unexpected_columns_are_an_error():
    with pytest.raises(Exception) as caught:
        audit.rows_of({"tables": [{"columns": [{"name": "x"}], "rows": []}]})
    assert caught.value.code == "internal"
    with pytest.raises(Exception):
        audit.rows_of({})


def test_merge_sums_the_same_operation_across_workspaces():
    a = [("SecretGet", 403, 5, "2026-11-04T01:00:00Z", "2026-11-04T02:00:00Z")]
    b = [
        ("SecretGet", 403, 3, "2026-11-04T00:30:00Z", "2026-11-04T03:00:00Z"),
        ("KeyGet", 404, 1, "t", "t"),
    ]
    merged = audit.merge([a, b])
    assert merged[0] == {
        "operation": "SecretGet",
        "httpStatusCode": 403,
        "count": 8,
        "firstSeen": "2026-11-04T00:30:00Z",
        "lastSeen": "2026-11-04T03:00:00Z",
    }
    assert len(merged) == 2


# --- performance ------------------------------------------------------------------------------------


def test_performance_healthy_crosses_nothing(go):
    p = out(go("performance", "healthy"), "performance")
    assert p["requests"]["crossed"] is False and p["latency"]["crossed"] is False
    assert p["requests"]["metric"] == "ServiceApiHit"
    assert p["latency"]["metric"] == "ServiceApiLatency"
    assert p["requests"]["grains"] == 24 and p["requests"]["threshold"] == 1000


def test_performance_broken_counts_the_intervals_beyond_the_thresholds(go):
    p = out(go("performance", "broken"), "performance")
    assert (p["requests"]["crossings"], p["requests"]["max"], p["requests"]["crossed"]) == (
        2,
        3100.0,
        True,
    )
    assert (p["latency"]["crossings"], p["latency"]["max"]) == (2, 812.25)
    assert p["latency"]["points"][14] == {
        "timestamp": "2026-11-03T14:00:00+00:00",
        "value": 640.5,
        "maximum": 1900.0,
    }
    assert (
        p["requests"]["last"] < 1000
    )  # a spike earlier in the window is not hidden by the last value


def test_performance_thresholds_are_settings_and_each_metric_has_its_aggregations(go):
    run = go(
        "performance", "broken", settings={"requestThreshold": 3000, "latencyThresholdMs": 700}
    )
    p = out(run, "performance")
    assert (p["requests"]["crossings"], p["latency"]["crossings"]) == (1, 1)
    hits, latency = run.calls[1], run.calls[2]
    assert hits[hits.index("--metrics") + 1 : hits.index("--interval")] == [
        "ServiceApiHit",
        "--aggregation",
        "Count",
    ]
    assert latency[latency.index("--aggregation") + 1 : latency.index("--interval")] == [
        "Average",
        "Maximum",
    ]


def test_one_metric_that_cannot_be_read_leaves_the_other_and_a_partial_run(go):
    show = stub_json("broken", "keyvault-show.json")
    window = "--start-time 2026-11-03T12:00:00Z --end-time 2026-11-04T12:00:00Z --subscription"
    hits = f"monitor metrics list --resource {DEV_ID} --metrics ServiceApiHit --aggregation Count --interval PT1H {window} {SUB}"
    latency = f"monitor metrics list --resource {DEV_ID} --metrics ServiceApiLatency --aggregation Average Maximum --interval PT1H {window} {SUB}"
    throttled = "ERROR: (TooManyRequests) The request is being throttled.\n"
    runner = Replies(
        {
            SHOW_DEV: (0, show, ""),
            tuple(hits.split()): (1, "", throttled),
            tuple(latency.split()): (0, stub_json("broken", "metrics-latency.json"), ""),
        }
    )
    run = go("performance", "broken", runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("rate_limited", "requests")]
    p = out(run, "performance")
    assert p["requests"] is None and p["latency"]["crossed"] is True


def test_when_both_metrics_fail_the_run_fails(go):
    run = go("performance", "throttled")
    assert run.envelope["status"] == "failed"


def test_summarize_with_no_threshold_has_no_crossing_fields():
    s = metrics.summarize([{"timeStamp": "t", "count": 3.0}], "count")
    assert "threshold" not in s and s["max"] == 3.0 and s["pointsOmitted"] == 0


# --- the declared tasks ---------------------------------------------------------------------------------


def az_actions(call):
    """The RBAC actions one az command needs (Azure's operation names)."""
    head = call[:2]
    if head == ["cloud", "show"]:
        return set()  # the active cloud's endpoints: a local read
    if head == ["keyvault", "show"]:
        return {("action", "Microsoft.KeyVault/vaults/read")}
    if head == ["monitor", "metrics"]:
        return {("action", "Microsoft.Insights/metrics/read")}
    if head == ["monitor", "diagnostic-settings"]:
        return {("action", "Microsoft.Insights/diagnosticSettings/read")}
    if call[:4] == ["monitor", "log-analytics", "workspace", "show"]:
        return {("action", "Microsoft.OperationalInsights/workspaces/read")}
    if head == ["rest", "--method"]:
        url = call[call.index("--url") + 1]
        if "ResourceHealth/availabilityStatuses" in url:
            return {("action", "Microsoft.ResourceHealth/availabilityStatuses/read")}
        if url.startswith("https://api.loganalytics.io/") and url.endswith("/query"):
            return {("action", "Microsoft.OperationalInsights/workspaces/query/read")}
    data = {
        ("keyvault", "secret", "list"): "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
        ("keyvault", "certificate", "list"): "Microsoft.KeyVault/vaults/certificates/read",
        ("keyvault", "key", "list"): "Microsoft.KeyVault/vaults/keys/read",
    }
    if tuple(call[:3]) in data:
        return {("dataAction", data[tuple(call[:3])])}
    raise AssertionError(f"no permission mapped for az {call}")


@pytest.mark.parametrize("task", TASKS)
def test_each_task_declares_exactly_the_permissions_its_az_calls_need(go, task):
    run = go(task, "healthy" if task != "audit-failures" else "broken")
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


def test_the_windows_default_to_a_day_not_to_ten_minutes():
    for name in ("availability", "audit-failures", "performance"):
        inputs = next(t for t in doc()["tasks"] if t["name"] == name)["inputs"]
        assert inputs["window"]["default"] == "24h"


def test_the_skill_applies_to_key_vaults_and_the_type_exists():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["runwhen"]["appliesTo"]["types"] == ["azure/keyvault"]
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert "keyvault" in types


def test_read_settings_returns_the_values_as_declared():
    from types import SimpleNamespace

    declared = SimpleNamespace(
        max_items=200,
        command_timeout="60s",
        metric_interval="PT1H",
        availability_min_percent=100,
        request_threshold=1000,
        latency_threshold_ms=500,
    )
    st, th = read_settings(declared), read_thresholds(declared)
    assert (st.max_items, st.metric_interval, th.availability_min_percent) == (200, "PT1H", 100)
    assert (th.request_threshold, th.latency_threshold_ms) == (1000, 500)


# --- the target: subscription -> resource group -> vault, every call scoped with -g ---------------------


def test_every_arm_call_names_the_resource_group_and_the_subscription(go):
    for task in TASKS:
        run = go(task, "healthy" if task != "audit-failures" else "broken")
        shows = [c for c in run.calls if c[:2] == ["keyvault", "show"]]
        assert len(shows) == 1, task
        group = "rg-orders-prod" if task != "audit-failures" else "rg-orders-dev"
        assert shows[0][shows[0].index("-g") + 1] == group
        assert shows[0][-2:] == ["--subscription", SUB]
        for call in run.calls:
            if call[:2] in (["monitor", "metrics"], ["monitor", "diagnostic-settings"]):
                assert call[-2:] == ["--subscription", SUB], call


@pytest.mark.parametrize(
    "change",
    [
        {"resourcegroup": ""},
        {"resourcegroup": "-rg"},
        {"resourcegroup": "rg/../x"},
        {"resourcegroup": "a" * 91},
        {"name": "ab"},
        {"name": "-kv-orders"},
        {"name": "kv_orders"},
        {"subscription": "not-a-guid"},
    ],
)
def test_a_target_with_a_bad_scope_is_invalid_input_before_any_az_call(run_script, change):
    run = run_script("azure-kv-health", "configuration", resource={**vault(DEV), **change}, now=NOW)
    assert run.errors[0]["code"] == "invalid_input" and run.calls == []


def test_a_vault_whose_id_names_another_group_is_an_error_not_the_wrong_vault(go):
    other = json.loads(stub_json("broken", "keyvault-show.json"))
    other["id"] = other["id"].replace("rg-orders-dev", "rg-elsewhere")
    runner = Replies({SHOW_DEV: (0, json.dumps(other), "")})
    run = go("configuration", "broken", runner=runner)
    assert run.errors[0]["code"] == "internal" and "another scope" in run.errors[0]["message"]


def test_an_id_that_is_not_a_vault_resource_id_is_refused_before_it_reaches_a_query(go):
    other = json.loads(stub_json("broken", "keyvault-show.json"))
    other["id"] = DEV_ID + '"; drop'
    runner = Replies({SHOW_DEV: (0, json.dumps(other), "")})
    run = go("audit-failures", "broken", runner=runner)
    assert run.errors[0]["code"] == "internal" and len(runner.calls) == 1


def test_the_group_case_arm_reports_may_differ_from_the_identitys_lower_case(go):
    # the identity keeps the group lower case; ARM spells it as it was created
    shown = json.loads(stub_json("broken", "keyvault-show.json"))
    shown["id"] = shown["id"].replace("rg-orders-dev", "RG-Orders-Dev")
    runner = Replies({SHOW_DEV: (0, json.dumps(shown), "")})
    run = go("configuration", "broken", runner=runner)
    assert run.envelope["status"] == "ok"


def test_a_group_that_does_not_exist_is_not_found(go):
    gone = "ERROR: (ResourceGroupNotFound) Resource group 'rg-gone' could not be found.\n"
    runner = Replies({SHOW_DEV: (1, "", gone)})
    run = go("configuration", "broken", runner=runner)
    assert run.errors[0]["code"] == "not_found"


# --- expiring-items: the data-plane refusal names the action ---------------------------------------------


def test_the_permission_denied_message_names_the_missing_action(go):
    run = go("expiring-items", "data-plane-forbidden")
    (error,) = run.errors
    assert error["code"] == "permission_denied"
    assert "Action: 'Microsoft.KeyVault/vaults/secrets/readMetadata/action'" in error["message"]
    assert "Microsoft.KeyVault/vaults/secrets/readMetadata/action" in error["message"]


def test_the_data_plane_fixture_records_the_text_that_names_the_action():
    fixture = yaml.safe_load(
        (SKILL / "tests" / "expiring-items" / "data-plane-forbidden.yaml").read_text()
    )
    stderr = "\n".join(c["stderr"] for c in fixture["calls"] if c["rc"] != 0)
    assert "Action: 'Microsoft.KeyVault/vaults/secrets/readMetadata/action'" in stderr
    assert fixture["expect"]["errorCodes"] == ["permission_denied"]


def test_certificate_managed_secrets_are_skipped_and_counted(go):
    run = go("expiring-items", "broken")
    e = out(run, "expiry")
    assert e["managedSecretsSkipped"] == 1
    assert e["scanned"]["secrets"] == 5  # the sixth row is the certificate's own secret
    assert [i["name"] for i in e["items"]["items"]].count(
        "tls-orders-dev"
    ) == 1  # the certificate's


def test_items_with_an_unreadable_expiry_are_named(go):
    show = stub_json("broken", "keyvault-show.json")
    secrets = json.dumps(
        [
            {"name": "weird", "attributes": {"expires": "next tuesday", "enabled": True}},
            {"name": "plain", "attributes": {"expires": None}},
        ]
    )
    runner = Replies(
        {
            SHOW_DEV: (0, show, ""),
            ("keyvault", "secret", "list", "--vault-name", DEV): (0, secrets, ""),
            ("keyvault", "certificate", "list", "--vault-name", DEV): (0, "[]", ""),
            ("keyvault", "key", "list", "--vault-name", DEV): (0, "[]", ""),
        }
    )
    e = out(go("expiring-items", "broken", runner=runner), "expiry")
    assert e["unreadableExpiry"] == {
        "items": [{"kind": "secret", "name": "weird"}],
        "total": 1,
        "omitted": 0,
    }
    assert e["noExpiry"]["secrets"] == 2  # unreadable is also "no known expiry"
    assert e["items"]["items"] == []


# --- configuration: network bypass and private endpoint states ----------------------------------------------


def test_configuration_reports_the_firewall_bypass_and_the_private_endpoint_states(go):
    c = out(go("configuration", "healthy"), "configuration")
    assert c["networkAclBypass"] == "AzureServices"
    assert c["privateEndpointConnectionStates"] == [{"status": "Approved", "count": 1}]
    assert out(go("configuration", "broken"), "configuration")["networkAclBypass"] is None


def test_connection_states_count_each_status_in_either_shape():
    from azure_kv_health.configuration import connection_states

    states = connection_states(
        [
            {"privateLinkServiceConnectionState": {"status": "Approved"}},  # az keyvault show
            {"properties": {"privateLinkServiceConnectionState": {"status": "Pending"}}},  # REST
            {"privateLinkServiceConnectionState": {"status": "Approved"}},
            {"privateLinkServiceConnectionState": {}},
            {},
        ]
    )
    assert states == [
        {"status": "Approved", "count": 2},
        {"status": "Pending", "count": 1},
        {"status": "Unknown", "count": 2},
    ]
    assert connection_states(None) == [] and connection_states([]) == []


# --- metrics: a newest interval with no value ----------------------------------------------------------------


def test_a_missing_newest_interval_makes_last_stale_and_says_so(go):
    data = [
        {"timeStamp": "2026-11-04T01:00:00+00:00", "average": 100.0},
        {"timeStamp": "2026-11-04T02:00:00+00:00"},
    ]
    a = out(go("availability", "broken", runner=with_metric(metric_reply(data))), "availability")
    assert a["last"] == 100.0 and a["lastGrainMissing"] is True
    assert out(go("availability", "healthy"), "availability")["lastGrainMissing"] is False


def test_performance_series_carry_last_grain_missing(go):
    p = out(go("performance", "broken"), "performance")
    assert p["requests"]["lastGrainMissing"] is False and p["latency"]["lastGrainMissing"] is False
    assert metrics.summarize([], "count")["lastGrainMissing"] is False
    assert metrics.summarize([{"timeStamp": "t"}], "count")["lastGrainMissing"] is True


# --- audit-failures: which diagnostic settings count, which table, the endpoint, the cuts ------------------


@pytest.mark.parametrize(
    "log,enabled",
    [
        ({"category": "AuditEvent", "enabled": True}, True),
        ({"category": "AuditEvent", "categoryGroup": None, "enabled": True}, True),
        ({"category": None, "categoryGroup": "audit", "enabled": True}, True),
        ({"category": None, "categoryGroup": "allLogs", "enabled": True}, True),
        ({"category": None, "categoryGroup": "AllLogs", "enabled": True}, True),
        ({"category": None, "categoryGroup": "audit", "enabled": False}, False),
        ({"category": None, "categoryGroup": "audit", "enabled": None}, False),
        ({"category": "AzurePolicyEvaluationDetails", "enabled": True}, False),
        ({"category": None, "categoryGroup": "allMetrics", "enabled": True}, False),
    ],
)
def test_audit_enabled_counts_the_audit_category_and_the_groups_that_hold_it(log, enabled):
    assert audit.audit_enabled({"logs": [log]}) is enabled


def test_a_setting_with_the_all_logs_group_is_queried(go):
    a = out(go("audit-failures", "category-group"), "audit")
    assert a["auditLoggingToWorkspace"] is True and a["workspacesQueried"] == 1
    assert a["diagnosticSettings"][0]["auditEvent"] is True
    assert a["table"] == "AzureDiagnostics" and a["failureCount"] == 50


def test_a_dedicated_destination_is_read_from_azkvauditlogs(go):
    run = go("audit-failures", "dedicated")
    a = out(run, "audit")
    assert run.envelope["status"] == "ok"
    assert a["table"] == "AZKVAuditLogs"
    assert a["diagnosticSettings"][0]["table"] == "AZKVAuditLogs"
    last = run.calls[-1]
    query = json.loads(last[last.index("--body") + 1])["query"]
    assert query.startswith("AZKVAuditLogs\n")
    assert f'_ResourceId =~ "{DEV_ID}"' in query and "HttpStatusCode" in query
    assert "AzureDiagnostics" not in query and "httpStatusCode_d" not in query
    assert a["failureCount"] == 50  # the same result columns, whatever the table


def test_the_table_follows_the_destination_type():
    assert audit.table_of({"logAnalyticsDestinationType": "Dedicated"}) == "AZKVAuditLogs"
    assert audit.table_of({"logAnalyticsDestinationType": "dedicated"}) == "AZKVAuditLogs"
    assert audit.table_of({"logAnalyticsDestinationType": "AzureDiagnostics"}) == "AzureDiagnostics"
    assert audit.table_of({"logAnalyticsDestinationType": None}) == "AzureDiagnostics"
    assert audit.table_of({}) == "AzureDiagnostics"


def test_both_tables_are_queried_when_the_settings_use_both(go):
    setting = json.loads(stub_json("broken", "diagnostic-settings.json"))[0]
    both = json.dumps(
        [setting, {**setting, "name": "dedicated", "logAnalyticsDestinationType": "Dedicated"}]
    )
    run = go("audit-failures", "broken", runner=audit_replies(both))
    a = out(run, "audit")
    assert a["table"] == "AZKVAuditLogs, AzureDiagnostics" and a["workspacesQueried"] == 2
    assert a["failureCount"] == 100  # the same rows from both


def audit_replies(settings_json, cloud=None, query=None):
    """A runner for audit-failures on the broken vault with the given settings, cloud and query."""

    class Run(Replies):
        def __call__(self, argv, **kw):
            if argv[1] == "rest":
                self.calls.append(["rest", *argv[2:-2]])
                return subprocess.CompletedProcess(
                    argv, 0, query or stub_json("broken", "audit-query.json"), ""
                )
            return super().__call__(argv, **kw)

    workspace = json.loads(settings_json)[0]["workspaceId"]
    return Run(
        {
            SHOW_DEV: (0, stub_json("broken", "keyvault-show.json"), ""),
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
                settings_json,
                "",
            ),
            ("monitor", "log-analytics", "workspace", "show", "--ids", workspace): (
                0,
                stub_json("broken", "workspace.json"),
                "",
            ),
            ("cloud", "show"): (0, cloud or stub_json("broken", "cloud-show.json"), ""),
        }
    )


def test_the_query_endpoint_is_the_active_clouds(go):
    cloud = json.loads(stub_json("broken", "cloud-show.json"))
    cloud["endpoints"]["logAnalyticsResourceId"] = "https://api.loganalytics.us/"
    run = go(
        "audit-failures",
        "broken",
        runner=audit_replies(stub_json("broken", "diagnostic-settings.json"), json.dumps(cloud)),
    )
    rest = run.calls[-1]
    assert rest[rest.index("--url") + 1].startswith("https://api.loganalytics.us/v1/workspaces/")
    assert rest[rest.index("--resource") + 1] == "https://api.loganalytics.us"


@pytest.mark.parametrize(
    "endpoints", [{}, {"logAnalyticsResourceId": None}, {"logAnalyticsResourceId": "http://x"}]
)
def test_a_cloud_without_a_log_analytics_endpoint_is_a_dependency_error(go, endpoints):
    cloud = json.dumps({"endpoints": endpoints})
    run = go(
        "audit-failures",
        "broken",
        runner=audit_replies(stub_json("broken", "diagnostic-settings.json"), cloud),
    )
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "dependency"


def test_a_query_that_hit_its_row_limit_says_it_was_cut(go):
    reply = json.loads(stub_json("broken", "audit-query.json"))
    rows = [
        [f"Op{i:02d}", 403, 100 - i, "2026-11-04T01:00:00Z", "2026-11-04T02:00:00Z"]
        for i in range(51)
    ]
    reply["tables"][0]["rows"] = rows
    run = go(
        "audit-failures",
        "broken",
        runner=audit_replies(
            stub_json("broken", "diagnostic-settings.json"), query=json.dumps(reply)
        ),
    )
    a = out(run, "audit")
    assert a["truncated"] is True and a["failures"]["total"] == 50  # the 51st row is dropped
    assert out(go("audit-failures", "broken"), "audit")["truncated"] is False
    assert out(go("audit-failures", "no-diagnostics"), "audit")["truncated"] is False


def test_the_resource_id_is_escaped_for_kql_and_must_be_a_vault_id():
    assert audit.kql_quote('a"b\\c') == 'a\\"b\\\\c'
    body = json.loads(audit.query_body(DEV_ID, audit.LEGACY, "s", "e"))
    assert f'ResourceId =~ "{DEV_ID}"' in body["query"]
    for bad in (DEV_ID + '" or 1==1 //', "/subscriptions/x", DEV_ID + "/extra", "", 'x"'):
        with pytest.raises(Exception) as caught:
            audit.query_body(bad, audit.LEGACY, "s", "e")
        assert caught.value.code == "internal"


def test_the_workspaces_that_do_not_fit_the_time_budget_are_not_started(go):
    setting = json.loads(stub_json("broken", "diagnostic-settings.json"))[0]
    twin = json.dumps(
        [setting, {**setting, "name": "dedicated", "logAnalyticsDestinationType": "Dedicated"}]
    )
    run = go("audit-failures", "broken", runner=audit_replies(twin))
    assert out(run, "audit")["workspacesQueried"] == 2  # baseline: both fit with the real clock
    ticks = iter([0, 0, 530, 530, 530, 530])  # the first pair starts, the second has no room
    from types import SimpleNamespace

    ctx_run = audit_replies(twin)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s"),
        resource=vault(DEV),
        run=ctx_run,
        errors=[],
        now=lambda: NOW,
    )
    ctx.error = lambda code, message, scope=None: ctx.errors.append(
        {"code": code, "message": message, "scope": scope}
    )
    result = audit.read(ctx, "24h", 200, clock=lambda: next(ticks))
    assert result["workspacesQueried"] == 1
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("timeout", "workspace:law-central")]


def test_the_audit_task_has_a_timeout_that_fits_its_calls_and_budget():
    task = next(t for t in doc()["tasks"] if t["name"] == "audit-failures")
    seconds = float(task["timeout"].rstrip("s"))
    # keyvault show, diagnostic-settings, cloud show, then workspace show + query for each of 3
    calls = 3 + 2 * audit.MAX_WORKSPACES
    assert calls == 9 and audit.BUDGET <= seconds - 30
    assert calls * 60 <= audit.BUDGET


def test_a_vault_with_no_audit_setting_makes_no_cloud_call(go):
    run = go("audit-failures", "no-diagnostics")
    assert ["cloud", "show"] not in run.calls
