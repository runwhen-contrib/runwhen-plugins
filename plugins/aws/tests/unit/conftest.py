import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runwhen_capability.testing import make_context, run_main

# At run time a skill's lib/ is on sys.path (CAP-12); its package is named after the skill.
for lib in sorted((Path(__file__).parents[2] / "skills").glob("*/lib")):
    sys.path.insert(0, str(lib))

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"

ACCOUNT = {
    "path": "aws/accounts/123456789012",
    "type": "aws/account",
    "name": "123456789012",
}
FUNCTION = {
    "path": "aws/accounts/123456789012/regions/eu-west-1/lambda-functions/orders-api",
    "type": "aws/lambda-function",
    "name": "orders-api",
    "account": "123456789012",
    "region": "eu-west-1",
}


def load_script(skill, name):
    spec = importlib.util.spec_from_file_location(
        f"{skill.replace('-', '_')}_{name}", PLUGIN / "skills" / skill / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAws:
    """Stands in for ctx.run: answers `aws` from the first handler whose words start the call.

    Like the SDK's ctx.run under v13 it would raise on a non-zero exit, but aws_json must ask for
    check=False and classify itself, so a call that does not pass check=False fails here. Every
    call must end in `--region R --output json --no-cli-pager`. `calls` holds
    {"words", "region", "timeout"}; `words` is the argv without `aws` and without that tail."""

    def __init__(self):
        self.handlers = []
        self.calls = []

    def on(self, *words, region=None, body=None, rc=0, stderr="", stdout=None, then=()):
        """Answer a call that starts with `words` (and is for `region`). `body` is JSON-encoded;
        `then` is a list of further (rc, stdout, stderr) answers for the following calls."""
        out = json.dumps(body) if stdout is None and body is not None else (stdout or "")
        answers = [(rc, out, stderr), *then]
        self.handlers.append({"words": list(words), "region": region, "answers": answers})
        return self

    def words(self):
        return [c["words"] for c in self.calls]

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False, "aws_json must pass check=False and classify the exit itself"
        assert argv[0] == "aws"
        assert argv[-5] == "--region" and argv[-3:] == ["--output", "json", "--no-cli-pager"]
        words, region = [str(a) for a in argv[1:-5]], argv[-4]
        self.calls.append({"words": words, "region": region, "timeout": timeout})
        for handler in self.handlers:
            if words[: len(handler["words"])] == handler["words"] and handler["region"] in (
                None,
                region,
            ):
                rc, out, err = handler["answers"][0]
                if len(handler["answers"]) > 1:
                    handler["answers"].pop(0)
                if rc == "timeout":  # what ctx.run raises when the call outlives its timeout
                    raise subprocess.TimeoutExpired(argv, timeout)
                return subprocess.CompletedProcess(argv, rc, out, err)
        raise AssertionError(f"no fake answer for aws {' '.join(words)} --region {region}")


@pytest.fixture
def fake_aws():
    return FakeAws()


@pytest.fixture
def run_script(fake_aws):
    """run_script("aws-base", "discover", resource=ACCOUNT, settings={...}, **inputs): the
    script's main under the real v13 context, aws answered by `fake_aws`. Returns the result
    envelope, the errors and the files written under $RW_OUT."""

    def run(skill, script, *, resource, settings=None, now=None, **inputs):
        main = load_script(skill, script).main
        defaults = json.loads(json.dumps(DEFAULT_SETTINGS[skill]))
        with make_context(
            resource=resource, settings={**defaults, **(settings or {})}, now=now
        ) as tc:
            tc.ctx.run = fake_aws
            envelope = run_main(main, tc, **inputs)
            files = {p.name: p.read_text() for p in Path(tc.out_dir).iterdir()}
            return SimpleNamespace(
                envelope=envelope, errors=list(tc.errors), files=files, calls=fake_aws.calls
            )

    return run


DEFAULT_SETTINGS = {
    "aws-base": {"maxItems": 200, "regions": [], "defaultRegion": "us-east-1"},
    "aws-lambda-health": {
        "maxItems": 200,
        "maxLogEvents": 50,
        "errorPattern": '?ERROR ?Exception ?"Task timed out" ?"Runtime.ExitError"',
        "errorRateThreshold": 0.05,
        "throttleThreshold": 1,
        "durationTimeoutRatio": 0.9,
    },
}

# `aws <service>` -> the IAM service prefix (`aws s3api` is s3; the rest are their own prefix)
IAM_PREFIX = {
    "lambda": "lambda",
    "ec2": "ec2",
    "cloudwatch": "cloudwatch",
    "logs": "logs",
    "sts": "sts",
    "s3": "s3",
    "s3api": "s3",
    "sqs": "sqs",
    "eks": "eks",
    "ce": "ce",
    "elasticache": "elasticache",
}
NEEDS_NO_PERMISSION = {("sts", "get-caller-identity")}
# an operation whose IAM action is not its CamelCased name
IAM_ACTION = {
    ("s3api", "list-buckets"): "s3:ListAllMyBuckets",
    ("s3api", "head-bucket"): "s3:ListBucket",
    ("s3api", "head-object"): "s3:GetObject",
    ("s3api", "list-objects"): "s3:ListBucket",
    ("s3api", "list-objects-v2"): "s3:ListBucket",
    ("s3api", "list-object-versions"): "s3:ListBucketVersions",
}


def iam_action(words):
    """The IAM action an `aws <service> <operation>` call needs, or None when it needs none."""
    service, operation = words[0], words[1]
    if (service, operation) in NEEDS_NO_PERMISSION:
        return None
    if (service, operation) in IAM_ACTION:
        return IAM_ACTION[service, operation]
    return f"{IAM_PREFIX[service]}:{''.join(p.capitalize() for p in operation.split('-'))}"


@pytest.fixture
def stub_on_path(monkeypatch):
    """The aws CLI stub first on PATH; set AWS_STUB_SCENARIO per test with `use(name)`."""
    monkeypatch.setenv("PATH", f"{STUB / 'bin'}:{os.environ['PATH']}")
    monkeypatch.delenv("AWS_REGION", raising=False)

    def use(scenario):
        monkeypatch.setenv("AWS_STUB_SCENARIO", scenario)

    return use


@pytest.fixture
def run_with_stub(stub_on_path):
    """Like run_script, but ctx.run is real and `aws` is the stub: what `rwtask test --record`
    does. use(scenario) first."""

    def run(skill, script, *, resource, settings=None, now=None, **inputs):
        main = load_script(skill, script).main
        defaults = json.loads(json.dumps(DEFAULT_SETTINGS[skill]))
        with make_context(
            resource=resource, settings={**defaults, **(settings or {})}, now=now
        ) as tc:
            envelope = run_main(main, tc, **inputs)
            files = {p.name: p.read_text() for p in Path(tc.out_dir).iterdir()}
            return SimpleNamespace(envelope=envelope, errors=list(tc.errors), files=files)

    run.use = stub_on_path
    return run


def words_matching(pattern, calls):
    rx = re.compile(pattern)
    return [c for c in calls if rx.search(" ".join(c["words"]))]
