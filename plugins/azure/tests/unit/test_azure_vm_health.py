"""azure-vm-health against the stub scenarios and hand-made az replies: facts, bounds, every failure
as a code, the fixed in-guest scripts, and the permissions each task declares."""

import ast
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from azure_vm_health import activity, guest, guest_scripts, instance_view, monitor, scale_set
from azure_vm_health.common import read_settings
from azure_vm_health.errors import RwError
from azure_vm_health.target import read_thresholds, target  # noqa: F401
from conftest import SETTINGS, SUB, Replies, compute, scale_set as scale_set_target

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "azure-vm-health"
STUB = PLUGIN / "tests" / "stub" / "scenarios"
NOW = datetime(2026, 11, 4, 4, 0, 0, tzinfo=UTC)
WEB, BATCH, LEGACY = "vm-orders-web01", "vm-orders-batch02", "vm-platform-legacy"
API, JOBS = "vmss-orders-api", "vmss-orders-jobs"
VM_SETTINGS = {"metricInterval": "PT15M"}

TASKS = {
    "instance-view": ("instance_view", "instanceView"),
    "resource-health": ("resource_health", "health"),
    "metrics": ("resource_metrics", "metrics"),
    "activity-errors": ("activity_errors", "activity"),
    "scale-set-config": ("scale_set_config", "scaleSet"),
    "scale-set-instances": ("scale_set_instances", "instances"),
    "autoscale": ("autoscale", "autoscale"),
    "run-os-check": ("run_os_check", "osCheck"),
}
VMSS_ONLY = {"scale-set-config", "scale-set-instances", "autoscale"}
VM_ONLY = {"instance-view", "run-os-check"}
READ_TASKS = [t for t in TASKS if t != "run-os-check"]
GROUPS = {
    WEB: "rg-orders-prod",
    BATCH: "rg-orders-dev",
    LEGACY: "rg-platform",
    API: "rg-orders-prod",
    JOBS: "rg-orders-dev",
    "nope": "rg-orders-dev",
}


def stub_json(scenario, name):
    return (STUB / scenario / name).read_text()


def default_target(task, scenario, vmss=False):
    healthy = scenario == "healthy"
    if task == "run-os-check" and scenario in ("guest-broken", "runcommand-forbidden"):
        return compute(WEB)  # the VMs whose agent is ready
    if scenario == "not-found":
        name = "nope"
    elif scenario == "stopped":
        name = LEGACY
    elif task in VMSS_ONLY or vmss:
        name = API if healthy else JOBS
    else:
        name = WEB if healthy else BATCH
    return scale_set_target(name) if (task in VMSS_ONLY or vmss) else compute(name)


@pytest.fixture
def go(run_script):
    """go(task, scenario, vmss=False, resource=None, **inputs): the task on the scenario's default target"""

    def run(task, scenario="healthy", *, vmss=False, resource=None, settings=None, **kw):
        script, _ = TASKS[task]
        return run_script(
            "azure-vm-health",
            script,
            scenario=f"vm-{scenario}",
            resource=resource or default_target(task, scenario, vmss),
            now=NOW,
            settings={**VM_SETTINGS, **(settings or {})},
            **kw,
        )

    return run


def out(run, task):
    return run.envelope["outputs"][TASKS[task][1]]


def doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def task_doc(task):
    return next(t for t in doc()["tasks"] if t["name"] == task)


def schema_of(task):
    ref = task_doc(task)["outputs"][TASKS[task][1]]["schema"]
    return json.loads((SKILL / ref).read_text())


# --- schemas ----------------------------------------------------------------------------------


def runs():
    for task in TASKS:
        for scenario in ("healthy", "broken"):
            if task == "run-os-check":
                continue  # one check per run: see test_every_os_check_validates
            yield task, scenario, False
    for task in ("resource-health", "metrics", "activity-errors"):
        for scenario in ("healthy", "broken"):
            yield task, scenario, True


@pytest.mark.parametrize(("task", "scenario", "vmss"), list(runs()))
def test_every_output_validates_against_its_schema(go, task, scenario, vmss):
    run = go(task, scenario, vmss=vmss)
    assert run.envelope["status"] == "ok", run.errors
    jsonschema.validate(out(run, task), schema_of(task))


@pytest.mark.parametrize("task", TASKS)
def test_each_output_schema_is_valid_2020_12_and_names_no_verdict(task):
    schema = schema_of(task)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert not {"severity", "title", "nextSteps", "status", "errors"} & set(schema["properties"])
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("check", guest_scripts.CHECKS)
@pytest.mark.parametrize("scenario", ["healthy", "guest-broken"])
def test_every_os_check_validates(go, check, scenario):
    run = go("run-os-check", scenario, check=check)
    assert run.envelope["status"] == "ok", run.errors
    jsonschema.validate(out(run, "run-os-check"), schema_of("run-os-check"))


# --- instance-view ---------------------------------------------------------------------------------


def test_instance_view_of_a_healthy_vm(go):
    run = go("instance-view")
    view = out(run, "instance-view")
    assert (view["vm"], view["powerState"], view["running"]) == (WEB, "running", True)
    assert (view["provisioningState"], view["instanceProvisioningState"]) == (
        "Succeeded",
        "succeeded",
    )
    assert (view["vmSize"], view["osType"], view["location"]) == (
        "Standard_D2s_v5",
        "Linux",
        "eastus",
    )
    assert view["agent"]["ready"] is True and view["agent"]["version"] == "2.13.1.1"
    assert view["extensions"]["total"] == 1 and view["extensions"]["failed"] == 0
    assert view["bootDiagnostics"] == {
        "enabled": True,
        "consoleScreenshotAvailable": True,
        "serialLogAvailable": True,
        "status": None,
    }
    assert run.calls == [
        ["vm", "get-instance-view", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB]
    ]


def test_instance_view_of_a_broken_vm_names_the_failed_extension_and_the_agent(go):
    view = out(go("instance-view", "broken"), "instance-view")
    assert view["provisioningState"] == "Failed"
    assert view["instanceProvisioningState"] == "failed/VMExtensionProvisioningError"
    assert view["running"] is True  # running and still failed: both are facts
    assert view["agent"]["ready"] is False
    assert view["agent"]["statuses"][0]["displayStatus"] == "Not Ready"
    assert view["extensions"]["failed"] == 1
    (failed,) = [e for e in view["extensions"]["items"] if e["failed"]]
    assert failed["name"] == "CustomScript" and failed["level"] == "Error"
    assert "No space left on device" in failed["message"]
    assert view["bootDiagnostics"]["enabled"] is False
    assert view["bootDiagnostics"]["consoleScreenshotAvailable"] is False


def test_a_deallocated_vm_has_no_agent_report_and_that_is_not_a_ready_agent(go):
    view = out(go("instance-view", "stopped"), "instance-view")
    assert (view["powerState"], view["running"]) == ("deallocated", False)
    assert view["agent"] == {"reported": False, "version": None, "ready": None, "statuses": []}
    assert view["extensions"] == {"total": 0, "failed": 0, "items": [], "omitted": 0}


def test_a_reply_without_an_instance_view_is_an_error_not_a_quiet_vm(go):
    reply = json.loads(stub_json("vm-healthy", "vm-show.json"))
    runner = Replies(
        {
            (
                "vm",
                "get-instance-view",
                "--name",
                WEB,
                "-g",
                "rg-orders-prod",
                "--subscription",
                SUB,
            ): (0, json.dumps(reply), "")
        }
    )
    run = go("instance-view", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_a_vm_answer_for_another_vm_is_an_error(go):
    show = stub_json("vm-healthy", "vm-instance-view.json")
    runner = Replies(
        {
            (
                "vm",
                "get-instance-view",
                "--name",
                BATCH,
                "-g",
                "rg-orders-dev",
                "--subscription",
                SUB,
            ): (0, show, "")
        }
    )
    run = go("instance-view", "broken", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_extensions_are_cut_at_the_nested_limit_and_counted():
    view = {
        "extensions": [
            {
                "name": f"e{i}",
                "statuses": [{"code": "ProvisioningState/succeeded", "level": "Info"}],
            }
            for i in range(80)
        ]
    }
    facts = instance_view.extension_facts(view, 200)
    assert (facts["total"], len(facts["items"]), facts["omitted"]) == (80, 50, 30)


def test_a_status_message_is_cut_at_1024_characters():
    s = instance_view.status({"code": "x", "message": "m" * 3000})
    assert len(s["message"]) == 1024 and s["truncated"] is True


# --- failures common to every task ---------------------------------------------------------------------


FIRST_CALL = {
    "instance-view": ("vm", "get-instance-view"),
    "resource-health": ("vm", "show"),
    "metrics": ("vm", "show"),
    "activity-errors": ("vm", "show"),
    "scale-set-config": ("vmss", "show"),
    "scale-set-instances": ("vmss", "show"),
    "autoscale": ("vmss", "show"),
    "run-os-check": ("vm", "get-instance-view"),
}


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize(
    ("scenario", "code"),
    [("forbidden", "permission_denied"), ("throttled", "rate_limited"), ("not-found", "not_found")],
)
def test_a_task_that_cannot_look_fails_with_the_code_and_no_outputs(go, task, scenario, code):
    kw = {"check": "disk"} if task == "run-os-check" else {}
    run = go(task, scenario, **kw)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.errors] == [code]
    assert run.envelope["outputs"] == {}
    assert tuple(run.calls[0][:2]) == FIRST_CALL[task]
    assert len(run.calls) == 1  # the first call decides; nothing is asked after it


def test_the_forbidden_message_names_the_action_to_grant(go):
    run = go("resource-health", "forbidden")
    assert "Microsoft.Compute/virtualMachines/read" in run.errors[0]["message"]
    run = go("scale-set-instances", "forbidden")
    assert "Microsoft.Compute/virtualMachineScaleSets/read" in run.errors[0]["message"]


@pytest.mark.parametrize("task", sorted(VM_ONLY))
def test_a_vm_only_task_refuses_a_scale_set_before_any_az_call(go, task):
    kw = {"check": "disk"} if task == "run-os-check" else {}
    run = go(task, resource=scale_set_target(API), **kw)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert run.calls == []


@pytest.mark.parametrize("task", sorted(VMSS_ONLY))
def test_a_scale_set_only_task_refuses_a_vm_before_any_az_call(go, task):
    run = go(task, resource=compute(WEB))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert run.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"subscription": "x"},
        {"resourcegroup": "-g"},
        {"resourcegroup": ""},
        {"name": "-n"},
        {"name": "a b"},
        {"type": "azure/keyvault"},
    ],
)
def test_a_target_with_a_bad_scope_is_invalid_input_before_any_az_call(go, change):
    run = go("instance-view", resource={**compute(WEB), **change})
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert run.calls == []


def test_every_arm_call_names_the_resource_group_and_the_subscription(go):
    for task in READ_TASKS:
        run = go(task, "healthy")
        for call in run.calls:
            if call[:2] == ["monitor", "autoscale"]:
                assert call[call.index("-g") + 1] == "rg-orders-prod"
            if call[0] in ("vm", "vmss"):
                assert call[call.index("-g") + 1] == "rg-orders-prod"
            if call[0] != "rest":
                assert call[call.index("--subscription") + 1] == SUB, call


def test_a_group_that_the_id_names_differently_is_an_error_not_the_wrong_resource(go):
    show = json.loads(stub_json("vm-healthy", "vm-show.json"))
    show["id"] = show["id"].replace("rg-orders-prod", "rg-other")
    runner = Replies(
        {
            ("vm", "show", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB): (
                0,
                json.dumps(show),
                "",
            )
        }
    )
    run = go("resource-health", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


def test_the_group_case_arm_reports_may_differ_from_the_identitys_lower_case(go):
    run = go("instance-view", "stopped")  # ARM spells it RG-Platform; the target says rg-platform
    assert run.envelope["status"] == "ok"
    assert run.calls[0][run.calls[0].index("-g") + 1] == "rg-platform"


# --- resource-health -----------------------------------------------------------------------------------


def test_resource_health_available(go):
    run = go("resource-health")
    health = out(run, "resource-health")
    assert (health["availabilityState"], health["healthy"]) == ("Available", True)
    assert health["resource"] == WEB and health["resourceType"] == "azure/virtual-machine"
    assert health["serviceImpactingEvents"] == 0 and health["reasonType"] is None
    assert [c[:2] for c in run.calls] == [["vm", "show"], ["rest", "--method"]]


def test_resource_health_unavailable_for_a_scale_set_target(go):
    run = go("resource-health", "broken", vmss=True)
    health = out(run, "resource-health")
    assert (health["availabilityState"], health["healthy"]) == ("Unavailable", False)
    assert health["resourceType"] == "azure/vm-scale-set" and health["reasonType"] == "Unplanned"
    assert health["serviceImpactingEvents"] == 1
    assert [c[:2] for c in run.calls] == [["vmss", "show"], ["rest", "--method"]]


def test_resource_health_asks_for_the_current_status_and_registers_nothing(go):
    run = go("resource-health")
    rid = f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.Compute/virtualMachines/{WEB}"
    assert run.calls[1][2:] == [
        "get",
        "--url",
        f"{rid}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
    ]
    assert not any("register" in " ".join(c) for c in run.calls)


def test_a_reply_without_a_state_is_an_error_not_a_healthy_vm(go):
    rid = f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.Compute/virtualMachines/{WEB}"
    runner = Replies(
        {
            ("vm", "show", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB): (
                0,
                stub_json("vm-healthy", "vm-show.json"),
                "",
            ),
            (
                "rest",
                "--method",
                "get",
                "--url",
                f"{rid}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
            ): (0, "{}", ""),
        }
    )
    run = go("resource-health", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


# --- metrics -------------------------------------------------------------------------------------------


def test_metrics_of_a_healthy_vm_cross_nothing(go):
    run = go("metrics")
    metrics = out(run, "metrics")
    assert (
        metrics["resource"] == WEB
        and metrics["interval"] == "PT15M"
        and metrics["windowSeconds"] == 6 * 3600
    )
    for field in ("cpu", "availableMemory", "diskRead", "diskWrite", "networkIn", "networkOut"):
        assert metrics[field]["hasData"] is True and metrics[field]["grains"] == 24
        assert metrics[field]["missing"] == 0 and metrics[field]["lastGrainMissing"] is False
    assert metrics["cpu"]["crossed"] is False and metrics["cpu"]["threshold"] == 80
    assert metrics["availableMemory"]["crossed"] is False
    assert metrics["availableMemory"]["threshold"] == 256 * 1024 * 1024
    assert "crossed" not in metrics["diskRead"]


def test_metrics_of_a_broken_vm_count_the_intervals_beyond_the_thresholds(go):
    metrics = out(go("metrics", "broken"), "metrics")
    assert metrics["cpu"]["crossed"] is True and metrics["cpu"]["crossings"] == 3
    assert metrics["cpu"]["aggregation"] == "maximum" and metrics["cpu"]["max"] == 96.0
    assert (
        metrics["availableMemory"]["crossed"] is True
        and metrics["availableMemory"]["crossings"] == 2
    )
    assert metrics["availableMemory"]["aggregation"] == "minimum"
    assert metrics["availableMemory"]["min"] < 256 * 1024 * 1024
    assert (
        metrics["cpu"]["points"][0]["average"] is not None
    )  # the average rides beside the maximum


def test_a_scale_set_target_reads_its_own_namespace(go):
    run = go("metrics", "broken", vmss=True)
    assert out(run, "metrics")["resourceType"] == "azure/vm-scale-set"
    rid = f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/Microsoft.Compute/virtualMachineScaleSets/{JOBS}"
    assert {c[c.index("--resource") + 1] for c in run.calls if c[0] == "monitor"} == {rid}


def test_metrics_asks_for_each_metric_over_the_window_that_ends_at_the_reference_time(go):
    run = go("metrics", window="2h")
    metric_calls = [c for c in run.calls if c[:3] == ["monitor", "metrics", "list"]]
    assert [
        (c[c.index("--metrics") + 1], c[c.index("--aggregation") + 1 : c.index("--interval")])
        for c in metric_calls
    ] == [
        ("Percentage CPU", ["Average", "Maximum"]),
        ("Available Memory Bytes", ["Average", "Minimum"]),
        ("Disk Read Bytes", ["Total"]),
        ("Disk Write Bytes", ["Total"]),
        ("Network In Total", ["Total"]),
        ("Network Out Total", ["Total"]),
    ]
    first = metric_calls[0]
    assert first[first.index("--start-time") + 1 : first.index("--subscription")] == [
        "2026-11-04T02:00:00Z",
        "--end-time",
        "2026-11-04T04:00:00Z",
    ]
    assert first[first.index("--interval") + 1] == "PT15M"


def test_a_metric_with_no_datapoint_is_no_data_and_neither_zero_nor_healthy(go):
    run = go("metrics", "no-data")
    assert run.envelope["status"] == "ok"
    memory = out(run, "metrics")["availableMemory"]
    assert memory["hasData"] is False and memory["missing"] == memory["grains"] == 24
    assert (memory["min"], memory["max"], memory["last"]) == (None, None, None)
    assert memory["points"] == [] and memory["crossed"] is False and memory["crossings"] == 0
    assert memory["lastGrainMissing"] is True
    assert out(run, "metrics")["diskRead"]["hasData"] is False
    assert out(run, "metrics")["cpu"]["hasData"] is True


def test_one_metric_that_cannot_be_read_leaves_the_others_and_a_partial_run(go):
    runner = metrics_runner(fail={"Disk Write Bytes": (1, "ERROR: (TooManyRequests) throttled\n")})
    run = go("metrics", runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("rate_limited", "diskWrite")]
    metrics = out(run, "metrics")
    assert metrics["diskWrite"] is None and metrics["cpu"]["hasData"] is True


def test_when_no_metric_can_be_read_the_run_fails_with_the_code(go):
    everything = {
        name: (1, "ERROR: (AuthorizationFailed) no\n")
        for name in (
            "Percentage CPU",
            "Available Memory Bytes",
            "Disk Read Bytes",
            "Disk Write Bytes",
            "Network In Total",
            "Network Out Total",
        )
    }
    run = go("metrics", runner=metrics_runner(fail=everything))
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "permission_denied"


def test_an_azure_monitor_error_code_in_the_reply_is_an_error(go):
    body = json.loads(stub_json("vm-healthy", "vm-metrics-cpu.json"))
    body["value"][0]["errorCode"] = "BadRequest"
    runner = metrics_runner(replace={"Percentage CPU": json.dumps(body)})
    run = go("metrics", runner=runner)
    assert run.envelope["status"] == "partial" and run.errors[0]["scope"] == "cpu"


def test_the_budget_stops_further_metric_calls_and_says_so():
    from types import SimpleNamespace

    ticks = iter([0, 1, 1000, 1000, 1000, 1000, 1000])
    runner = metrics_runner()
    errors = []
    ctx = SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s", max_items=200, metric_interval="PT15M"),
        resource=compute(WEB),
        run=runner,
        now=lambda: NOW,
        error=lambda code, message, scope=None: errors.append((code, scope)),
    )
    st = read_settings(ctx.settings)
    th = SimpleNamespace(
        cpu_threshold_percent=80,
        available_memory_floor_mb=256,
        disk_used_percent=85,
        memory_used_percent=90,
        uptime_days=30,
    )
    result = monitor.read(ctx, "1h", st, th, clock=lambda: next(ticks), budget=300)
    assert result["cpu"] is not None and result["availableMemory"] is None
    assert errors == [
        ("timeout", f)
        for f in ("availableMemory", "diskRead", "diskWrite", "networkIn", "networkOut")
    ]
    assert (
        len([c for c in runner.calls if c[0] == "monitor"]) == 1
    )  # nothing was asked after the budget


def metrics_runner(fail=None, replace=None):
    """VM web01's show and the six metric replies of the healthy scenario, with some failed or replaced."""
    answers = {
        ("vm", "show", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB): (
            0,
            stub_json("vm-healthy", "vm-show.json"),
            "",
        )
    }
    files = {
        "Percentage CPU": ("vm-metrics-cpu.json", ["Average", "Maximum"]),
        "Available Memory Bytes": ("vm-metrics-memory.json", ["Average", "Minimum"]),
        "Disk Read Bytes": ("vm-metrics-disk-read.json", ["Total"]),
        "Disk Write Bytes": ("vm-metrics-disk-write.json", ["Total"]),
        "Network In Total": ("vm-metrics-network-in.json", ["Total"]),
        "Network Out Total": ("vm-metrics-network-out.json", ["Total"]),
    }

    class Runner(Replies):
        def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
            assert check is False
            key = tuple(argv[1:-2])
            self.calls.append(list(key))
            if key[:3] == ("monitor", "metrics", "list"):
                metric = key[key.index("--metrics") + 1]
                if fail and metric in fail:
                    rc, err = fail[metric]
                    return subprocess.CompletedProcess(argv, rc, "", err)
                body = (replace or {}).get(metric) or stub_json("vm-healthy", files[metric][0])
                return subprocess.CompletedProcess(argv, 0, body, "")
            rc, o, e = self.answers[key]
            return subprocess.CompletedProcess(argv, rc, o, e)

    return Runner(answers)


@pytest.mark.parametrize("bad", ["", "0s", "31d", "soon", "10"])
def test_a_bad_window_is_invalid_input_before_any_az_call(go, bad):
    for task in ("metrics", "activity-errors"):
        run = go(task, window=bad)
        assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
        assert run.calls == []


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("cpuThresholdPercent", 101),
        ("cpuThresholdPercent", "x"),
        ("availableMemoryFloorMb", -1),
        ("metricInterval", "PT2H"),
        ("maxItems", 0),
        ("commandTimeout", "soon"),
    ],
)
def test_a_bad_setting_is_invalid_input_before_any_az_call(go, setting, value):
    run = go("metrics", settings={setting: value})
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert run.calls == []


def test_the_thresholds_are_settings(go):
    metrics = out(
        go("metrics", "broken", settings={"cpuThresholdPercent": 99, "availableMemoryFloorMb": 1}),
        "metrics",
    )
    assert metrics["cpu"]["crossed"] is False and metrics["availableMemory"]["crossed"] is False
    assert metrics["availableMemoryFloorBytes"] == 1024 * 1024


# --- activity-errors -----------------------------------------------------------------------------------


def test_the_activity_log_of_a_healthy_vm_has_no_error_entries(go):
    run = go("activity-errors")
    activity_out = out(run, "activity-errors")
    assert activity_out["counts"] == {"Critical": 0, "Error": 0, "Warning": 0}
    assert activity_out["eventsRead"] == 1 and activity_out["events"] == {
        "items": [],
        "total": 0,
        "omitted": 0,
    }
    assert activity_out["readCapReached"] is False


def test_the_activity_log_of_a_broken_vm_lists_the_worst_first_and_drops_informational(go):
    run = go("activity-errors", "broken")
    result = out(run, "activity-errors")
    assert result["eventsRead"] == 4 and result["counts"] == {
        "Critical": 1,
        "Error": 1,
        "Warning": 1,
    }
    assert [e["level"] for e in result["events"]["items"]] == ["Critical", "Error", "Warning"]
    error = result["events"]["items"][1]
    assert error["operation"] == "Microsoft.Compute/virtualMachines/extensions/write"
    assert (error["status"], error["subStatus"]) == ("Failed", "Conflict")
    assert "VMExtensionProvisioningError" in error["message"]


def test_the_identifiers_of_who_was_refused_are_redacted_from_a_message():
    text = "The client '9d2c5a10-77b4' with object id '9d2c5a10-77b4' does not have authorization"
    out = activity.redact(text)
    assert (
        "9d2c5a10" not in out and "client '<redacted>'" in out and "object id '<redacted>'" in out
    )
    event = {"level": "Error", "properties": {"statusMessage": text}}
    assert "9d2c5a10" not in activity.event_facts(event)["message"]
    assert activity.redact(None) is None


def test_the_activity_log_permission_is_a_subscription_scope_read():
    permission = next(
        p for p in task_doc("activity-errors")["permissions"] if "eventtypes" in p["action"]
    )
    assert permission["scope"] == "subscription"


def test_who_did_it_is_not_returned(go):
    text = json.dumps(out(go("activity-errors", "broken"), "activity-errors"))
    for leaked in ("caller", "claims", "9d2c5a10", "appid", "tenantId"):
        assert leaked not in text


def test_the_activity_log_is_read_for_the_resource_id_over_the_window(go):
    run = go("activity-errors", "broken", window="3h")
    call = run.calls[1]
    assert call[:3] == ["monitor", "activity-log", "list"]
    assert call[call.index("--resource-id") + 1].endswith(f"virtualMachines/{BATCH}")
    assert call[call.index("--start-time") + 1] == "2026-11-04T01:00:00Z"
    assert call[call.index("--end-time") + 1] == "2026-11-04T04:00:00Z"
    assert call[call.index("--max-events") + 1] == "1000"


def test_a_log_that_reaches_the_read_cap_says_the_counts_are_lower_bounds():
    rows = [
        {
            "level": "Error",
            "eventTimestamp": f"2026-11-04T0{i % 10}:00:00Z",
            "operationName": {"value": "op"},
        }
        for i in range(activity.READ_CAP)
    ]
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s"),
        resource=compute(WEB),
        now=lambda: NOW,
        run=Replies(
            {
                ("vm", "show", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB): (
                    0,
                    stub_json("vm-healthy", "vm-show.json"),
                    "",
                ),
                tuple(
                    [
                        "monitor",
                        "activity-log",
                        "list",
                        "--resource-id",
                        f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.Compute/virtualMachines/{WEB}",
                    ]
                    + [
                        "--start-time",
                        "2026-11-04T03:00:00Z",
                        "--end-time",
                        "2026-11-04T04:00:00Z",
                        "--max-events",
                        "1000",
                        "--subscription",
                        SUB,
                    ]
                ): (0, json.dumps(rows), ""),
            }
        ),
    )
    result = activity.read(ctx, "1h", 10)
    assert result["readCapReached"] is True and result["counts"]["Error"] == 1000
    assert len(result["events"]["items"]) == 10 and result["events"]["omitted"] == 990


def test_an_activity_reply_that_is_not_a_list_is_an_error(go):
    runner = Replies(
        {
            ("vm", "show", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB): (
                0,
                stub_json("vm-healthy", "vm-show.json"),
                "",
            ),
        }
    )
    base_call = runner.__call__

    def call(argv, **kw):
        if argv[1:3] == ["monitor", "activity-log"]:
            runner.calls.append(list(argv[1:-2]))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return base_call(argv, **kw)

    call.calls = runner.calls
    run = go("activity-errors", runner=call)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "internal"


# --- scale-set-config ----------------------------------------------------------------------------------


def test_scale_set_config_of_a_rolling_scale_set(go):
    config = out(go("scale-set-config"), "scale-set-config")
    assert config["scaleSet"] == API and config["orchestrationMode"] == "Uniform"
    assert config["sku"] == {"name": "Standard_D2s_v5", "tier": "Standard", "capacity": 3}
    assert config["zones"] == ["1", "2", "3"] and config["upgradeMode"] == "Rolling"
    assert (
        config["rollingUpgradeMaxBatchPercent"] == 20 and config["automaticRepairsEnabled"] is True
    )
    assert config["hasLoadBalancerHealthProbe"] is True and config["osType"] == "Linux"
    assert (
        config["image"]["publisher"] == "Canonical"
        and config["image"]["fromGalleryOrCustomImage"] is False
    )
    assert config["extensions"]["items"] == [
        {
            "name": "HealthExtension",
            "type": "ApplicationHealthLinux",
            "typeHandlerVersion": "1.0",
            "provisioningState": "Succeeded",
        }
    ]


def test_scale_set_config_of_a_manual_scale_set_without_a_probe(go):
    config = out(go("scale-set-config", "broken"), "scale-set-config")
    assert (
        config["upgradeMode"],
        config["automaticRepairsEnabled"],
        config["hasLoadBalancerHealthProbe"],
    ) == ("Manual", False, False)
    assert config["automaticOsUpgradeEnabled"] is None  # not set: unknown, not false


def test_scale_set_config_returns_no_os_profile_key_or_extension_setting(go):
    text = json.dumps(out(go("scale-set-config"), "scale-set-config"))
    for leaked in (
        "azureuser",
        "adminUsername",
        "linuxConfiguration",
        "secrets",
        "customData",
        "requestPath",
        "8080",
        "settings",
    ):
        assert leaked not in text


# --- scale-set-instances -------------------------------------------------------------------------------


def test_the_instances_of_a_healthy_scale_set(go):
    run = go("scale-set-instances")
    result = out(run, "scale-set-instances")
    assert result["counts"]["total"] == 3 and result["counts"]["provisioningFailed"] == 0
    assert result["counts"]["modelOutdated"] == 0 and result["counts"]["unhealthy"] == 0
    assert result["counts"]["byProvisioningState"] == {"Succeeded": 3} and result["counts"][
        "byPowerState"
    ] == {"running": 3}
    assert result["statusesSummary"] == [{"code": "ProvisioningState/succeeded", "count": 3}]
    assert [c[:2] for c in run.calls] == [
        ["vmss", "show"],
        ["vmss", "get-instance-view"],
        ["vmss", "list-instances"],
    ]
    assert run.calls[2][-2:] == ["--expand", "instanceView"]


def test_the_instances_of_a_broken_scale_set_put_the_problems_first(go):
    result = out(go("scale-set-instances", "broken"), "scale-set-instances")
    counts = result["counts"]
    assert (
        counts["total"],
        counts["provisioningFailed"],
        counts["modelOutdated"],
        counts["unhealthy"],
    ) == (5, 1, 2, 1)
    assert (
        counts["withFailedExtensions"] == 1
        and counts["healthUnknown"] == 0
        and counts["modelUnknown"] == 0
    )
    assert counts["byProvisioningState"] == {"Failed": 1, "Succeeded": 3, "Updating": 1}
    items = result["instances"]["items"]
    assert [i["instanceId"] for i in items] == ["1", "2", "3", "4", "0"]  # 0 is the one thing clean
    failed = next(i for i in items if i["provisioningState"] == "Failed")
    assert failed["instanceId"] == "3" and failed["failedExtensions"] == 1
    unhealthy = next(i for i in items if i["health"] == "unhealthy")
    assert unhealthy["instanceId"] == "2" and "connection refused" in unhealthy["healthMessage"]
    assert [i["latestModelApplied"] for i in items if i["instanceId"] in ("1", "4")] == [
        False,
        False,
    ]
    assert result["statusesSummary"][1] == {
        "code": "ProvisioningState/failed/VMExtensionProvisioningError",
        "count": 1,
    }
    assert result["extensionsSummary"][0]["name"] == "CustomScript"


def test_max_items_cuts_the_instances_but_not_the_counts(go):
    result = out(
        go("scale-set-instances", "broken", settings={"maxItems": 2}), "scale-set-instances"
    )
    assert (
        len(result["instances"]["items"]),
        result["instances"]["total"],
        result["instances"]["omitted"],
    ) == (2, 5, 3)
    assert result["counts"]["total"] == 5


def test_instances_that_cannot_be_listed_leave_the_summary_and_a_partial_run(go):
    run = go("scale-set-instances", "instances-forbidden")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "instances")]
    result = out(run, "scale-set-instances")
    assert result["instances"] is None and result["counts"] is None  # never an empty list
    assert result["statusesSummary"] is not None


def test_a_summary_that_cannot_be_read_leaves_the_instances_and_a_partial_run(go):
    run = go("scale-set-instances", "instance-view-forbidden")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "instance-view")]
    result = out(run, "scale-set-instances")
    assert result["statusesSummary"] is None and result["counts"]["total"] == 5


def test_a_flexible_scale_set_is_an_explicit_gap_and_not_an_empty_list(go):
    show = json.loads(stub_json("vm-healthy", "vmss-show.json"))
    show["orchestrationMode"] = "Flexible"
    base = ("--name", API, "-g", "rg-orders-prod", "--subscription", SUB)
    runner = Replies(
        {
            ("vmss", "show", *base): (0, json.dumps(show), ""),
            ("vmss", "get-instance-view", *base): (
                0,
                stub_json("vm-healthy", "vmss-instance-view.json"),
                "",
            ),
        }
    )
    run = go("scale-set-instances", runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("invalid_input", "instances")]
    assert out(run, "scale-set-instances")["instances"] is None
    assert [c[:2] for c in runner.calls] == [["vmss", "show"], ["vmss", "get-instance-view"]]


def test_when_both_reads_fail_the_run_fails(go):
    base = ("--name", API, "-g", "rg-orders-prod", "--subscription", SUB)
    denied = (1, "", "ERROR: (AuthorizationFailed) denied\n")
    runner = Replies(
        {
            ("vmss", "show", *base): (0, stub_json("vm-healthy", "vmss-show.json"), ""),
            ("vmss", "get-instance-view", *base): denied,
            ("vmss", "list-instances", *base, "--expand", "instanceView"): denied,
        }
    )
    run = go("scale-set-instances", runner=runner)
    assert run.envelope["status"] == "failed" and run.errors[-1]["code"] == "permission_denied"


def test_the_budget_stops_the_instance_listing_and_says_so():
    from types import SimpleNamespace

    base = ("--name", API, "-g", "rg-orders-prod", "--subscription", SUB)
    runner = Replies(
        {
            ("vmss", "show", *base): (0, stub_json("vm-healthy", "vmss-show.json"), ""),
            ("vmss", "get-instance-view", *base): (
                0,
                stub_json("vm-healthy", "vmss-instance-view.json"),
                "",
            ),
        }
    )
    errors = []
    ctx = SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s"),
        resource=scale_set_target(API),
        run=runner,
        error=lambda code, message, scope=None: errors.append((code, scope)),
    )
    ticks = iter([0, 1000])
    result = scale_set.read_instances(ctx, 200, clock=lambda: next(ticks), budget=300)
    assert errors == [("timeout", "instances")] and result["instances"] is None
    assert not any(c[1] == "list-instances" for c in runner.calls)


# --- autoscale ------------------------------------------------------------------------------------------


def test_autoscale_of_a_scale_set_with_room_to_grow(go):
    run = go("autoscale")
    result = out(run, "autoscale")
    assert (result["found"], result["anyEnabled"], result["currentCapacity"]) == (True, True, 3)
    (setting,) = result["settings"]["items"]
    (profile,) = setting["profiles"]
    assert (profile["minimum"], profile["maximum"], profile["default"]) == (2, 6, 3)
    assert (profile["capacityAtMaximum"], profile["capacityAtMinimum"]) == (False, False)
    out_rule, in_rule = profile["rules"]
    assert (
        out_rule["metric"],
        out_rule["operator"],
        out_rule["threshold"],
        out_rule["direction"],
    ) == ("Percentage CPU", "GreaterThan", 70.0, "Increase")
    assert (in_rule["direction"], in_rule["cooldown"]) == ("Decrease", "PT10M")
    assert [c[:3] for c in run.calls] == [
        ["vmss", "show", "--name"],
        ["monitor", "autoscale", "list"],
    ]


def test_autoscale_at_its_maximum_and_only_the_settings_that_target_the_scale_set(go):
    result = out(go("autoscale", "broken"), "autoscale")
    assert result["settingsInGroup"] == 2  # as-orders-batch targets another scale set
    assert [s["name"] for s in result["settings"]["items"]] == ["as-orders-jobs"]
    (profile,) = result["settings"]["items"][0]["profiles"]
    assert profile["capacityAtMaximum"] is True and profile["capacityAtMinimum"] is False


def test_autoscale_found_false_is_a_fact_about_the_group_not_a_verdict(go):
    base = ("--name", API, "-g", "rg-orders-prod", "--subscription", SUB)
    runner = Replies(
        {
            ("vmss", "show", *base): (0, stub_json("vm-healthy", "vmss-show.json"), ""),
            ("monitor", "autoscale", "list", "-g", "rg-orders-prod", "--subscription", SUB): (
                0,
                "[]",
                "",
            ),
        }
    )
    result = out(go("autoscale", runner=runner), "autoscale")
    assert result == {
        "scaleSet": API,
        "currentCapacity": 3,
        "settingsInGroup": 0,
        "found": False,
        "anyEnabled": False,
        "settings": {"items": [], "total": 0, "omitted": 0},
    }


def test_the_notification_recipients_of_an_autoscale_setting_are_not_returned(go):
    text = json.dumps(out(go("autoscale", "broken"), "autoscale"))
    assert (
        "notifications" not in text and "customEmails" not in text and "email" not in text.lower()
    )


# --- run-os-check: the fixed scripts -------------------------------------------------------------------


def test_disk_check_reports_filesystems_against_the_threshold(go):
    result = out(go("run-os-check", "guest-broken", check="disk"), "run-os-check")
    assert result["executed"] is True and result["dryRun"] is False
    assert result["invocation"] == {
        "code": "ProvisioningState/succeeded",
        "displayStatus": "Provisioning succeeded",
        "level": "Info",
    }
    disk = result["disk"]
    assert disk["overThreshold"] is True and disk["threshold"] == 85
    assert [
        (f["mount"], f["usedPercent"], f["overThreshold"]) for f in disk["filesystems"]["items"]
    ] == [
        ("/data", 97.7, True),
        ("/", 93.1, True),
        ("/boot", 7.6, False),
        ("/boot/efi", 5.8, False),
    ]
    assert result["memory"] is None and result["uptime"] is None and result["patches"] is None


def test_disk_check_of_a_healthy_vm_is_under_the_threshold(go):
    disk = out(go("run-os-check", check="disk"), "run-os-check")["disk"]
    assert disk["overThreshold"] is False and disk["filesystems"]["total"] == 3
    assert (
        disk["filesystems"]["items"][0]["mount"] == "/"
        and disk["filesystems"]["items"][0]["usedPercent"] == 30.3
    )


def test_memory_check_counts_available_memory_not_cache(go):
    memory = out(go("run-os-check", "guest-broken", check="memory"), "run-os-check")["memory"]
    assert memory["usedPercent"] == 94.0 and memory["overThreshold"] is True
    assert memory["totalKb"] == 16376204 and memory["availableKb"] == 980820
    healthy = out(go("run-os-check", check="memory"), "run-os-check")["memory"]
    assert healthy["usedPercent"] == 24.0 and healthy["overThreshold"] is False


def test_uptime_check_reports_days_and_load(go):
    uptime = out(go("run-os-check", "guest-broken", check="uptime"), "run-os-check")["uptime"]
    assert (
        uptime["days"] == 41.15 and uptime["overThreshold"] is True and uptime["seconds"] == 3555000
    )
    assert uptime["load"] == {"one": 5.96, "five": 6.26, "fifteen": 6.4}
    healthy = out(go("run-os-check", check="uptime"), "run-os-check")["uptime"]
    assert healthy["days"] == 15.0 and healthy["overThreshold"] is False


def test_patches_check_reports_pending_updates_and_the_index_age(go):
    patches = out(go("run-os-check", "guest-broken", check="patches"), "run-os-check")["patches"]
    assert (
        patches["packageManager"] == "apt" and patches["pending"] == 3 and patches["security"] == 2
    )
    assert patches["packages"] == ["libssl3t64", "openssl", "linux-image-azure"]
    assert patches["indexAgeSeconds"] == 3456000 and patches["rebootRequired"] is True
    healthy = out(go("run-os-check", check="patches"), "run-os-check")["patches"]
    assert (healthy["pending"], healthy["security"], healthy["rebootRequired"]) == (0, 0, False)


def test_the_guest_thresholds_are_settings(go):
    result = out(
        go("run-os-check", "guest-broken", check="disk", settings={"diskUsedPercent": 99}),
        "run-os-check",
    )
    assert result["disk"]["overThreshold"] is False and result["disk"]["threshold"] == 99


def test_the_script_sent_is_the_constant_of_the_skill_and_its_hash_is_returned(go):
    run = go("run-os-check", check="memory")
    call = next(c for c in run.calls if c[:3] == ["vm", "run-command", "invoke"])
    assert call[call.index("--scripts") + 1] == guest_scripts.SCRIPTS["memory"]
    assert call[call.index("--command-id") + 1] == "RunShellScript"
    result = out(run, "run-os-check")
    assert (
        result["scriptSha256"]
        == hashlib.sha256(guest_scripts.SCRIPTS["memory"].encode()).hexdigest()
    )
    assert result["scriptLines"] == len(guest_scripts.SCRIPTS["memory"].splitlines())
    assert [c[:2] for c in run.calls] == [["vm", "get-instance-view"], ["vm", "run-command"]]


@pytest.mark.parametrize("bad", ["", "df -h", "disk; rm -rf /", "all", None, "DISK"])
def test_a_check_is_one_of_the_four_names_and_nothing_else_reaches_the_vm(go, bad):
    run = go("run-os-check", check=bad)
    assert run.envelope["status"] == "failed" and run.errors[0]["code"] == "invalid_input"
    assert run.calls == []


def test_a_dry_run_reads_the_instance_view_and_sends_nothing(run_script):
    run = run_script(
        "azure-vm-health", "run_os_check", scenario="vm-healthy", resource=compute(WEB), now=NOW,
        settings=VM_SETTINGS, dry_run=True, check="patches",
    )  # fmt: skip
    result = run.envelope["outputs"]["osCheck"]
    assert result["executed"] is False and result["dryRun"] is True and result["invocation"] is None
    assert (
        result["check"] == "patches"
        and result["powerState"] == "running"
        and result["osType"] == "Linux"
    )
    assert result["scriptSha256"] == guest_scripts.sha256("patches")
    assert result["disk"] is result["memory"] is result["uptime"] is result["patches"] is None
    assert [c[:2] for c in run.calls] == [["vm", "get-instance-view"]]
    jsonschema.validate(result, schema_of("run-os-check"))


def test_a_stopped_vm_is_skipped_with_the_reason_and_nothing_is_run(go):
    run = go("run-os-check", "stopped", check="disk")
    assert run.envelope["status"] == "skipped" and run.errors == []
    assert "not running" in run.envelope["reason"] and "deallocated" in run.envelope["reason"]
    assert [c[:2] for c in run.calls] == [["vm", "get-instance-view"]]


def vm_view(**change):
    doc = json.loads(stub_json("vm-healthy", "vm-instance-view.json"))
    for key, value in change.items():
        parent = doc
        *path, last = key.split(".")
        for step in path:
            parent = parent[step]
        parent[last] = value
    return json.dumps(doc)


IV = ("vm", "get-instance-view", "--name", WEB, "-g", "rg-orders-prod", "--subscription", SUB)


def test_a_windows_vm_is_skipped_not_sent_a_shell_script(go):
    runner = Replies({IV: (0, vm_view(**{"storageProfile.osDisk.osType": "Windows"}), "")})
    run = go("run-os-check", runner=runner, check="disk")
    assert run.envelope["status"] == "skipped" and "Windows" in run.envelope["reason"]
    assert len(runner.calls) == 1


def test_an_agent_that_is_not_ready_is_skipped_and_an_unreported_agent_is_tried(go):
    doc = json.loads(vm_view())
    doc["instanceView"]["vmAgent"]["statuses"][0]["displayStatus"] = "Not Ready"
    run = go("run-os-check", runner=Replies({IV: (0, json.dumps(doc), "")}), check="disk")
    assert run.envelope["status"] == "skipped" and "agent is not ready" in run.envelope["reason"]


def test_a_run_command_the_identity_may_not_invoke_is_permission_denied_naming_the_action(go):
    run = go("run-os-check", "runcommand-forbidden", check="disk")
    assert run.envelope["status"] == "failed" and run.envelope["outputs"] == {}
    assert [e["code"] for e in run.errors] == ["permission_denied"]
    assert "Microsoft.Compute/virtualMachines/runCommand/action" in run.errors[0]["message"]
    assert [c[:2] for c in run.calls] == [["vm", "get-instance-view"], ["vm", "run-command"]]


def reply_with(stdout, stderr="", code="ProvisioningState/succeeded"):
    message = f"Enable succeeded: \n[stdout]\n{stdout}\n[stderr]\n{stderr}\n"
    body = {
        "value": [
            {
                "code": code,
                "level": "Info",
                "displayStatus": "Provisioning succeeded",
                "message": message,
                "time": None,
            }
        ]
    }
    return json.dumps(body)


def run_check(go, check, stdout, stderr="", code="ProvisioningState/succeeded"):
    script = guest_scripts.SCRIPTS[check]
    runner = Replies(
        {
            IV: (0, vm_view(), ""),
            (
                "vm",
                "run-command",
                "invoke",
                "--name",
                WEB,
                "-g",
                "rg-orders-prod",
                "--command-id",
                "RunShellScript",
                "--scripts",
                script,
                "--subscription",
                SUB,
            ): (0, reply_with(stdout, stderr, code), ""),
        }
    )
    return go("run-os-check", runner=runner, check=check)


def test_output_cut_short_is_an_error_never_a_clean_reading(go):
    run = run_check(
        go,
        "disk",
        "RW_BEGIN disk\nFilesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 100 10 90 10% /",
    )
    assert run.envelope["status"] == "failed" and "cut short" in run.errors[0]["message"]


def test_output_without_the_begin_marker_is_an_error(go):
    run = run_check(go, "disk", "something else\n")
    assert run.errors[0]["code"] == "internal" and "RW_BEGIN" in run.errors[0]["message"]


def test_a_command_that_failed_in_the_guest_is_an_error_with_its_exit_status(go):
    run = run_check(
        go, "disk", "RW_BEGIN disk\nRW_END disk rc=1", stderr="df: invalid option -- 'x'"
    )
    assert run.errors[0]["code"] == "internal" and "df exited 1" in run.errors[0]["message"]


def test_a_failed_run_command_is_an_error_carrying_the_guests_stderr(go):
    run = run_check(go, "disk", "", stderr="sh: 1: syntax error", code="ProvisioningState/failed/1")
    assert run.errors[0]["code"] == "internal" and "syntax error" in run.errors[0]["message"]


def test_stderr_is_returned_cut_not_discarded(go):
    healthy = "RW_BEGIN uptime\n100.5 90.0\n0.1 0.2 0.3 1/2 3\nRW_END uptime rc=0"
    run = run_check(go, "uptime", healthy, stderr="warning: " + "x" * 3000)
    result = out(run, "run-os-check")
    assert (
        result["stderr"].startswith("warning:")
        and len(result["stderr"]) == 1024
        and result["truncated"] is True
    )


def test_a_package_manager_that_fails_is_an_error_and_not_up_to_date(go):
    run = run_check(
        go,
        "patches",
        "RW_BEGIN patches\nmanager=apt\nerror=E: Could not open lock\nRW_END patches rc=100",
    )
    assert run.errors[0]["code"] == "internal" and "Could not open lock" in run.errors[0]["message"]


def test_an_unknown_package_manager_is_pending_null_and_not_up_to_date(go):
    run = run_check(go, "patches", "RW_BEGIN patches\nmanager=unknown\nRW_END patches rc=0")
    patches = out(run, "run-os-check")["patches"]
    assert (
        patches["packageManager"] == "unknown"
        and patches["pending"] is None
        and patches["rebootRequired"] is None
    )


def test_the_guest_check_has_its_own_timeout_and_the_reads_keep_theirs(run_script):
    fake = Replies({IV: (0, vm_view(), "")})
    seen = []
    script = guest_scripts.SCRIPTS["uptime"]
    reply = reply_with("RW_BEGIN uptime\n100.5 90.0\n0.1 0.2 0.3 1/2 3\nRW_END uptime rc=0")

    def runner(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        runner.calls.append(argv[1:-2])
        seen.append((argv[2], timeout))
        if argv[2] == "run-command":
            return subprocess.CompletedProcess(argv, 0, reply, "")
        return fake(argv, check=check)

    runner.calls = []
    assert script  # the constant exists
    run = run_script(
        "azure-vm-health",
        "run_os_check",
        scenario="vm-healthy",
        resource=compute(WEB),
        now=NOW,
        settings={**VM_SETTINGS, "osCheckTimeout": 90},
        runner=runner,
        check="uptime",
    )
    assert run.envelope["status"] == "ok", run.errors
    assert seen == [("get-instance-view", 60.0), ("run-command", 90.0)]


# --- the scripts that run in the guest ------------------------------------------------------------------


CHANGES_A_MACHINE = [
    "apt-get update", "apt-get install", "apt-get upgrade -y", "apt-get -y", "apt update", "yum update", "yum install", "yum makecache",
    "dnf update", "dnf install", "dnf makecache", "sed -i", "export PATH", "/etc/shadow", "/etc/passwd", "cat /etc", "$(cat",
]  # fmt: skip
# a command word that changes a machine or reads what a diagnostic has no business reading, wherever a command can start
COMMAND_WORDS = re.compile(
    r"(?:^|[;&|(`]|\bthen\b|\belse\b|\bdo\b|\bif\b)\s*"
    r"(?:rm|mv|cp|chmod|chown|kill|pkill|reboot|shutdown|poweroff|halt|systemctl|service|curl|wget|sudo|su|tee|dd|mkfs|mount|umount|"
    r"eval|base64|nc|ncat|printenv|env|set|ssh|scp|crontab|useradd|passwd|touch|mkdir|ln|tar|zip|python|python3|perl|bash|source|exec)\b"
)


@pytest.mark.parametrize("check", guest_scripts.CHECKS)
def test_a_guest_script_changes_nothing_and_prints_no_file_or_environment(check):
    text = guest_scripts.SCRIPTS[check]
    for word in CHANGES_A_MACHINE:
        assert word not in text, f"{check} contains {word!r}"
    for line in text.splitlines():
        if not line.lstrip().startswith("#"):
            assert not COMMAND_WORDS.search(line), f"{check}: {line}"
    assert text.startswith("#!/bin/sh\n")
    assert "2>/dev/null" not in text.replace("command -v apt-get >/dev/null 2>&1", "").replace(
        "command -v dnf >/dev/null 2>&1", ""
    ).replace("command -v yum >/dev/null 2>&1", "")
    # no redirect into a file: the only `>` are the stderr merge and command -v's null
    for line in text.splitlines():
        stripped = line.replace("2>&1", "").replace(">/dev/null", "")
        assert ">" not in stripped, f"{check}: {line}"
    assert text.count("RW_BEGIN") == 1 and text.count("RW_END") == 1


def test_the_denylist_catches_what_it_is_there_for():
    for bad in (
        "rm -rf /tmp/x",
        "x=1; reboot",
        "if true; then curl http://x; fi",
        "echo hi | tee /etc/x",
        "env",
        "cat f | base64",
    ):
        assert COMMAND_WORDS.search(bad), bad
    for fine in (
        'echo "reboot_required=yes"',
        "df -P -k",
        "grep -c '^Inst '",
        "[ -e /var/run/reboot-required ]",
    ):
        assert not COMMAND_WORDS.search(fine), fine


def test_every_guest_script_is_a_plain_string_literal_in_the_module():
    """Not an f-string, a format call, a concatenation or a computed value: what runs as root is the text in the file."""
    tree = ast.parse((SKILL / "lib" / "azure_vm_health" / "guest_scripts.py").read_text())
    literals = {
        n.targets[0].id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
    }
    for name in ("DISK", "MEMORY", "UPTIME", "PATCHES"):
        assert isinstance(literals[name], ast.Constant) and isinstance(literals[name].value, str), (
            name
        )
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree))
    assert guest_scripts.SCRIPTS == {
        "disk": guest_scripts.DISK,
        "memory": guest_scripts.MEMORY,
        "uptime": guest_scripts.UPTIME,
        "patches": guest_scripts.PATCHES,
    }
    for check in guest_scripts.CHECKS:
        assert (
            guest_scripts.sha256(check)
            == hashlib.sha256(guest_scripts.SCRIPTS[check].encode()).hexdigest()
        )


def test_the_patch_script_asks_the_package_manager_without_refreshing_it():
    text = guest_scripts.PATCHES
    assert "apt-get -s " in text and "dnf -C" in text.replace("$tool -C", "dnf -C")
    assert "check-update" in text and "-C -q check-update" in text
    assert "apt-get update" not in text and "makecache" not in text and "--refresh" not in text


def test_no_check_name_outside_the_four_has_a_script():
    assert (
        set(guest_scripts.SCRIPTS)
        == {"disk", "memory", "uptime", "patches"}
        == set(guest_scripts.CHECKS)
    )


def test_df_lines_with_spaces_in_the_mount_point_parse():
    lines = [
        "Filesystem 1024-blocks Used Available Capacity Mounted on",
        "/dev/sdb1 1000 400 600 40% /mnt/my data",
    ]
    disk = guest.disk(lines, 0, 85, 10)
    assert disk["filesystems"]["items"][0]["mount"] == "/mnt/my data"


def test_a_full_filesystem_with_no_blocks_is_unknown_not_over():
    lines = ["Filesystem 1024-blocks Used Available Capacity Mounted on", "none 0 0 0 - /x"]
    row = guest.disk(lines, 0, 85, 10)["filesystems"]["items"][0]
    assert row["usedPercent"] is None and row["overThreshold"] is None


def test_a_kernel_without_memavailable_gives_no_figure():
    memory = guest.memory(["MemTotal:  1000 kB", "MemFree:  100 kB"], 0, 90)
    assert (
        memory["availableKb"] is None
        and memory["usedPercent"] is None
        and memory["overThreshold"] is None
    )


@pytest.mark.parametrize("lines", [["1.0 2.0"], ["x y", "0.1 0.2 0.3"], ["1.0 2.0", "0.1 0.2"]])
def test_uptime_output_that_does_not_parse_is_an_error(lines):
    with pytest.raises(RwError):
        guest.uptime(lines, 0, 30)


def test_split_message_handles_a_reply_with_no_stderr_section():
    assert guest.split_message("Enable succeeded: \n[stdout]\nhello\n") == ("hello\n", "")
    assert guest.split_message("no markers") == ("", "no markers")


# --- permissions, declarations ---------------------------------------------------------------------------


def actions_of(argv):
    """The RBAC actions one az command needs."""
    c = list(argv)
    if c[:2] == ["vm", "show"]:
        return {"Microsoft.Compute/virtualMachines/read"}
    if c[:2] == ["vm", "get-instance-view"]:
        return {
            "Microsoft.Compute/virtualMachines/read",
            "Microsoft.Compute/virtualMachines/instanceView/read",
        }
    if c[:2] == ["vmss", "show"]:
        return {"Microsoft.Compute/virtualMachineScaleSets/read"}
    if c[:2] == ["vmss", "get-instance-view"]:
        return {"Microsoft.Compute/virtualMachineScaleSets/instanceView/read"}
    if c[:2] == ["vmss", "list-instances"]:
        return {
            "Microsoft.Compute/virtualMachineScaleSets/virtualMachines/read",
            "Microsoft.Compute/virtualMachineScaleSets/virtualMachines/instanceView/read",
        }
    if c[:3] == ["monitor", "metrics", "list"]:
        return {"Microsoft.Insights/metrics/read"}
    if c[:3] == ["monitor", "activity-log", "list"]:
        return {"Microsoft.Insights/eventtypes/management/values/read"}
    if c[:3] == ["monitor", "autoscale", "list"]:
        return {"Microsoft.Insights/autoscalesettings/read"}
    if c[0] == "rest" and "ResourceHealth/availabilityStatuses" in c[-1]:
        return {"Microsoft.ResourceHealth/availabilityStatuses/read"}
    if c[:3] == ["vm", "run-command", "invoke"]:
        return {"Microsoft.Compute/virtualMachines/runCommand/action"}
    raise AssertionError(f"no permission mapped for {c[:3]}")


CASES = [
    ("instance-view", "healthy", False), ("resource-health", "healthy", False), ("resource-health", "healthy", True),
    ("metrics", "healthy", False), ("metrics", "healthy", True), ("activity-errors", "healthy", False), ("activity-errors", "healthy", True),
    ("scale-set-config", "healthy", False), ("scale-set-instances", "healthy", False), ("autoscale", "healthy", False),
]  # fmt: skip


def declared(task):
    return {p["action"] for p in task_doc(task)["permissions"]}


@pytest.mark.parametrize("task", READ_TASKS)
def test_each_task_declares_exactly_the_permissions_its_az_calls_need(go, task):
    needed = set()
    for name, scenario, vmss in CASES:
        if name == task:
            for call in go(task, scenario, vmss=vmss).calls:
                needed |= actions_of(call)
    assert needed == declared(task)


def test_run_os_check_declares_exactly_the_permissions_of_its_calls(go):
    needed = set()
    for call in go("run-os-check", check="disk").calls:
        needed |= actions_of(call)
    assert needed == declared("run-os-check")
    assert "Microsoft.Compute/virtualMachines/runCommand/action" in declared("run-os-check")


def test_no_read_task_may_invoke_a_run_command_and_no_other_task_declares_the_action():
    for task in READ_TASKS:
        assert "Microsoft.Compute/virtualMachines/runCommand/action" not in declared(task)
        assert task_doc(task)["readOnly"] is True


def test_every_declared_permission_is_a_valid_azure_permission():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "azure-permission.json").read_text())
    for task in doc()["tasks"]:
        for permission in task["permissions"]:
            jsonschema.validate(permission, schema)


def test_the_write_task_declares_its_effects_and_a_dry_run_and_is_not_destructive():
    task = task_doc("run-os-check")
    assert task["readOnly"] is False and task["destructive"] is False and task["idempotent"] is True
    assert task["dryRun"] is True and len(task["effects"]) >= 3
    assert any("as root" in e and "read-only" in e for e in task["effects"])
    assert any("one run command at a time" in e for e in task["effects"])
    assert task["inputs"]["check"]["enum"] == list(guest_scripts.CHECKS)
    assert (
        "required" not in task["inputs"]["check"] and "default" not in task["inputs"]["check"]
    )  # required: no default


def test_the_skill_uses_the_azure_runtime_and_credential_and_no_other_task_is_a_write():
    d = doc()
    assert d["runtime"] == "azure@1" and d["defaults"]["credentials"] == "azure/cli"
    assert [t["name"] for t in d["tasks"] if not t["readOnly"]] == ["run-os-check"]
    assert [t["name"] for t in d["tasks"]] == list(TASKS)


def test_the_defaults_in_the_tests_match_the_declared_settings():
    for name, spec in doc()["settings"].items():
        if name != "metricInterval":  # the shared test settings keep the kv skill's PT1H
            assert SETTINGS[name] == spec["default"], name
    assert doc()["settings"]["metricInterval"]["default"] == VM_SETTINGS["metricInterval"]


def test_the_lookback_defaults_are_hours_not_minutes():
    for name in ("metrics", "activity-errors"):
        assert task_doc(name)["inputs"]["window"]["default"] in ("6h", "24h")


def test_task_timeouts_cover_their_calls():
    assert monitor.BUDGET + 60 <= int(task_doc("metrics")["timeout"].rstrip("s"))
    assert scale_set.LIST_BUDGET + 60 <= int(task_doc("scale-set-instances")["timeout"].rstrip("s"))
    maximum = doc()["settings"]["osCheckTimeout"]["max"]
    # the default invocation fits the task timeout beside the instance view; a larger one needs a larger timeout
    assert 60 + doc()["settings"]["osCheckTimeout"]["default"] <= int(
        task_doc("run-os-check")["timeout"].rstrip("s")
    )
    assert maximum == 900


def test_the_skill_applies_to_the_two_types_and_they_exist_under_the_resource_group():
    skill = yaml.safe_load((SKILL / "SKILL.md").read_text().split("---")[1])
    assert skill["name"] == "azure-vm-health" and skill["runwhen"]["appliesTo"]["types"] == [
        "azure/virtual-machine",
        "azure/vm-scale-set",
    ]
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    for name, plural in (
        ("virtual-machine", "virtual-machines"),
        ("vm-scale-set", "vm-scale-sets"),
    ):
        assert types[name]["parent"] == "resourcegroup" and types[name]["plural"] == plural


def test_skill_md_stays_within_the_brief():
    text = (SKILL / "SKILL.md").read_text()
    assert len(text.splitlines()) <= 150
    for heading in (
        "## When to use",
        "## What each task tells you",
        "## How to read the result",
        "## What to try next",
    ):
        assert heading in text
    assert len(skill_description(text)) <= 1024


def skill_description(text):
    return yaml.safe_load(text.split("---")[1])["description"]


def test_target_returns_lower_case_subscription_and_the_group_as_the_identity_has_it():
    kind, subscription, group, name = target({**compute(WEB), "subscription": SUB.upper()})
    assert (kind, subscription, group, name) == (
        "azure/virtual-machine",
        SUB,
        "rg-orders-prod",
        WEB,
    )


# --- the stub is rendered by the harness whose plan the code must follow ------------------------------------


def test_the_scenario_calls_are_the_calls_the_render_plan_makes():
    import sys

    sys.path.insert(0, str(PLUGIN / "tests" / "stub" / "capture"))
    import render_vm

    for scenario, items in render_vm.plans().items():
        recorded = json.loads(
            "".join(
                line
                for line in (STUB / f"vm-{scenario}" / "calls.yaml").read_text().splitlines(True)
                if not line.startswith("#")
            )
        )["calls"]
        want = [
            ["<ts>" if a in (render_vm.START, render_vm.END) else a for a in i["argv"]]
            for i in items
        ]
        assert [c["argv"] for c in recorded] == want, scenario
