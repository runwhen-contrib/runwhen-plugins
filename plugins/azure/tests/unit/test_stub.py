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
AZ = STUB / "bin" / "az"
SCENARIOS = sorted(p.name for p in (STUB / "scenarios").iterdir() if p.is_dir())
SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"


def az(*argv, scenario="healthy", extra=None, cwd=None):
    env = {"PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent)}
    if scenario is not None:
        env["AZ_STUB_SCENARIO"] = scenario
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
        "not-found",
        "throttled",
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
    done = az("keyvault", "show", "--name", "something-else", "--subscription", SUB)
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


def test_a_star_in_an_argv_entry_matches_any_text():
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
    assert az(*changed).returncode == 2  # only the time arguments are wildcards


def test_python_path_and_the_working_directory_cannot_shadow_the_standard_library(tmp_path):
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed json was imported')\n")
    done = az(
        "account", "show", "--subscription", SUB, extra={"PYTHONPATH": str(tmp_path)}, cwd=tmp_path
    )
    assert done.returncode == 0 and "shadowed" not in done.stderr


def test_stub_log_records_each_argv(tmp_path):
    log = tmp_path / "log"
    az(
        "account",
        "show",
        "--output",
        "json",
        "--subscription",
        SUB,
        extra={"AZ_STUB_LOG": str(log)},
    )
    az("nope", extra={"AZ_STUB_LOG": str(log)})
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
    assert "The Vault 'nope' not found within subscription." in texts["not-found"]
    assert "TooManyRequests" in texts["throttled"]
    assert texts["healthy"].count("WARNING:") == 1  # rc 0 with a warning


def test_the_error_scenarios_fail_the_calls_that_need_the_missing_permission():
    forbidden = {tuple(c["argv"][:2]): c["rc"] for c in calls("forbidden")}
    assert forbidden[("account", "show")] == 0  # local profile data
    assert forbidden[("keyvault", "list")] == 1 and forbidden[("monitor", "metrics")] == 1
    data = {tuple(c["argv"][:3]): c["rc"] for c in calls("data-plane-forbidden")}
    assert data[("keyvault", "secret", "list")] == data[("keyvault", "key", "list")] == 1
    assert data[("keyvault", "show", "--name")] == 0


def test_the_rw_prefixed_names_still_work_when_the_stub_is_run_directly():
    # the SDK runner strips RW_* from a task's environment, so recordings use AZ_STUB_SCENARIO
    env = {"PATH": "/usr/bin:/bin", "RW_STUB_SCENARIO": "healthy"}
    done = subprocess.run(
        [str(AZ), "account", "show", "--subscription", SUB], env=env, capture_output=True, text=True
    )
    assert done.returncode == 0


def test_az_stub_scenario_wins_over_rw_stub_scenario():
    done = az(
        "account",
        "show",
        "--subscription",
        SUB,
        scenario="healthy",
        extra={"RW_STUB_SCENARIO": "nope"},
    )
    assert done.returncode == 0
