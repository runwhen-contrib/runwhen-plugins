"""The aws CLI stub (tests/stub/bin/aws) and its scenarios: how a call is looked up, and that every
canned file is documented, parseable and reachable."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"
SCENARIOS = STUB / "scenarios"
AWS = STUB / "bin" / "aws"
DOC_PREFIX = "https://docs.aws.amazon.com/"


def scenario_dirs():
    return sorted(p for p in SCENARIOS.iterdir() if p.is_dir() and not p.name.startswith("_"))


def calls_of(scenario_dir):
    return json.loads((scenario_dir / "calls.yaml").read_text())["calls"]


def run_stub(scenario, *argv, log=None):
    env = {"PATH": os.environ["PATH"]}
    if scenario is not None:
        env["AWS_STUB_SCENARIO"] = scenario
    if log is not None:
        env["AWS_STUB_LOG"] = str(log)
    return subprocess.run([str(AWS), *argv], env=env, capture_output=True, text=True)


def test_the_stub_is_a_standalone_python_script_without_dependencies():
    first = AWS.read_text().splitlines()[0]
    assert first == "#!/usr/bin/env -S python3 -I"
    assert os.access(AWS, os.X_OK)
    imports = {
        line.split()[1].split(".")[0]
        for line in AWS.read_text().splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= set(sys.stdlib_module_names)


@pytest.mark.parametrize("directory", scenario_dirs(), ids=lambda p: p.name)
def test_every_call_names_the_doc_page_its_shape_follows_and_its_files_exist(directory):
    calls = calls_of(directory)
    assert calls
    seen = set()
    for call in calls:
        assert call["argv"] and all(isinstance(w, str) for w in call["argv"])
        assert "--output" not in call["argv"] and "--no-cli-pager" not in call["argv"]
        assert call["doc"].startswith(DOC_PREFIX)
        assert isinstance(call["rc"], int)
        key = json.dumps(call["argv"])
        assert key not in seen, "two answers to one argv: the first would win"
        seen.add(key)
        if call["rc"] == 0:
            assert call["stdout"] and not call["stderr"]
            path = directory / call["stdout"]
            assert path.is_file(), path
            text = path.read_text()
            if (
                text.strip()
            ):  # the CLI prints nothing for an empty response (get-function-concurrency)
                json.loads(text)  # what the CLI prints with --output json
        else:
            assert not call["stdout"]
            assert call["stderr"].startswith("\naws: [ERROR]: ") or "stub" in call["stderr"]


def test_no_canned_file_is_left_unreferenced():
    referenced = set()
    for directory in scenario_dirs():
        for call in calls_of(directory):
            if call["stdout"]:
                referenced.add((directory / call["stdout"]).resolve())
    on_disk = {p.resolve() for p in SCENARIOS.rglob("*") if p.is_file() and p.name != "calls.yaml"}
    assert on_disk == referenced


def test_every_failure_reproduces_the_cli_wording_and_the_exit_code_254():
    for directory in scenario_dirs():
        for call in calls_of(directory):
            if call["rc"]:
                assert call["rc"] == 254  # the CLI's exit code for a service error
                assert "\naws: [ERROR]: An error occurred (" in call["stderr"]


def test_a_throttle_says_the_cli_gave_up_retrying():
    for call in calls_of(SCENARIOS / "throttled"):
        if call["rc"]:
            assert "(reached max retries: 2)" in call["stderr"]


def test_a_known_call_is_answered_with_its_canned_stdout_and_exit_code():
    done = run_stub("healthy", "sts", "get-caller-identity", "--region", "us-east-1")
    assert done.returncode == 0 and json.loads(done.stdout)["Account"] == "123456789012"
    done = run_stub("forbidden", "ec2", "describe-regions", "--region", "us-east-1")
    assert done.returncode == 254 and "UnauthorizedOperation" in done.stderr and done.stdout == ""


def test_output_and_pager_flags_are_noise_and_the_order_of_the_rest_is_not():
    noisy = [
        "sts",
        "get-caller-identity",
        "--region",
        "us-east-1",
        "--output",
        "json",
        "--no-cli-pager",
    ]
    assert run_stub("healthy", *noisy).returncode == 0
    assert (
        run_stub(
            "healthy", "-o", "json", "sts", "get-caller-identity", "--region", "us-east-1"
        ).returncode
        == 0
    )
    swapped = run_stub(
        "healthy", "sts", "get-caller-identity", "--region", "us-east-1", "--output", "text"
    )
    assert swapped.returncode == 2  # --output text is not noise
    reordered = run_stub("healthy", "--region", "us-east-1", "sts", "get-caller-identity")
    assert reordered.returncode == 2


def test_start_and_end_times_match_whatever_the_run_clock_was():
    base = ["cloudwatch", "get-metric-statistics", "--namespace", "AWS/Lambda", "--metric-name", "Invocations",
            "--dimensions", "Name=FunctionName,Value=orders-api"]  # fmt: skip
    tail = ["--period", "300", "--statistics", "Sum", "--region", "eu-west-1"]
    for start, end in (
        ("2026-11-04T00:00:00Z", "2026-11-04T01:00:00Z"),
        ("1999-01-01T00:00:00Z", "x"),
    ):
        done = run_stub("healthy", *base, "--start-time", start, "--end-time", end, *tail)
        assert done.returncode == 0 and json.loads(done.stdout)["Label"] == "Invocations"
    other = run_stub(
        "healthy", *base, "--start-time", "a", "--end-time", "b", *tail[:-1], "us-east-1"
    )
    assert other.returncode == 2  # the region is part of the call


def test_an_unknown_argv_exits_2_and_names_the_call_and_the_known_ones():
    done = run_stub("healthy", "s3", "ls")
    assert done.returncode == 2 and done.stdout == ""
    assert "no canned response for: aws s3 ls" in done.stderr
    assert "aws sts get-caller-identity --region us-east-1" in done.stderr


@pytest.mark.parametrize("scenario", [None, "", "nope", "../healthy", ".hidden"])
def test_a_missing_or_unknown_scenario_exits_2(scenario):
    done = run_stub(scenario, "sts", "get-caller-identity")
    assert done.returncode == 2 and done.stderr.startswith("aws stub: ")


def test_the_log_file_records_each_normalised_argv(tmp_path):
    log = tmp_path / "calls.log"
    run_stub(
        "healthy",
        "sts",
        "get-caller-identity",
        "--region",
        "us-east-1",
        "--output",
        "json",
        log=log,
    )
    run_stub("healthy", "s3", "ls", log=log)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert lines == [["sts", "get-caller-identity", "--region", "us-east-1"], ["s3", "ls"]]


def test_the_stub_refuses_to_run_without_aws_stub_scenario_and_ignores_the_rw_name():
    # the runner strips RW_*, so an RW_ name would work by hand and fail under rwtask: not accepted
    for env in (
        {"PATH": os.environ["PATH"]},
        {"PATH": os.environ["PATH"], "RW_STUB_SCENARIO": "healthy"},
        {"PATH": os.environ["PATH"], "AWS_STUB_SCENARIO": ""},
    ):
        done = subprocess.run(
            [str(AWS), "sts", "get-caller-identity", "--region", "us-east-1"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 2 and done.stdout == ""
        assert "AWS_STUB_SCENARIO is not set: refusing to run" in done.stderr
    done = subprocess.run(
        [str(AWS), "--version"], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True
    )
    assert done.returncode == 2  # not even --version: a run without a scenario is a mistake


# --- record.sh: recording always goes through the harness -----------------------------------------------

RECORD = STUB / "record.sh"


def run_record(tmp_path, *args, env=None):
    """record.sh with `rwtask` replaced by a script that prints what the harness set up."""
    fake = tmp_path / "rwtask"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "aws=$(command -v aws)"\n'
        'echo "scenario=$AWS_STUB_SCENARIO"\n'
        "echo \"aws_vars=$(env | sed -n 's/^\\(AWS_[A-Z_]*\\)=.*/\\1/p' | sort | tr '\\n' ' ')\"\n"
        'echo "args=$*"\n'
    )
    fake.chmod(0o755)
    base = {"PATH": os.environ["PATH"], "RWTASK": str(fake), **(env or {})}
    return subprocess.run([str(RECORD), *args], env=base, capture_output=True, text=True)


def test_record_sh_puts_the_stub_first_names_the_scenario_and_drops_ambient_aws_variables(tmp_path):
    done = run_record(
        tmp_path,
        "aws-lambda-health/function-config",
        "healthy",
        "healthy",
        "aws/accounts/123456789012/regions/eu-west-1/lambda-functions/orders-api",
        env={
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_PROFILE": "prod",
            "AWS_ENDPOINT_URL": "http://elsewhere",
        },
    )
    assert done.returncode == 0, done.stderr
    lines = dict(line.split("=", 1) for line in done.stdout.splitlines())
    assert lines["aws"] == str(STUB / "bin" / "aws")
    assert lines["scenario"] == "healthy"
    assert lines["aws_vars"].split() == ["AWS_STUB_SCENARIO"]  # nothing else of the caller's AWS_*
    args = lines["args"]
    assert args.startswith("test aws-lambda-health/function-config --plugin-dir ")
    assert "--record --force --scenario healthy --resource aws/accounts/123456789012" in args


def test_record_sh_takes_the_fixture_name_apart_from_the_stub_scenario(tmp_path):
    done = run_record(
        tmp_path, "aws-lambda-health/function-metrics", "idle", "no-log-group", "aws/x"
    )
    lines = dict(line.split("=", 1) for line in done.stdout.splitlines())
    assert lines["scenario"] == "no-log-group" and "--scenario idle " in lines["args"]


def test_record_sh_refuses_an_unknown_scenario_and_missing_arguments(tmp_path):
    done = run_record(tmp_path, "aws-lambda-health/function-config", "x", "no-such", "aws/x")
    assert done.returncode == 2 and "no stub scenario 'no-such'" in done.stderr
    done = run_record(tmp_path, "aws-lambda-health/function-config")
    assert done.returncode == 2 and done.stdout == ""


def test_there_is_no_rwtask_record_without_the_harness_in_the_docs():
    text = (STUB / "README.md").read_text()
    assert "record.sh" in text and "verify.sh" in text
    assert "RW_STUB_SCENARIO" not in text.replace("not `RW_STUB_SCENARIO`", "")
