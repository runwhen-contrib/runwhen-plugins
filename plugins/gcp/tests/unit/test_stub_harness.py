"""The gcloud/curl stubs and their harness: nothing can reach a real Google account, a missing call
is loud, and every canned answer cites its reference and the SDK file that formats it."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"
sys.path.insert(0, str(STUB))
import harness  # noqa: E402

SCENARIOS = sorted(p.name for p in (STUB / "scenarios").iterdir() if p.is_dir())
P = "shop-prod-1234"
BILLING = f"--billing-project={P}"
IDENTITIES = json.loads((STUB / "identities.json").read_text())
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
    spec = importlib.util.spec_from_file_location("stublib", STUB / "stublib.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    # no metadata server to fall back to: gcloud reads GCE_METADATA_ROOT (not _HOST) for its own
    # metadata client, and is told not to look for one at all
    assert env["GCE_METADATA_ROOT"] == "127.0.0.1:1" and "GCE_METADATA_HOST" not in env
    assert env["CLOUDSDK_CORE_CHECK_GCE_METADATA"] == "false"


def test_the_stubs_are_first_on_path_whatever_the_callers_path_holds(config_dir, tmp_path):
    real = tmp_path / "real-tools-dir"
    real.mkdir()
    for tool in harness.STUBBED:
        (real / tool).write_text("#!/bin/sh\necho REAL\n")
        (real / tool).chmod(0o755)
    env = harness.stub_env("healthy", config_dir, {"PATH": f"{real}{os.pathsep}/usr/bin"})
    assert env["PATH"].split(os.pathsep)[0] == str(harness.BIN)
    done = subprocess.run(
        ["gcloud", "projects", "describe", P, BILLING], env=env, capture_output=True, text=True
    )
    assert "REAL" not in done.stdout and done.returncode == 0, done.stderr


@pytest.mark.parametrize("tool", ["gsutil", "bq"])
def test_gsutil_and_bq_are_blocked_so_the_real_ones_never_run(config_dir, tool):
    env = harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})
    done = subprocess.run([tool, "ls"], env=env, capture_output=True, text=True)
    assert done.returncode == 2 and f"{tool} (stub)" in done.stderr
    assert "refusing to run the real one" in done.stderr or "does not use" in done.stderr


def test_the_harness_refuses_to_run_when_a_stubbed_tool_would_not_be_the_stub(
    config_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "BIN", tmp_path)  # a bin directory without the stubs
    with pytest.raises(ValueError, match="does not resolve to the stub"):
        harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})


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


@pytest.mark.parametrize(
    "tool, args", [("gcloud", ["projects", "describe", P]), ("curl", ["https://x.example"])]
)
def test_the_stubs_refuse_to_answer_when_not_started_by_the_harness(tmp_path, tool, args):
    env = {
        "PATH": "/usr/bin:/bin",
        "GCP_STUB_SCENARIO": "healthy",
        "CLOUDSDK_CONFIG": str(tmp_path),
    }
    done = subprocess.run(
        [str(STUB / "bin" / tool), *args], env=env, capture_output=True, text=True
    )
    assert done.returncode == 2 and "refusing to run" in done.stderr
    env["GCP_STUB_HARNESS"] = "0"
    done = subprocess.run(
        [str(STUB / "bin" / tool), *args], env=env, capture_output=True, text=True
    )
    assert done.returncode == 2 and "refusing to run" in done.stderr


def test_only_the_harness_sets_the_variable_the_stubs_check(config_dir):
    assert harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})["GCP_STUB_HARNESS"] == "1"
    texts = {p: p.read_text() for p in STUB.glob("*.py")} | {
        p: p.read_text() for p in (STUB / "bin").iterdir()
    }
    setters = [p.name for p, t in texts.items() if "GCP_STUB_HARNESS" in t and '"1"' in t]
    assert set(setters) <= {"harness.py", "stublib.py"}


def test_the_scripts_are_executable_and_the_stubs_are_isolated_python3():
    for name in ("gcloud", "curl", "gsutil", "bq"):
        assert (STUB / "bin" / name).stat().st_mode & stat.S_IXUSR
    for name in ("gcloud", "curl"):
        assert (STUB / "bin" / name).read_text().startswith("#!/usr/bin/env -S python3 -I\n")
    for name in ("record.sh", "capture/capture.sh"):
        assert (STUB / name).stat().st_mode & stat.S_IXUSR


# ---- matching ---------------------------------------------------------------------------------


def test_a_known_argv_prints_its_file_and_exits_with_its_rc(config_dir):
    done = run_stub("healthy", config_dir, "projects", "describe", P, BILLING, "--format=json")
    assert done.returncode == 0 and json.loads(done.stdout)["projectId"] == P
    done = run_stub(
        "forbidden", config_dir, "storage", "buckets", "list", f"--project={P}", "--raw", BILLING
    )
    assert done.returncode == 1 and done.stdout == "[]\n"  # a list verb prints [] before its error
    assert "HTTPError 403" in done.stderr and done.stderr.startswith(
        "ERROR: (gcloud.storage.buckets.list)"
    )


@pytest.mark.parametrize(
    "noise",
    [["--format=json"], ["--format", "json"], ["--quiet"], ["-q"], ["--format=json", "--quiet"]],
)
def test_output_and_prompt_flags_are_dropped_before_matching(config_dir, noise):
    done = run_stub("healthy", config_dir, "projects", "describe", P, BILLING, *noise)
    assert done.returncode == 0, done.stderr


def test_an_unknown_argv_exits_2_and_names_the_argv(config_dir):
    done = run_stub("healthy", config_dir, "storage", "buckets", "delete", "gs://shop-assets")
    assert done.returncode == 2 and done.stdout == ""
    assert '"storage", "buckets", "delete", "gs://shop-assets"' in done.stderr
    assert "no canned answer" in done.stderr


def test_flag_order_matters_nothing_is_sorted(config_dir):
    done = run_stub(
        "healthy", config_dir, "storage", "buckets", "list", "--raw", f"--project={P}", BILLING
    )
    assert done.returncode == 2


def test_an_unknown_scenario_exits_2(config_dir):
    env = harness.stub_env("healthy", config_dir, {"PATH": "/usr/bin"})
    env["GCP_STUB_SCENARIO"] = "missing"
    done = subprocess.run(["gcloud", "projects", "list"], env=env, capture_output=True, text=True)
    assert done.returncode == 2 and "no scenario" in done.stderr


def test_the_scenario_is_not_read_from_a_variable_the_runner_strips():
    # rwtask drops every RW_* variable from a task's environment, so the stub (a grandchild of the
    # task) would never see one
    for path in (
        STUB / "stublib.py",
        STUB / "bin" / "gcloud",
        STUB / "bin" / "curl",
        STUB / "harness.py",
    ):
        assert "RW_" not in path.read_text().replace("RW_*", ""), path.name


# ---- the curl stub ----------------------------------------------------------------------------


def monitoring_argv(cwd, header="@.gcp-monitoring-header"):
    sys.path.insert(0, str(PLUGIN / "skills" / "gcp-bucket-health" / "lib"))
    from gcp_bucket_health import monitoring

    url = monitoring.url(P, datetime(2026, 11, 4, 6, tzinfo=UTC))
    return ["-sS", "--fail-with-body", "--max-redirs", "0", "-H", header, url]


def run_curl(scenario, config_dir, cwd, argv):
    env = harness.stub_env(scenario, config_dir, {"PATH": "/usr/bin:/bin"})
    return subprocess.run(["curl", *argv], env=env, cwd=cwd, capture_output=True, text=True)


def write_header(cwd, token=IDENTITIES["fakeToken"], mode=0o600, name=".gcp-monitoring-header"):
    path = Path(cwd) / name
    path.write_text(f"Authorization: Bearer {token}\nX-Goog-User-Project: {P}\n")
    path.chmod(mode)
    return path


def test_curl_answers_the_monitoring_query_whatever_the_time_window(config_dir, tmp_path):
    write_header(tmp_path)
    done = run_curl("healthy", config_dir, tmp_path, monitoring_argv(tmp_path))
    assert done.returncode == 0, done.stderr
    assert {s["resource"]["labels"]["bucket_name"] for s in json.loads(done.stdout)["timeSeries"]} == {
        "shop-assets", "shop-backups", "shop-logs",
    }  # fmt: skip
    assert stub_module().normalise("curl", ["x?interval.endTime=2026-11-04T06%3A00%3A00Z&a=b"]) == [
        "x?interval.endTime=<T>&a=b"
    ]


def test_curl_keeps_the_error_body_and_exits_22_like_fail_with_body(config_dir, tmp_path):
    write_header(tmp_path)
    done = run_curl("monitoring-disabled", config_dir, tmp_path, monitoring_argv(tmp_path))
    assert done.returncode == 22
    assert json.loads(done.stdout)["error"]["details"][0]["reason"] == "SERVICE_DISABLED"
    assert done.stderr == "curl: (22) The requested URL returned error: 403\n"


@pytest.mark.parametrize(
    "problem",
    ["no header", "token on argv", "world readable", "wrong token", "two headers"],
)
def test_curl_refuses_a_call_that_does_not_keep_the_token_in_a_private_header_file(
    config_dir, tmp_path, problem
):
    argv = monitoring_argv(tmp_path)
    if problem == "no header":
        argv = argv[:4] + argv[6:]
    elif problem == "token on argv":
        write_header(tmp_path)
        argv = [*argv[:5], f"Authorization: Bearer {IDENTITIES['fakeToken']}", argv[6]]
    elif problem == "world readable":
        write_header(tmp_path, mode=0o644)
    elif problem == "wrong token":
        write_header(tmp_path, token="ya29.not-the-stubs-token")
    else:
        write_header(tmp_path)
        argv = [*argv[:6], "-H", "@.gcp-monitoring-header", argv[6]]
    done = run_curl("healthy", config_dir, tmp_path, argv)
    assert done.returncode == 2 and "curl (stub)" in done.stderr, done.stderr


def test_curl_and_gcloud_calls_do_not_answer_each_other(config_dir, tmp_path):
    write_header(tmp_path)
    # a gcloud call whose argv is the curl's is unknown to gcloud, and the other way round
    done = run_stub("healthy", config_dir, *monitoring_argv(tmp_path))
    assert done.returncode == 2
    done = run_curl("healthy", config_dir, tmp_path, ["-sS", "projects", "describe", P])
    assert done.returncode == 2


# ---- recording goes through the harness -------------------------------------------------------


def test_record_sh_always_goes_through_the_harness(tmp_path):
    fake = tmp_path / "rwtask"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$@" > "$OUT_ARGS"\n'
        'echo "$CLOUDSDK_CONFIG|$GCP_STUB_SCENARIO|$GCP_STUB_HARNESS|$(command -v gcloud)|$(command -v curl)" > "$OUT_ENV"\n'
    )
    fake.chmod(0o755)
    env = {**os.environ, "RWTASK": str(fake), "OUT_ARGS": str(tmp_path / "args"), "OUT_ENV": str(tmp_path / "env"),
           "CLOUDSDK_CONFIG": "/Users/me/.config/gcloud", "GOOGLE_APPLICATION_CREDENTIALS": "/Users/me/adc.json"}  # fmt: skip
    done = subprocess.run(
        [str(STUB / "record.sh"), "gcp-bucket-health/bucket-config", "broken", "--resource", f"gcp/projects/{P}"],
        env=env, capture_output=True, text=True, cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "args").read_text().split() == [
        "test", "gcp-bucket-health/bucket-config", "--plugin-dir", "plugins/gcp", "--record",
        "--scenario", "broken", "--resource", f"gcp/projects/{P}",
    ]  # fmt: skip
    config, scenario, harness_flag, gcloud_path, curl_path = (
        (tmp_path / "env").read_text().strip().split("|")
    )
    assert Path(config).name.startswith("gcp-stub-config-") and config != "/Users/me/.config/gcloud"
    assert (scenario, harness_flag) == ("broken", "1")
    assert Path(gcloud_path).parent == harness.BIN and Path(curl_path).parent == harness.BIN
    assert not Path(config).exists(), "the throwaway config directory is removed afterwards"


def test_record_sh_names_the_fixture_after_the_scenario_unless_told_otherwise(tmp_path):
    fake = tmp_path / "rwtask"
    fake.write_text('#!/bin/sh\necho "$@" > "$OUT_ARGS"\n')
    fake.chmod(0o755)
    env = {**os.environ, "RWTASK": str(fake), "OUT_ARGS": str(tmp_path / "args")}
    subprocess.run(
        [str(STUB / "record.sh"), "gcp-base", "healthy", "--scenario", "healthy-x"],
        env=env,
        check=True,
    )
    words = (tmp_path / "args").read_text().split()
    assert words.count("--scenario") == 1 and words[words.index("--scenario") + 1] == "healthy-x"
    done = subprocess.run(
        [str(STUB / "record.sh"), "gcp-base"], env=env, capture_output=True, text=True
    )
    assert done.returncode == 2


def test_the_readme_is_the_only_documented_recording_path():
    readme = (STUB / "README.md").read_text()
    assert "record.sh" in readme and "capture.sh" in readme
    documented = []
    for path in PLUGIN.rglob("*"):
        rel = path.relative_to(PLUGIN)
        if (
            not path.is_file()
            or path.suffix not in {".md", ".py", ".sh", ".yaml"}
            or rel.parts[:2] in {("tests", "unit")}
            or "scenarios" in rel.parts
            or "tests"
            in rel.parts[2:3]  # recorded fixtures: skills/<s>/tests/<task>/<scenario>.yaml
            or path.name in {"README.md", "record.sh"}
        ):
            continue
        if "--record" in path.read_text():
            documented.append(str(rel))
    assert documented == [], f"only tests/stub/README.md documents recording: {documented}"
    assert "harness.py" in (STUB / "record.sh").read_text()


# ---- the canned data --------------------------------------------------------------------------


def calls_of(scenario):
    return yaml.safe_load((STUB / "scenarios" / scenario / "calls.yaml").read_text())


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_minimal_parser_reads_calls_yaml_exactly_as_a_yaml_parser_does(scenario):
    text = (STUB / "scenarios" / scenario / "calls.yaml").read_text()
    assert stub_module().parse_calls(text) == yaml.safe_load(text)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_call_cites_its_reference_and_the_sdk_file_and_every_file_exists_and_is_used(
    scenario,
):
    directory = STUB / "scenarios" / scenario
    calls = calls_of(scenario)
    assert calls
    used = {"calls.yaml"}
    for call in calls:
        assert set(call) <= {"tool", "argv", "rc", "stdout", "stderr", "source", "cli_source"}
        assert re.fullmatch(r"https://(cloud|docs)\.google\.com/\S+", call["source"]), call
        tool = call.get("tool", "gcloud")
        if tool == "gcloud":
            # the SDK file that formats the answer, in the pinned gcloud
            assert call["cli_source"].startswith("gcloud 592.0.0: "), call
            assert re.search(r"(surface|googlecloudsdk)/\S+\.py", call["cli_source"]), call
            assert call["argv"][0] in ("projects", "storage", "auth")
            assert f"--billing-project={P}" in call["argv"], (
                "every gcloud call is billed to the project"
            )
        else:
            assert tool == "curl" and call["cli_source"].startswith("curl 8.14.1")
            assert call["argv"][:5] == ["-sS", "--fail-with-body", "--max-redirs", "0", "-H"]
            assert call["argv"][5].startswith("@") and "ya29" not in json.dumps(call["argv"])
            assert "interval.startTime=<T>" in call["argv"][6], "the time window is normalised"
        assert not {"--format=json", "--quiet", "-q"} & set(call["argv"]), (
            "argv is stored normalised"
        )
        if call["rc"] == 0:
            assert call.get("stdout") and "stderr" not in call
        else:
            assert call["stderr"].startswith(("ERROR: (gcloud.", "curl: ("))
            assert "stdout" not in call or call["stdout"].startswith(
                ("buckets-list", "monitoring-")
            )
        if call.get("stdout"):
            used.add(call["stdout"])
            text = (directory / call["stdout"]).read_text()
            if call["stdout"].endswith(".json"):
                json.loads(text)
    assert {p.name for p in directory.iterdir()} == used, "a scenario file nothing refers to"
    seen = [json.dumps([c.get("tool"), c["argv"]]) for c in calls]
    assert len(seen) == len(set(seen)), "two answers for one argv"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_canned_files_hold_no_credential_material(scenario):
    text = "".join(p.read_text() for p in (STUB / "scenarios" / scenario).iterdir())
    for needle in ("private_key", "BEGIN PRIVATE KEY", "ya29.", "client_secret", "refresh_token"):
        assert needle not in text


def test_the_listing_rows_carry_the_acls_the_full_projection_returns():
    rows = json.loads((STUB / "scenarios" / "broken" / "buckets-list.json").read_text())
    legacy = next(r for r in rows if r["name"] == "shop-legacy")
    assert legacy["acl"] and legacy["defaultObjectAcl"], (
        "acl and defaultObjectAcl are in the list rows"
    )
    assert {"allUsers"} & {e["entity"] for e in legacy["acl"]}
    for row in rows:  # the list rows are the describe rows
        assert row == json.loads(
            (STUB / "scenarios" / "broken" / f"bucket-{row['name']}.json").read_text()
        )


def test_the_policies_have_only_what_gcloud_asks_for():
    # get_bucket_iam_policy asks for fields=bindings,etag: no kind, resourceId or version
    for path in (STUB / "scenarios").glob("*/iam-*.json"):
        assert set(json.loads(path.read_text())) == {"bindings", "etag"}, path


def test_the_error_scenarios_print_the_words_real_gcloud_prints():
    texts = {s: " ".join(c.get("stderr", "") for c in calls_of(s)) for s in SCENARIOS}
    assert "HTTPError 403" in texts["forbidden"] and "denied on resource" in texts["forbidden"]
    assert "gs://nope not found: 404" in texts["not-found"]
    assert "RESOURCE_EXHAUSTED" in texts["throttled"] and "Quota exceeded" in texts["throttled"]
    assert "do not currently have an active account" in texts["unauthenticated"]
    assert "gcloud auth login" in texts["unauthenticated"]
    assert (
        "SERVICE_DISABLED" in texts["api-disabled"] or "has not been used" in texts["api-disabled"]
    )
    assert "RESOURCE_EXHAUSTED" in texts["partial-throttled"]
    assert (
        "returned error: 403" in texts["monitoring-disabled"]
    )  # curl's own line; the body is stdout
    body = (STUB / "scenarios" / "monitoring-disabled" / "monitoring-project.json").read_text()
    assert "SERVICE_DISABLED" in body and "monitoring.googleapis.com" in body


def test_the_scenarios_the_fixtures_need_exist():
    assert set(SCENARIOS) == {
        "healthy", "broken", "forbidden", "not-found", "throttled", "unauthenticated",
        "api-disabled", "project-denied", "partial-throttled", "du-fallback", "monitoring-disabled",
    }  # fmt: skip
