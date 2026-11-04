"""The gcloud stub and its harness: nothing can reach a real Google account, a missing call is
loud, and every canned answer cites its reference."""

import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"
sys.path.insert(0, str(STUB))
import harness  # noqa: E402

SCENARIOS = sorted(p.name for p in (STUB / "scenarios").iterdir() if p.is_dir())
P = "shop-prod-1234"
REAL_LOOKING = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/Users/me/.config/gcloud/application_default_credentials.json",
    "GOOGLE_CLOUD_PROJECT": "a-real-project",
    "GCLOUD_PROJECT": "a-real-project",
    "CLOUDSDK_CORE_PROJECT": "a-real-project",
    "CLOUDSDK_CORE_ACCOUNT": "me@example.com",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/tmp/token",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "work",
    "CLOUDSDK_CONFIG": "/Users/me/.config/gcloud",
}


def stub_module():
    loader = importlib.machinery.SourceFileLoader("gcloud_stub", str(STUB / "bin" / "gcloud"))
    spec = importlib.util.spec_from_loader("gcloud_stub", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def config_dir():
    with tempfile.TemporaryDirectory(prefix=harness.CONFIG_PREFIX) as d:
        yield d


def run_stub(scenario, config_dir, *argv, base=None):
    env = harness.stub_env(
        scenario, config_dir, base or {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    )
    return subprocess.run(["gcloud", *argv], env=env, capture_output=True, text=True)


# ---- the harness keeps the real gcloud out ----------------------------------------------------


def test_the_harness_sets_cloudsdk_config_to_a_throwaway_temp_dir(config_dir):
    env = harness.stub_env("healthy", config_dir, dict(REAL_LOOKING, PATH="/usr/bin"))
    assert env["CLOUDSDK_CONFIG"] == config_dir
    cfg = Path(env["CLOUDSDK_CONFIG"])
    assert cfg.name.startswith("gcp-stub-config-") and not any(cfg.iterdir())
    assert Path(tempfile.gettempdir()).resolve() in cfg.resolve().parents
    assert env["CLOUDSDK_CONFIG"] != REAL_LOOKING["CLOUDSDK_CONFIG"]


def test_run_gives_the_command_a_fresh_empty_config_dir_and_removes_it(tmp_path):
    seen = tmp_path / "seen.json"
    code = (
        "import json, os, pathlib;"
        "c = os.environ['CLOUDSDK_CONFIG'];"
        f"pathlib.Path({str(seen)!r}).write_text(json.dumps({{'dir': c, 'ls': os.listdir(c),"
        " 'scenario': os.environ['GCP_STUB_SCENARIO']}))"
    )
    rc = harness.run("healthy", [sys.executable, "-c", code], base={"PATH": "/usr/bin:/bin"})
    seen_doc = json.loads(seen.read_text())
    assert rc == 0 and seen_doc["ls"] == [] and seen_doc["scenario"] == "healthy"
    assert Path(seen_doc["dir"]).name.startswith("gcp-stub-config-")
    assert not Path(seen_doc["dir"]).exists(), "the throwaway config dir is removed afterwards"


def test_the_callers_google_variables_never_reach_the_command(config_dir):
    env = harness.stub_env("healthy", config_dir, dict(REAL_LOOKING, PATH="/usr/bin", KEEP="1"))
    assert env["KEEP"] == "1"
    leaked = [
        k for k in env
        if k.startswith(("GOOGLE_", "GCLOUD_", "CLOUDSDK_")) and env[k] in REAL_LOOKING.values()
    ]  # fmt: skip
    assert leaked == []
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env and "CLOUDSDK_CORE_ACCOUNT" not in env
    assert env["GCE_METADATA_HOST"] == "127.0.0.1:1"  # no metadata server to fall back to


def test_the_stub_is_first_on_path_whatever_the_callers_path_holds(config_dir, tmp_path):
    real = tmp_path / "real-gcloud-dir"
    real.mkdir()
    (real / "gcloud").write_text("#!/bin/sh\necho REAL\n")
    (real / "gcloud").chmod(0o755)
    (real / "gsutil").write_text("#!/bin/sh\necho REAL\n")
    (real / "gsutil").chmod(0o755)
    env = harness.stub_env("healthy", config_dir, {"PATH": f"{real}{os.pathsep}/usr/bin"})
    assert env["PATH"].split(os.pathsep)[0] == str(harness.BIN)
    done = subprocess.run(
        ["gcloud", "projects", "describe", P], env=env, capture_output=True, text=True
    )
    assert "REAL" not in done.stdout and done.returncode == 0


def test_gsutil_is_blocked_so_the_real_one_never_runs(config_dir):
    done = run_stub("healthy", config_dir, "ignored")  # make sure env builds
    assert done.returncode in (0, 2)
    env = harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})
    done = subprocess.run(["gsutil", "ls"], env=env, capture_output=True, text=True)
    assert done.returncode == 2 and "gcloud storage" in done.stderr


def test_the_harness_refuses_an_unknown_scenario_and_a_dirty_or_foreign_config_dir(
    config_dir, tmp_path
):
    with pytest.raises(ValueError, match="no scenario"):
        harness.stub_env("nope", config_dir, {})
    with pytest.raises(ValueError, match="empty gcp-stub-config"):
        harness.stub_env("healthy", str(tmp_path), {})  # not named like a throwaway dir
    (Path(config_dir) / "credentials.db").write_text("x")
    with pytest.raises(ValueError, match="empty gcp-stub-config"):
        harness.stub_env("healthy", config_dir, {})


def test_the_stub_itself_refuses_to_run_without_the_harnesss_config_dir(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "GCP_STUB_SCENARIO": "healthy",
        "CLOUDSDK_CONFIG": str(tmp_path),
    }
    done = subprocess.run([str(STUB / "bin" / "gcloud"), "projects", "describe", P], env=env,
                          capture_output=True, text=True)  # fmt: skip
    assert done.returncode == 2 and "refusing to run" in done.stderr
    env.pop("CLOUDSDK_CONFIG")
    done = subprocess.run([str(STUB / "bin" / "gcloud"), "projects", "describe", P], env=env,
                          capture_output=True, text=True)  # fmt: skip
    assert done.returncode == 2 and "refusing to run" in done.stderr


def test_the_scripts_are_executable_and_the_stub_is_isolated_python3():
    for name in ("gcloud", "gsutil"):
        assert (STUB / "bin" / name).stat().st_mode & stat.S_IXUSR
    assert (STUB / "bin" / "gcloud").read_text().startswith("#!/usr/bin/env -S python3 -I\n")


# ---- matching ---------------------------------------------------------------------------------


def test_a_known_argv_prints_its_file_and_exits_with_its_rc(config_dir):
    done = run_stub("healthy", config_dir, "projects", "describe", P, "--format=json")
    assert done.returncode == 0 and json.loads(done.stdout)["projectId"] == P
    done = run_stub(
        "forbidden", config_dir, "storage", "buckets", "list", f"--project={P}", "--raw"
    )
    assert done.returncode == 1 and done.stdout == ""
    assert "HTTPError 403" in done.stderr and done.stderr.startswith(
        "ERROR: (gcloud.storage.buckets.list)"
    )


@pytest.mark.parametrize(
    "noise",
    [["--format=json"], ["--format", "json"], ["--quiet"], ["-q"], ["--format=json", "--quiet"]],
)
def test_output_and_prompt_flags_are_dropped_before_matching(config_dir, noise):
    done = run_stub("healthy", config_dir, "projects", "describe", P, *noise)
    assert done.returncode == 0, done.stderr


def test_an_unknown_argv_exits_2_and_names_the_argv(config_dir):
    done = run_stub("healthy", config_dir, "storage", "buckets", "delete", "gs://shop-assets")
    assert done.returncode == 2 and done.stdout == ""
    assert '"storage", "buckets", "delete", "gs://shop-assets"' in done.stderr
    assert "no canned answer" in done.stderr


def test_flag_order_matters_nothing_is_sorted(config_dir):
    done = run_stub("healthy", config_dir, "storage", "buckets", "list", "--raw", f"--project={P}")
    assert done.returncode == 2


def test_an_unknown_scenario_exits_2(config_dir):
    env = harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})
    env["GCP_STUB_SCENARIO"] = "missing"
    done = subprocess.run(["gcloud", "projects", "list"], env=env, capture_output=True, text=True)
    assert done.returncode == 2 and "no scenario" in done.stderr


def test_the_scenario_is_not_read_from_a_variable_the_runner_strips():
    # rwtask drops every RW_* variable from a task's environment, so the stub (a grandchild of the
    # task) would never see one
    assert "RW_" not in (STUB / "bin" / "gcloud").read_text()
    assert "RW_" not in (STUB / "harness.py").read_text().replace("RW_*", "")


# ---- the canned data --------------------------------------------------------------------------


def calls_of(scenario):
    return yaml.safe_load((STUB / "scenarios" / scenario / "calls.yaml").read_text())


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_minimal_parser_reads_calls_yaml_exactly_as_a_yaml_parser_does(scenario):
    text = (STUB / "scenarios" / scenario / "calls.yaml").read_text()
    assert stub_module().parse_calls(text) == yaml.safe_load(text)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_call_cites_a_reference_and_every_file_exists_and_is_used(scenario):
    directory = STUB / "scenarios" / scenario
    calls = calls_of(scenario)
    assert calls
    used = {"calls.yaml"}
    for call in calls:
        assert set(call) <= {"argv", "rc", "stdout", "stderr", "source"}
        assert re.fullmatch(r"https://(cloud|docs)\.google\.com/\S+", call["source"]), call
        assert call["argv"][0] in ("projects", "storage")
        assert not {"--format=json", "--quiet", "-q"} & set(call["argv"]), (
            "argv is stored normalised"
        )
        if call["rc"] == 0:
            assert call.get("stdout") and "stderr" not in call
        else:
            assert call["stderr"].startswith("ERROR: (gcloud.") and "stdout" not in call
        if call.get("stdout"):
            used.add(call["stdout"])
            text = (directory / call["stdout"]).read_text()
            if call["stdout"].endswith(".json"):
                json.loads(text)
    assert {p.name for p in directory.iterdir()} == used, "a scenario file nothing refers to"
    seen = [json.dumps(c["argv"]) for c in calls]
    assert len(seen) == len(set(seen)), "two answers for one argv"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_canned_files_hold_no_credential_material(scenario):
    text = "".join(p.read_text() for p in (STUB / "scenarios" / scenario).iterdir())
    for needle in ("private_key", "BEGIN PRIVATE KEY", "ya29.", "client_secret", "refresh_token"):
        assert needle not in text


def test_the_error_scenarios_print_the_words_real_gcloud_prints():
    texts = {s: " ".join(c.get("stderr", "") for c in calls_of(s)) for s in SCENARIOS}
    assert "HTTPError 403" in texts["forbidden"] and "denied on resource" in texts["forbidden"]
    assert "HTTPError 404" in texts["not-found"] and "not found: 404" in texts["not-found"]
    assert "RESOURCE_EXHAUSTED" in texts["throttled"] and "HTTPError 429" in texts["throttled"]
    assert "Reauthentication required" in texts["unauthenticated"]
    assert "gcloud auth login" in texts["unauthenticated"]
    assert "getIamPolicy" in texts["partial-iam"]


def test_the_scenarios_the_fixtures_need_exist():
    assert set(SCENARIOS) >= {
        "healthy", "broken", "forbidden", "not-found", "throttled", "unauthenticated", "partial-iam",
    }  # fmt: skip
