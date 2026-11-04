"""No recorded fixture, stub scenario or captured unit data may hold a credential, and every task
has the scenarios the fixture brief asks for.

A recording keeps what the tool printed. `aws lambda list-functions` and `get-function-configuration`
print a function's `Environment.Variables`; the skills narrow both with `--query` so they are never
requested, and a recording of a real account must not hold a key. This scans every skill's
`tests/**/*.yaml` fixture, the stub scenarios and `tests/unit/data/**` for the shapes of AWS
credentials. The two values the broken-logs scenario plants on purpose (to prove the output scrubs
them) are listed with why; a fixture's `expect` must never hold them, because that is the scrubbed
output."""

import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]  # plugins/aws

# an access key id is 20 characters (AKIA / ASIA + 16); an IAM user or role unique id is 21
# (AIDA / AROA + 17)
ACCESS_KEY_ID = re.compile(r"\b(?:(?:AKIA|ASIA)[0-9A-Z]{16}|(?:AIDA|AROA)[0-9A-Z]{17})\b")
# a real account id in a recording: the only one a recording may hold is the documented example
ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
ARN_ACCOUNT = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:([0-9]{12}):")
# a bare secret access key: exactly 40 base64 characters, mixed case with a digit or / or +
BARE_KEY = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
SECRET_KEY = re.compile(
    r"(?i)(?:secret_?access_?key|session_?token|security_?token)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{16,}"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")

# Values that may appear in a recording, each with why. Add one only when it is a documented
# example or a planted test value, never a real credential.
ALLOWED_ACCOUNT = "123456789012"
# Identifiers AWS's own documentation uses as examples; they may appear anywhere in a recording
DOCUMENTED = {
    "AIDACKCEVSQ6C2EXAMPLE": "the UserId of AWS's get-caller-identity example (the sts canned answer)",
}
ALLOWED = {
    "AKIAIOSFODNN7EXAMPLE": "the access key id in AWS's own documentation; planted in the broken log",
    "hunter2": "a placeholder password planted in the broken log to prove the output scrubs it",
}


def scanned_files():
    files = []
    for skill in sorted((PLUGIN / "skills").iterdir()):
        if (skill / "tests").is_dir():
            files += sorted((skill / "tests").glob("**/*.yaml"))
    files += sorted((PLUGIN / "tests" / "stub" / "scenarios").glob("**/*"))
    files += sorted((PLUGIN / "tests" / "unit" / "data").glob("**/*.json"))
    return [f for f in files if f.is_file()]


def environment_variables(node, where="$"):
    """[where] of every `Variables` map with an entry, as Lambda prints Environment.Variables."""
    hits = []
    if isinstance(node, dict):
        if isinstance(node.get("Variables"), dict) and node["Variables"]:
            hits.append(where)
        for key, value in node.items():
            hits += environment_variables(value, f"{where}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits += environment_variables(value, f"{where}[{i}]")
    elif isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
        try:  # a recorded stdout
            hits += environment_variables(json.loads(node), where)
        except ValueError:
            pass
    return hits


def looks_like_a_secret_key(text):
    """40 base64 characters that are not a hex digest (a git sha, a sha1) or a lower-case word."""
    return (
        any(c.isupper() for c in text)
        and any(c.islower() for c in text)
        and any(c.isdigit() or c in "/+" for c in text)
        and text not in ALLOWED
    )


def credential_hits(text):
    """Credential-shaped strings in `text` that are not on the allowlist."""
    hits = [m for m in ACCESS_KEY_ID.findall(text) if m not in ALLOWED and m not in DOCUMENTED]
    hits += [m for m in SECRET_KEY.findall(text)]
    hits += [m for m in BARE_KEY.findall(text) if looks_like_a_secret_key(m)]
    hits += PRIVATE_KEY.findall(text)
    return hits


def account_hits(text):
    """12-digit numbers and ARN account fields other than the one documented example account."""
    found = ACCOUNT_ID.findall(text) + ARN_ACCOUNT.findall(text)
    return sorted({m for m in found if m != ALLOWED_ACCOUNT})


def test_the_scan_covers_the_fixtures_the_stub_and_the_unit_data():
    files = scanned_files()
    assert any("skills" in f.parts and "tests" in f.parts for f in files)
    assert any("scenarios" in f.parts for f in files)


def test_the_scanner_finds_a_real_account_id_and_lets_only_the_documented_one_through():
    assert account_hits("arn:aws:iam::123456789012:user/x and 123456789012") == []
    assert account_hits("account 210987654321") == ["210987654321"]
    assert account_hits("arn:aws:lambda:eu-west-1:210987654321:function:f") == ["210987654321"]
    assert account_hits("arn:aws-us-gov:iam::111111111111:role/r") == ["111111111111"]
    assert account_hits("timestamp 1730678400000") == []  # 13 digits is a millisecond clock


def test_the_scanner_finds_each_shape_it_looks_for():
    assert credential_hits("key AKIAABCDEFGHIJKLMNOP used")
    assert credential_hits("temporary ASIAABCDEFGHIJKLMNOP used")
    assert credential_hits("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert credential_hits("AWS_SESSION_TOKEN: IQoJb3JpZ2luX2VjEXAMPLEtoken")
    assert credential_hits("-----BEGIN RSA PRIVATE KEY-----")
    # a bare secret key (no name beside it) and the 21-character unique ids
    assert credential_hits("copied wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY from the console")
    assert credential_hits("userId AIDAJQABLZS4A3QDU576Q") and credential_hits(
        "AROAJQABLZS4A3QDU576Q"
    )
    assert not credential_hits("AIDAEXAMPLE")  # too short to be an id: a placeholder
    assert not credential_hits("sha da39a3ee5e6b4b0d3255bfef95601890afd80709")  # a hex digest
    assert not credential_hits("a path /aws/lambda/some-function-name-that-is-forty-chars-x")
    assert not credential_hits("key AKIAIOSFODNN7EXAMPLE is the documented example")
    assert not credential_hits("no credential here, only a [REDACTED] marker")
    doc = {"Functions": [{"Environment": {"Variables": {"DB_PASSWORD": "x"}}}]}
    assert environment_variables(doc) and environment_variables({"stdout": json.dumps(doc)})
    assert not environment_variables({"Environment": {"Variables": {}}})
    assert not environment_variables({"Environment": {"Error": {"ErrorCode": "x"}}})


@pytest.mark.parametrize("path", scanned_files(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_no_recording_holds_a_credential_or_a_functions_environment_variables(path):
    text = path.read_text()
    assert not credential_hits(text), f"{path.relative_to(PLUGIN)} holds a credential-shaped value"
    # a canned CLI output may be empty: the CLI prints nothing for an empty response
    doc = (
        yaml.safe_load(text)
        if path.suffix == ".yaml"
        else (json.loads(text) if text.strip() else {})
    )
    assert not environment_variables(doc), (
        f"{path.relative_to(PLUGIN)} records environment variables"
    )


@pytest.mark.parametrize("path", scanned_files(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_no_recording_holds_an_account_id_but_the_documented_example(path):
    assert account_hits(path.read_text()) == [], (
        f"{path.relative_to(PLUGIN)} holds another account id than {ALLOWED_ACCOUNT}"
    )


def fixtures():
    return sorted((PLUGIN / "skills").glob("*/tests/*/*.yaml"))


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_a_fixtures_expectation_is_the_scrubbed_output_never_a_planted_value(path):
    expect = yaml.safe_load(path.read_text())["expect"]
    shown = json.dumps(expect)
    for value in ALLOWED:
        assert value not in shown, f"{path.relative_to(PLUGIN)}: expect holds {value!r}"
    assert expect["status"] in {"ok", "partial", "failed"}
    if expect["status"] != "failed":
        assert expect.get("outputs"), "an ok or partial run asserts what it produced"


def test_the_planted_values_are_only_in_the_broken_log_recording():
    holders = {
        str(p.relative_to(PLUGIN))
        for p in scanned_files()
        for value in ALLOWED
        if value in p.read_text()
    }
    assert holders == {
        "skills/aws-lambda-health/tests/function-errors/broken.yaml",
        "tests/stub/scenarios/broken/logs-filter-log-events.json",
    }


STANDARD = {"healthy", "broken", "forbidden", "not-found"}


@pytest.mark.parametrize("task", ["function-config", "function-metrics", "function-errors"])
def test_every_health_task_has_the_four_standard_scenarios(task):
    names = {
        p.stem for p in (PLUGIN / "skills" / "aws-lambda-health" / "tests" / task).glob("*.yaml")
    }
    assert STANDARD <= names


def test_discover_has_its_scenarios_and_every_failure_kind_the_brief_names():
    names = {p.stem for p in (PLUGIN / "skills" / "aws-base" / "tests" / "discover").glob("*.yaml")}
    assert {"healthy", "partial", "forbidden", "expired", "throttled", "paged"} <= names


def test_the_fixture_error_codes_cover_the_classes_the_cli_produces():
    codes = set()
    for path in fixtures():
        codes |= set(yaml.safe_load(path.read_text())["expect"].get("errorCodes", []))
    assert {"permission_denied", "not_found", "rate_limited"} <= codes


def test_every_recorded_call_is_an_aws_call_with_an_explicit_region():
    for path in fixtures():
        for call in yaml.safe_load(path.read_text())["calls"]:
            argv = call["argv"]
            assert argv[0] == "aws" and argv[-3:] == ["--output", "json", "--no-cli-pager"]
            assert argv[-5] == "--region", f"{path.name}: {argv[:3]} names no region"
