"""awscli.py (shared by every aws skill): how a failed call is classified from the error code in
stderr, how a call is built, and that a truncated list never reads as complete."""

import json
import subprocess

import pytest
from aws_base import awscli
from aws_base.awscli import aws_json, aws_paged, classify_error, error_code, failure_message
from aws_base.errors import RwError
from conftest import FakeAws


def err(code, op="ListFunctions", message="boom", retries=None):
    tail = f" (reached max retries: {retries})" if retries is not None else ""
    return f"\naws: [ERROR]: An error occurred ({code}) when calling the {op} operation{tail}: {message}\n"


@pytest.mark.parametrize(
    "stderr,rc,code",
    [
        (err("AccessDeniedException"), 254, "permission_denied"),
        (err("AccessDenied", "GetObject"), 254, "permission_denied"),
        (err("UnauthorizedOperation", "DescribeRegions"), 254, "permission_denied"),
        (err("ExpiredToken", "GetCallerIdentity"), 254, "permission_denied"),
        (err("ExpiredTokenException"), 254, "permission_denied"),
        (err("UnrecognizedClientException"), 254, "permission_denied"),
        (err("InvalidClientTokenId", "GetCallerIdentity"), 254, "permission_denied"),
        (err("SignatureDoesNotMatch"), 254, "permission_denied"),
        (err("AuthFailure", "DescribeRegions"), 254, "permission_denied"),
        (err("OptInRequired", "DescribeInstances"), 254, "permission_denied"),
        (
            err("NoCredentials").replace(
                "calling the ListFunctions operation: boom", "Unable to locate credentials"
            ),
            253,
            "permission_denied",
        ),
        (
            '\naws: [ERROR]: Unable to locate credentials. You can configure credentials by running "aws login".\n',
            253,
            "permission_denied",
        ),
        (err("ResourceNotFoundException", "GetFunctionConfiguration"), 254, "not_found"),
        (err("NoSuchEntity", "GetRole"), 254, "not_found"),
        (err("InvalidInstanceID.NotFound", "DescribeInstances"), 254, "not_found"),
        (err("ThrottlingException", "FilterLogEvents", retries=2), 254, "rate_limited"),
        (err("Throttling", "GetMetricStatistics", retries=4), 254, "rate_limited"),
        (err("TooManyRequestsException", retries=2), 254, "rate_limited"),
        (err("RequestLimitExceeded", "DescribeRegions"), 254, "rate_limited"),
        (
            err("ParamValidation", message="x").replace(
                "calling the ListFunctions operation: ", ""
            ),
            252,
            "invalid_input",
        ),
        (
            "\naws: [ERROR]: An error occurred (ParamValidation): argument --max-items: invalid int value: 'abc'\n\nusage: aws [options]",
            252,
            "invalid_input",
        ),
        (err("ValidationException"), 254, "invalid_input"),
        (err("InvalidParameterValueException"), 254, "invalid_input"),
        (
            "\naws: [ERROR]: Provided region_name 'bad region' doesn't match a supported format.\n",
            255,
            "invalid_input",
        ),
        (
            '\naws: [ERROR]: Could not connect to the endpoint URL: "https://lambda.eu-west-9.amazonaws.com/2015-03-31/functions"\n',
            255,
            "unreachable",
        ),
        (
            '\naws: [ERROR]: Connect timeout on endpoint URL: "https://sts.eu-west-1.amazonaws.com/"\n',
            255,
            "timeout",
        ),
        (
            '\naws: [ERROR]: Read timeout on endpoint URL: "https://logs.eu-west-1.amazonaws.com/"\n',
            255,
            "timeout",
        ),
        (err("RequestTimeout"), 254, "timeout"),
        (err("ServiceUnavailable"), 254, "unreachable"),
        (err("InternalFailure"), 254, "internal"),
        ("something nobody planned for", 1, "internal"),
        ("", 124, "timeout"),
        ("", 127, "dependency"),
        ("aws: command not found", 127, "dependency"),
    ],
)
def test_a_failure_is_classified_from_the_error_code_in_stderr(stderr, rc, code):
    assert classify_error(stderr, rc) == code


def test_the_code_decides_not_the_exit_code():
    # rc 254 is every service error: only the code tells a throttle from a denial
    assert classify_error(err("ThrottlingException"), 254) == "rate_limited"
    assert classify_error(err("AccessDeniedException"), 254) == "permission_denied"
    # a denial message that mentions another word does not change the code
    assert (
        classify_error(err("AccessDeniedException", message="not found in policy"), 254)
        == "permission_denied"
    )
    assert (
        classify_error(
            err("ResourceNotFoundException", message="Access denied to the log group"), 254
        )
        == "not_found"
    )


def test_the_error_code_is_read_from_anywhere_in_stderr_and_from_old_cli_wording():
    assert (
        error_code("An error occurred (AccessDenied) when calling the X operation: y")
        == "AccessDenied"
    )
    assert error_code(err("ThrottlingException")) == "ThrottlingException"
    assert error_code("no code here") is None


def test_the_message_drops_the_prefix_and_the_usage_block_and_is_cut_to_1024():
    usage = "\n\nusage: aws [options] <command> <subcommand> [<subcommand> ...] [parameters]\nTo see help text, you can run:"
    text = failure_message(err("ParamValidation", message="m") + usage, 252)
    assert text.startswith("An error occurred (ParamValidation)") and "usage" not in text
    assert len(failure_message(err("X", message="y" * 5000), 254)) == 1024
    assert failure_message("", 255) == "aws exited 255"


# --- calls ---------------------------------------------------------------------------------------


def ctx_with(fake):
    from types import SimpleNamespace

    return SimpleNamespace(run=fake)


def test_a_call_names_the_region_and_asks_for_json_without_a_pager():
    seen = []

    def run(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        seen.append((argv, timeout, check))
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    assert aws_json(ctx_with(run), ["sts", "get-caller-identity"], region="eu-west-1") == {
        "ok": True
    }
    assert seen == [
        (
            [
                "aws",
                "sts",
                "get-caller-identity",
                "--region",
                "eu-west-1",
                "--output",
                "json",
                "--no-cli-pager",
            ],
            awscli.DEFAULT_TIMEOUT,
            False,
        )
    ]


@pytest.mark.parametrize(
    "region", ["", "EU-WEST-1", "eu-west", "eu west 1", "eu-west-1\n--profile x", None, 5]
)
def test_a_malformed_region_is_invalid_input_before_any_process_starts(region):
    fake = FakeAws()
    with pytest.raises(RwError) as exc:
        aws_json(ctx_with(fake), ["sts", "get-caller-identity"], region=region)
    assert exc.value.code == "invalid_input" and fake.calls == []


def test_a_non_zero_exit_raises_with_the_classified_code_and_the_clean_message():
    fake = FakeAws().on(
        "lambda", rc=254, stderr=err("AccessDeniedException", message="User: x is not authorized")
    )
    with pytest.raises(RwError) as exc:
        aws_json(ctx_with(fake), ["lambda", "list-functions"], region="eu-west-1")
    assert exc.value.code == "permission_denied"
    assert exc.value.message.startswith("An error occurred (AccessDeniedException)")


def test_a_run_that_times_out_is_timeout():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)

    with pytest.raises(RwError) as exc:
        aws_json(ctx_with(run), ["lambda", "list-functions"], region="eu-west-1", timeout=5)
    assert exc.value.code == "timeout"


def test_a_missing_aws_binary_is_a_dependency_error():
    def run(argv, **kw):
        raise FileNotFoundError("aws")

    with pytest.raises(RwError) as exc:
        aws_json(ctx_with(run), ["lambda", "list-functions"], region="eu-west-1")
    assert exc.value.code == "dependency"


def test_output_that_is_not_json_is_internal_and_empty_output_is_none():
    with pytest.raises(RwError) as exc:
        aws_json(ctx_with(FakeAws().on("x", stdout="not json")), ["x", "y"], region="eu-west-1")
    assert exc.value.code == "internal"
    assert aws_json(ctx_with(FakeAws().on("x", stdout="")), ["x", "y"], region="eu-west-1") is None


# --- pagination -----------------------------------------------------------------------------------

QUERY = "{Functions: Functions[].FunctionName, NextToken: NextToken}"


def pages(*docs):
    first, *rest = [(0, json.dumps(d), "") for d in docs]
    fake = FakeAws()
    fake.handlers.append({"words": ["lambda"], "region": None, "answers": [first, *rest]})
    return fake


def paged(fake, **kw):
    return aws_paged(
        ctx_with(fake),
        ["lambda", "list-functions", "--query", QUERY],
        region="eu-west-1",
        items_key="Functions",
        **kw,
    )


def test_pages_are_followed_with_the_starting_token_until_the_token_is_null():
    fake = pages(
        {"Functions": ["a", "b"], "NextToken": "T1"}, {"Functions": ["c"], "NextToken": None}
    )
    assert paged(fake, page_size=2) == (["a", "b", "c"], False)
    assert [c["words"][-4:] for c in fake.calls] == [
        ["--query", QUERY, "--max-items", "2"],
        ["--max-items", "2", "--starting-token", "T1"],
    ]


def test_a_list_cut_at_the_limit_is_truncated_when_the_service_has_more():
    fake = pages({"Functions": ["a", "b"], "NextToken": "T1"})
    assert paged(fake, limit=2) == (["a", "b"], True)
    assert len(fake.calls) == 1  # it did not read on


def test_a_list_that_ends_exactly_at_the_limit_is_complete():
    fake = pages({"Functions": ["a", "b"], "NextToken": None})
    assert paged(fake, limit=2) == (["a", "b"], False)


def test_the_last_page_asks_only_for_what_is_still_wanted():
    fake = pages(
        {"Functions": ["a", "b"], "NextToken": "T1"}, {"Functions": ["c"], "NextToken": "T2"}
    )
    assert paged(fake, limit=3, page_size=2) == (["a", "b", "c"], True)
    assert [c["words"][c["words"].index("--max-items") + 1] for c in fake.calls] == ["2", "1"]


def test_the_page_limit_stops_a_runaway_list_and_says_truncated(monkeypatch):
    monkeypatch.setattr(awscli, "MAX_PAGES", 3)
    fake = pages({"Functions": ["a"], "NextToken": "again"})
    assert paged(fake) == (["a", "a", "a"], True)


def test_a_query_that_drops_next_token_is_refused_not_read_as_complete():
    # --query 'Functions[]' prints a bare list: the CLI added NextToken, the query dropped it
    fake = pages(["a", "b"])
    with pytest.raises(RwError) as exc:
        paged(fake)
    assert exc.value.code == "internal" and "NextToken" in exc.value.message
    fake = pages({"Functions": ["a"]})
    with pytest.raises(RwError) as exc:
        paged(fake)
    assert exc.value.code == "internal"


def test_a_document_without_the_items_key_is_refused_not_read_as_empty():
    with pytest.raises(RwError) as exc:
        paged(pages({"Other": [], "NextToken": None}))
    assert exc.value.code == "internal"


def test_a_null_list_is_an_empty_page():
    assert paged(pages({"Functions": None, "NextToken": None})) == ([], False)


def test_a_failure_on_a_later_page_raises_instead_of_returning_the_first_page():
    fake = FakeAws()
    fake.handlers.append(
        {
            "words": ["lambda"],
            "region": None,
            "answers": [
                (0, json.dumps({"Functions": ["a"], "NextToken": "T1"}), ""),
                (254, "", err("ThrottlingException", retries=2)),
            ],
        }
    )
    with pytest.raises(RwError) as exc:
        paged(fake)
    assert exc.value.code == "rate_limited"


def test_durations_parse_and_a_bad_one_is_invalid_input():
    assert awscli.parse_duration("1h30m") == 5400
    assert awscli.parse_duration("500ms") == 0.5
    for bad in ("", "0s", "1x", "h", "-1h", "1h garbage"):
        with pytest.raises(RwError) as exc:
            awscli.parse_duration(bad, "since")
        assert exc.value.code == "invalid_input"


# every classified failure in the stub scenarios maps to the D64 code its scenario is named for
EXPECTED = {
    ("forbidden", "UnauthorizedOperation"): "permission_denied",
    ("forbidden", "AccessDeniedException"): "permission_denied",
    ("not-found", "ResourceNotFoundException"): "not_found",
    ("no-log-group", "ResourceNotFoundException"): "not_found",
    ("logs-forbidden", "AccessDeniedException"): "permission_denied",
    ("throttled", "TooManyRequestsException"): "rate_limited",
    ("throttled", "Throttling"): "rate_limited",
    ("throttled", "ThrottlingException"): "rate_limited",
    ("expired", "ExpiredToken"): "permission_denied",
    ("expired", "ExpiredTokenException"): "permission_denied",
    ("partial", "AccessDeniedException"): "permission_denied",
    ("ec2-denied", "UnauthorizedOperation"): "permission_denied",
    ("ec2-forbidden", "UnauthorizedOperation"): "permission_denied",
    ("ec2-metrics-forbidden", "AccessDenied"): "permission_denied",
    ("ec2-not-found", "InvalidInstanceID.NotFound"): "not_found",
    ("ec2-throttled", "RequestLimitExceeded"): "rate_limited",
    ("ec2-throttled", "Throttling"): "rate_limited",
}


def test_every_failure_in_the_stub_scenarios_is_classified_from_its_error_code():
    from pathlib import Path

    scenarios = Path(__file__).parents[1] / "stub" / "scenarios"
    found = set()
    for directory in sorted(p for p in scenarios.iterdir() if p.is_dir() and p.name != "_shared"):
        for call in json.loads((directory / "calls.yaml").read_text())["calls"]:
            if call["rc"]:
                code = error_code(call["stderr"])
                found.add((directory.name, code))
                assert (
                    classify_error(call["stderr"], call["rc"]) == EXPECTED[(directory.name, code)]
                )
    assert found == set(EXPECTED)
