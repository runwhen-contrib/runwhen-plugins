"""The az stub (tests/stub/bin/az): how it answers, and that its scenario files are well formed.

The stub is what fixtures are recorded against, so a malformed scenario or a stub that guesses
would put wrong calls into a fixture."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"
RECORD = STUB / "record.sh"
AZ = STUB / "bin" / "az"
SCENARIOS = sorted(p.name for p in (STUB / "scenarios").iterdir() if p.is_dir())
SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"


def az(*argv, scenario="healthy", extra=None, cwd=None):
    env = {"PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent)}
    if scenario is not None:
        env["AZURE_STUB_SCENARIO"] = scenario
    env.update(extra or {})
    return subprocess.run([str(AZ), *argv], env=env, capture_output=True, text=True, cwd=cwd)


def calls(scenario):
    text = "".join(
        line
        for line in (STUB / "scenarios" / scenario / "calls.yaml").read_text().splitlines(True)
        if not line.lstrip().startswith("#")
    )
    return json.loads(text)["calls"]


def test_the_scenarios_this_plugin_records_exist():
    assert set(SCENARIOS) == {
        "healthy",
        "broken",
        "no-diagnostics",
        "forbidden",
        "data-plane-forbidden",
        "secrets-forbidden",
        "not-found",
        "throttled",
        "category-group",
        "dedicated",
        "group-forbidden",
        "stopped",
        "monitor-forbidden",
        # azure-vm-health (rendered by capture/render_vm.py)
        "vm-healthy",
        "vm-broken",
        "vm-guest-broken",
        "vm-forbidden",
        "vm-throttled",
        "vm-not-found",
        "vm-stopped",
        "vm-no-data",
        "vm-instance-view-forbidden",
        "vm-instances-forbidden",
        "vm-runcommand-forbidden",
    }


def test_the_stub_runs_isolated_with_no_dependencies():
    assert AZ.read_text().splitlines()[0] == "#!/usr/bin/env -S python3 -I"
    assert os.access(AZ, os.X_OK)


def test_it_answers_with_the_canned_stdout_stderr_and_exit_code():
    done = az("account", "show", "--subscription", SUB)
    assert done.returncode == 0
    assert json.loads(done.stdout)["id"] == SUB
    assert done.stderr.startswith("WARNING:")  # a warning on a successful call


def test_output_json_is_not_part_of_the_lookup():
    plain = az("account", "show", "--subscription", SUB)
    for noise in (["--output", "json"], ["-o", "json"], ["--output=json"]):
        done = az("account", "show", *noise, "--subscription", SUB)
        assert (done.returncode, done.stdout) == (0, plain.stdout), noise
    # and another output format is a different command, which has no canned answer
    assert az("account", "show", "--output", "table", "--subscription", SUB).returncode == 2


def test_an_unknown_argv_exits_2_and_names_it():
    done = az("keyvault", "show", "--name", "something-else", "-g", "rg", "--subscription", SUB)
    assert done.returncode == 2 and done.stdout == ""
    assert "no canned response" in done.stderr
    assert "az keyvault show --name something-else" in done.stderr


def test_arguments_are_not_matched_by_position_alone_or_prefix():
    assert az("account", "show").returncode == 2
    assert az("account", "show", "--subscription", SUB, "--extra").returncode == 2


@pytest.mark.parametrize("scenario", [None, "", "no-such-scenario"])
def test_a_missing_or_unknown_scenario_exits_2(scenario):
    done = az("account", "show", scenario=scenario)
    assert done.returncode == 2 and "scenario" in done.stderr.lower()


def test_without_a_scenario_it_refuses_to_run_even_for_az_version():
    done = az("version", scenario=None)
    assert done.returncode == 2 and done.stdout == ""
    assert "AZURE_STUB_SCENARIO is not set" in done.stderr and "refusing" in done.stderr
    # an unset scenario never falls back to a default one
    assert az("account", "show", "--subscription", SUB, scenario=None).stdout == ""


def test_az_version_answers_with_the_stub_marker_the_recorder_checks():
    done = az("version")
    assert done.returncode == 0
    version = json.loads(done.stdout)
    assert version["rw-azure-stub"] is True and version["azure-cli"].endswith("-stub")
    assert json.loads(az("version", "--output", "json").stdout) == version
    assert STUB_MARKER in RECORD.read_text()  # record.sh looks for exactly this key


STUB_MARKER = "rw-azure-stub"


def test_ts_in_an_argv_entry_matches_a_timestamp_and_nothing_else():
    argv = [
        "monitor", "metrics", "list", "--resource",
        f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.KeyVault/vaults/kv-orders-prod",
        "--metrics", "Availability", "--aggregation", "Average", "--interval", "PT1H",
        "--start-time", "2031-01-01T00:00:00Z", "--end-time", "2031-01-02T00:00:00Z",
        "--subscription", SUB,
    ]  # fmt: skip
    done = az(*argv)
    assert done.returncode == 0
    assert json.loads(done.stdout)["value"][0]["name"]["value"] == "Availability"
    changed = [*argv]
    changed[argv.index("PT1H")] = "PT5M"
    assert az(*changed).returncode == 2  # only the time arguments are tokens
    for bad in ("tomorrow", "2031-01-01", "2031-01-01T00:00:00", "2031-01-01T00:00:00.5Z", "*", ""):
        wrong = [*argv]
        wrong[argv.index("--start-time") + 1] = bad
        assert az(*wrong).returncode == 2, bad


def test_a_star_is_text_not_a_wildcard():
    assert az("account", "show", "--subscription", "*").returncode == 2
    assert az("account", "show", "--subscription", SUB[:8] + "*").returncode == 2


def test_ts_inside_a_longer_entry_is_matched_in_place():
    # the audit query's body carries the window as "<ts>/<ts>" inside one JSON string
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("az_stub", str(AZ))
    spec = importlib.util.spec_from_loader("az_stub", loader)
    stub = importlib.util.module_from_spec(spec)
    loader.exec_module(stub)
    want = ['{"timespan":"<ts>/<ts>"}']
    assert stub.matches(want, ['{"timespan":"2031-01-01T00:00:00Z/2031-01-02T00:00:00Z"}'])
    assert not stub.matches(want, ['{"timespan":"x/y"}'])
    assert not stub.matches(want, ['{"timespan":"2031-01-01T00:00:00Z/2031-01-02T00:00:00Z"}x'])


def test_python_path_and_the_working_directory_cannot_shadow_the_standard_library(tmp_path):
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed json was imported')\n")
    done = az(
        "account", "show", "--subscription", SUB, extra={"PYTHONPATH": str(tmp_path)}, cwd=tmp_path
    )
    assert done.returncode == 0 and "shadowed" not in done.stderr


@pytest.mark.parametrize("name", ["AZURE_STUB_LOG", "AZ_STUB_LOG"])
def test_stub_log_records_each_argv(tmp_path, name):
    log = tmp_path / "log"
    az("account", "show", "--output", "json", "--subscription", SUB, extra={name: str(log)})
    az("nope", extra={name: str(log)})
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert lines == [["account", "show", "--output", "json", "--subscription", SUB], ["nope"]]


# ---- the scenario files ------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_call_is_well_formed_and_cites_its_documentation(scenario):
    directory = STUB / "scenarios" / scenario
    seen = set()
    for call in calls(scenario):
        assert set(call) <= {"argv", "stdout", "stderr", "rc", "source"}
        assert call["argv"] and all(isinstance(a, str) for a in call["argv"])
        assert call["argv"][0] != "az", "argv is what follows `az`"
        assert "-o" not in call["argv"] and "--output" not in call["argv"]
        assert call["source"].startswith("https://learn.microsoft.com/")
        assert "*" not in "".join(call["argv"]), "a timestamp is written <ts>, not *"
        assert isinstance(call["rc"], int)
        if "stdout" in call:
            assert call["rc"] == 0, "a failing call prints nothing on stdout"
            json.loads((directory / call["stdout"]).read_text())
            seen.add(call["stdout"])
        if call["rc"] != 0:
            assert call["stderr"].startswith("ERROR:"), call["argv"]
    assert {p.name for p in directory.glob("*.json")} == seen, "unreferenced or missing file"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_two_calls_of_a_scenario_have_the_same_argv(scenario):
    argvs = [json.dumps(c["argv"]) for c in calls(scenario)]
    assert len(argvs) == len(set(argvs))


def test_the_error_scenarios_print_the_wording_az_prints():
    texts = {s: "\n".join(c.get("stderr", "") for c in calls(s)) for s in SCENARIOS}
    assert "AuthorizationFailed" in texts["forbidden"] and "(Forbidden)" in texts["forbidden"]
    assert "(Forbidden) Caller is not authorized" in texts["data-plane-forbidden"]
    assert "AuthorizationFailed" not in texts["data-plane-forbidden"]
    assert (
        "(ResourceNotFound) The Resource 'Microsoft.KeyVault/vaults/nope' under resource group"
        in texts["not-found"]
    )
    assert "TooManyRequests" in texts["throttled"]
    assert texts["healthy"].count("WARNING:") == 1  # rc 0 with a warning


def test_the_error_scenarios_fail_the_calls_that_need_the_missing_permission():
    forbidden = {tuple(c["argv"][:2]): c["rc"] for c in calls("forbidden")}
    assert forbidden[("account", "show")] == 0  # local profile data
    assert forbidden[("cloud", "show")] == 0  # local too
    assert forbidden[("group", "list")] == 1 and forbidden[("monitor", "metrics")] == 1
    data = {tuple(c["argv"][:3]): c["rc"] for c in calls("data-plane-forbidden")}
    assert data[("keyvault", "secret", "list")] == data[("keyvault", "key", "list")] == 1
    assert data[("keyvault", "show", "--name")] == 0


def test_the_az_prefixed_name_is_an_alias_and_the_azure_prefixed_one_wins():
    env = {"PATH": "/usr/bin:/bin", "AZ_STUB_SCENARIO": "healthy"}
    done = subprocess.run(
        [str(AZ), "account", "show", "--subscription", SUB], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0
    both = az("account", "show", "--subscription", SUB, extra={"AZ_STUB_SCENARIO": "nope"})
    assert both.returncode == 0  # AZURE_STUB_SCENARIO=healthy


def test_the_rw_prefixed_name_is_not_read_the_runner_strips_it_anyway():
    env = {"PATH": "/usr/bin:/bin", "RW_STUB_SCENARIO": "healthy"}
    done = subprocess.run(
        [str(AZ), "account", "show", "--subscription", SUB], env=env, capture_output=True, text=True
    )
    assert done.returncode == 2 and "AZURE_STUB_SCENARIO is not set" in done.stderr


# ---- record.sh: the only recording path ----------------------------------------------------------

FAKE_RWTASK = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
path = os.environ["PATH"].split(os.pathsep)
entry = {
    "argv": argv,
    "first_path": path[0],
    "stub_on_path": any(p.endswith("tests/stub/bin") for p in path),
    "scenario": os.environ.get("AZURE_STUB_SCENARIO"),
    "alias": os.environ.get("AZ_STUB_SCENARIO"),
    "config_dir": os.environ.get("AZURE_CONFIG_DIR"),
    "client_id": os.environ.get("AZURE_CLIENT_ID"),
}
with open(os.environ["FAKE_RWTASK_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\\n")
if "--record" in argv:
    skill, _, task = argv[1].partition("/")
    plugin = argv[argv.index("--plugin-dir") + 1]
    scenario = argv[argv.index("--scenario") + 1]
    target = os.path.join(plugin, "skills", skill, "tests", task, scenario + ".yaml")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    resource = argv[argv.index("--resource") + 1]
    with open(target, "w") as handle:
        handle.write("# Recorded by `rwtask test --record`.\\nresource: %s\\ncalls: []\\nexpect:\\n  status: ok\\n  errorCodes: []\\n" % resource)
"""


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    """A copy of the plugin's stub and record.sh under tmp/plugins/azure, a fake rwtask, and run()."""
    import shutil

    plugin = tmp_path / "repo" / "plugins" / "azure"
    shutil.copytree(STUB, plugin / "tests" / "stub", ignore=shutil.ignore_patterns("__pycache__"))
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    rwtask = fake_bin / "rwtask"
    rwtask.write_text(FAKE_RWTASK)
    rwtask.chmod(0o755)
    log = tmp_path / "rwtask.log"

    def run(*args, env=None):
        full = {
            "PATH": f"/usr/bin:/bin:{Path(sys.executable).parent}",
            "RWTASK": str(rwtask),
            "FAKE_RWTASK_LOG": str(log),
            "HOME": str(tmp_path),
            **(env or {}),
        }
        done = subprocess.run(
            ["bash", str(plugin / "tests" / "stub" / "record.sh"), *args],
            env=full, capture_output=True, text=True, cwd=tmp_path,
        )  # fmt: skip
        entries = (
            [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        )
        return done, entries

    run.plugin = plugin
    return run


def test_record_sh_puts_the_stub_first_names_the_scenario_and_records_with_force(recorder):
    done, calls = recorder("azure-kv-health/availability/broken")
    assert done.returncode == 0, done.stderr
    record = calls[0]
    assert record["first_path"].endswith("tests/stub/bin") and record["scenario"] == "broken"
    assert record["alias"] is None and record["client_id"] is None
    assert "--record" in record["argv"] and "--force" in record["argv"]
    assert record["argv"][record["argv"].index("--resource") + 1] == (
        f"azure/subscriptions/{SUB}/resourcegroups/rg-orders-dev/keyvaults/kv-orders-dev"
    )
    assert record["argv"][record["argv"].index("--scenario") + 1] == "broken"
    assert record["config_dir"] and "broken" not in record["config_dir"]  # a scratch directory


def test_record_sh_targets_the_healthy_vault_and_the_missing_one(recorder):
    done, calls = recorder(
        "azure-kv-health/configuration/healthy", "azure-kv-health/configuration/not-found"
    )
    assert done.returncode == 0, done.stderr
    resources = [
        c["argv"][c["argv"].index("--resource") + 1] for c in calls if "--record" in c["argv"]
    ]
    assert resources[0].endswith("/resourcegroups/rg-orders-prod/keyvaults/kv-orders-prod")
    assert resources[1].endswith("/resourcegroups/rg-orders-dev/keyvaults/nope")


def test_record_sh_keeps_the_hand_written_expect_block(recorder):
    target = (
        recorder.plugin / "skills" / "azure-kv-health" / "tests" / "availability" / "broken.yaml"
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        "# old header\nresource: old\ncalls: []\nexpect:\n  status: ok\n  errorCodes: []\n  outputs:\n    availability:\n      belowThreshold: true\n"
    )
    done, _ = recorder("azure-kv-health/availability/broken")
    assert done.returncode == 0, done.stderr
    text = target.read_text()
    assert "resource: azure/subscriptions/" in text and "old header" not in text  # recorded afresh
    assert "belowThreshold: true" in text  # the hand-written outputs survive


def test_record_sh_says_so_when_a_fixture_is_new(recorder):
    done, _ = recorder("azure-kv-health/audit-failures/dedicated")
    assert done.returncode == 0
    assert "NEW" in done.stderr and "expect" in done.stderr


def test_record_sh_replays_with_the_stub_off_the_path(recorder):
    done, calls = recorder("azure-base/discover/healthy")
    assert done.returncode == 0, done.stderr
    replays = [c for c in calls if "--record" not in c["argv"]]
    assert [c["argv"][1] for c in replays] == [
        "azure-base", "azure-kv-health", "azure-aks-health", "azure-vm-health"
    ]  # fmt: skip
    assert all(not c["stub_on_path"] and c["scenario"] is None for c in replays)


def test_record_sh_refuses_when_az_is_not_the_stub(recorder):
    az = recorder.plugin / "tests" / "stub" / "bin" / "az"
    az.write_text('#!/bin/sh\necho \'{"azure-cli": "2.92.0"}\'\n')
    done, calls = recorder("azure-base/discover/healthy")
    assert done.returncode == 1 and calls == []
    assert "stub marker" in done.stderr


def test_record_sh_refuses_an_argument_that_matches_nothing(recorder):
    done, calls = recorder("azure-base/discover/no-such-fixture")
    assert done.returncode == 1 and calls == [] and "nothing matched" in done.stderr


def test_record_sh_is_the_only_documented_recording_path():
    readme = (STUB / "README.md").read_text()
    assert "record.sh" in readme
    for line in readme.splitlines():
        if "rwtask test" in line and "--record" in line:
            assert "never" in line, f"README documents a bare recording: {line}"
    assert "AZ_STUB_SCENARIO=" not in readme.replace("`AZ_STUB_SCENARIO` is an alias", "")


def test_record_sh_targets_the_vm_and_scale_set_fixtures_and_names_their_scenarios(recorder):
    done, calls = recorder(
        "azure-vm-health/instance-view/healthy",
        "azure-vm-health/instance-view/stopped",
        "azure-vm-health/metrics/vmss-broken",
        "azure-vm-health/autoscale/not-found",
        "azure-vm-health/scale-set-instances/instances-forbidden",
    )
    assert done.returncode == 0, done.stderr
    recorded = [c for c in calls if "--record" in c["argv"]]
    seen = [
        (
            c["argv"][c["argv"].index("--resource") + 1].split("resourcegroups/")[1],
            c["scenario"],
        )
        for c in recorded
    ]
    assert seen == [
        ("rg-orders-prod/virtual-machines/vm-orders-web01", "vm-healthy"),
        ("rg-platform/virtual-machines/vm-platform-legacy", "vm-stopped"),
        ("rg-orders-dev/vm-scale-sets/vmss-orders-jobs", "vm-broken"),
        ("rg-orders-dev/vm-scale-sets/vmss-orders-jobs", "vm-instances-forbidden"),
        ("rg-orders-dev/vm-scale-sets/nope", "vm-not-found"),
    ]  # in the order of the recorder's manifest


def test_record_sh_records_a_write_fixture_with_its_input_and_flags(recorder):
    done, calls = recorder(
        "azure-vm-health/run-os-check/dry-run",
        "azure-vm-health/run-os-check/disk-broken",
        "azure-vm-health/run-os-check/runcommand-forbidden",
    )
    assert done.returncode == 0, done.stderr
    recorded = [c for c in calls if "--record" in c["argv"]]
    broken, dry, forbidden = recorded  # the order of the recorder's manifest
    assert "--dry-run" in dry["argv"] and "--allow-write" not in dry["argv"]
    assert dry["argv"][dry["argv"].index("--input") + 1] == "check=disk"
    assert dry["scenario"] == "vm-healthy"
    assert "--allow-write" in broken["argv"] and "--dry-run" not in broken["argv"]
    # the findings come from a VM whose agent is ready, in the scenario that answers the guest scripts
    assert broken["scenario"] == "vm-guest-broken"
    assert broken["argv"][broken["argv"].index("--resource") + 1].endswith("/vm-orders-web01")
    assert forbidden["scenario"] == "vm-runcommand-forbidden"
    assert forbidden["argv"][forbidden["argv"].index("--resource") + 1].endswith("/vm-orders-web01")


def test_the_vm_scenarios_follow_the_naming_the_recorder_uses():
    names = {s for s in SCENARIOS if s.startswith("vm-")}
    for row in RECORD.read_text().splitlines():
        if row.strip().startswith('"azure-vm-health/'):
            for entry in row.strip().strip('"').partition(":")[2].split():
                fixture, _, rest = entry.partition("@")[0].partition("=")
                scenario = "vm-" + (rest or fixture).removeprefix("vmss-")
                assert scenario in names, f"{entry} -> {scenario}"
