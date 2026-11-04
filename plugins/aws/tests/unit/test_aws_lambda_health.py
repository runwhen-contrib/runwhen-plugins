"""aws-lambda-health: function-config, function-metrics and function-errors through their
scripts, with aws answered by a fake and by the CLI stub. The classic bundle's false passes are
the point: a failed call, a missing function or log group, and a truncated log list must not read
as a healthy function."""

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from aws_lambda_health import logs as lg
from aws_lambda_health import metrics as mx
from aws_lambda_health.config import CONFIG_QUERY
from aws_lambda_health.logtext import MAX_TEXT_CHARS, clip, redact
from conftest import FUNCTION, PLUGIN, iam_action

SKILL = PLUGIN / "skills" / "aws-lambda-health"
NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
ACCT = "123456789012"
REGION = "eu-west-1"


def err(code, op, message="boom", retries=None):
    tail = f" (reached max retries: {retries})" if retries is not None else ""
    return f"\naws: [ERROR]: An error occurred ({code}) when calling the {op} operation{tail}: {message}\n"


DENIED = err(
    "AccessDeniedException",
    "GetFunctionConfiguration",
    "User: x is not authorized to perform: lambda:GetFunctionConfiguration",
)
NOT_FOUND = err(
    "ResourceNotFoundException",
    "GetFunctionConfiguration",
    "Function not found: arn:aws:lambda:eu-west-1:123456789012:function:orders-api",
)
THROTTLED = err("ThrottlingException", "FilterLogEvents", "Rate exceeded", retries=2)
NO_GROUP = err(
    "ResourceNotFoundException", "FilterLogEvents", "The specified log group does not exist."
)


def config(**kw):
    base = {
        "FunctionName": "orders-api",
        "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCT}:function:orders-api",
        "Runtime": "python3.12",
        "PackageType": "Zip",
        "Handler": "app.handler",
        "MemorySize": 256,
        "Timeout": 30,
        "EphemeralStorageMb": 512,
        "Architectures": ["arm64"],
        "State": "Active",
        "StateReasonCode": None,
        "StateReason": None,
        "LastUpdateStatus": "Successful",
        "LastUpdateStatusReasonCode": None,
        "LastUpdateStatusReason": None,
        "LastModified": "2026-10-28T09:14:03.000+0000",
        "CodeSize": 4821337,
        "Version": "$LATEST",
        "LayerCount": 0,
        "SubnetCount": 0,
        "SecurityGroupCount": 0,
        "DeadLetterTargetArn": None,
        "TracingMode": "PassThrough",
        "LogGroup": None,
        "LogFormat": "Text",
        "EnvironmentErrorCode": None,
        "EnvironmentErrorMessage": None,
    }
    return {**base, **kw}


def points(metric, rows):
    return {
        "Label": metric,
        "Datapoints": [{"Timestamp": "2026-11-04T11:05:00+00:00", **row} for row in rows],
    }


def metrics_for(fake, region=REGION, name="orders-api", **overrides):
    """Answers the four get-metric-statistics calls; overrides: Metric=(rc, stdout, stderr)."""
    bodies = {
        "Invocations": points("Invocations", [{"Sum": 100.0}, {"Sum": 300.0}]),
        "Errors": points("Errors", [{"Sum": 0.0}, {"Sum": 0.0}]),
        "Throttles": points("Throttles", [{"Sum": 0.0}]),
        "Duration": points(
            "Duration",
            [
                {"SampleCount": 100.0, "Average": 100.0, "Maximum": 500.0},
                {"SampleCount": 300.0, "Average": 200.0, "Maximum": 900.0},
            ],
        ),
    }
    for metric, body in bodies.items():
        rc, out, stderr = overrides.get(metric, (0, json.dumps(body), ""))
        fake.on(
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            "AWS/Lambda",
            "--metric-name",
            metric,
            region=region,
            rc=rc,
            stdout=out,
            stderr=stderr,
        )


def events(n, message="[ERROR] boom {i}", start=1730678400000):
    return [
        {
            "timestamp": start + i * 1000,
            "logStreamName": "2026/11/04/[$LATEST]abc",
            "message": message.format(i=i),
        }
        for i in range(n)
    ]


def run(run_script, script, **kw):
    return run_script(
        "aws-lambda-health", script, resource=kw.pop("resource", FUNCTION), now=NOW, **kw
    )


# --- function-config ---------------------------------------------------------------------------------


def test_function_config_reports_what_lambda_says_as_plain_facts(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    out = run(run_script, "function_config").envelope
    assert out["status"] == "ok" and out["errors"] == []
    fn = out["outputs"]["function"]
    assert (fn["name"], fn["region"], fn["runtime"], fn["state"], fn["lastUpdateStatus"]) == (
        "orders-api", REGION, "python3.12", "Active", "Successful",
    )  # fmt: skip
    assert (
        fn["vpc"] is None
        and fn["environmentError"] is None
        and fn["logGroup"] == "/aws/lambda/orders-api"
    )
    assert fn["memoryMb"] == 256 and fn["timeoutSeconds"] == 30 and fn["architectures"] == ["arm64"]
    # one call, narrowed, for the function's own region
    assert fake_aws.words() == [
        [
            "lambda",
            "get-function-configuration",
            "--function-name",
            "orders-api",
            "--query",
            CONFIG_QUERY,
        ]
    ]
    assert fake_aws.calls[0]["region"] == REGION


def test_function_config_reports_a_broken_function(run_script, fake_aws):
    fake_aws.on(
        "lambda",
        "get-function-configuration",
        body=config(
            Runtime="python3.8",
            State="Failed",
            StateReasonCode="InsufficientRolePermissions",
            StateReason="The role defined for the function cannot be assumed by Lambda.",
            LastUpdateStatus="Failed",
            LastUpdateStatusReasonCode="EniLimitExceeded",
            SubnetCount=2,
            SecurityGroupCount=1,
            LogGroup="/custom/orders",
            EnvironmentErrorCode="AccessDeniedException",
            EnvironmentErrorMessage="Lambda was unable to decrypt the environment variables because KMS access was denied.",
        ),
    )
    fn = run(run_script, "function_config").envelope["outputs"]["function"]
    assert (fn["runtime"], fn["state"], fn["stateReasonCode"]) == (
        "python3.8",
        "Failed",
        "InsufficientRolePermissions",
    )
    assert (
        fn["lastUpdateStatus"] == "Failed"
        and fn["lastUpdateStatusReasonCode"] == "EniLimitExceeded"
    )
    assert fn["vpc"] == {"subnetCount": 2, "securityGroupCount": 1}
    assert fn["logGroup"] == "/custom/orders"
    assert fn["environmentError"]["code"] == "AccessDeniedException"


def test_function_config_never_asks_for_or_returns_environment_variables(run_script, fake_aws):
    assert "Variables" not in CONFIG_QUERY and "Environment.Variables" not in CONFIG_QUERY
    # even if the CLI answered with more than the projection, only the projected fields are read
    leaky = config()
    leaky["Environment"] = {"Variables": {"DB_PASSWORD": "hunter2"}}
    fake_aws.on("lambda", "get-function-configuration", body=leaky)
    shown = json.dumps(run(run_script, "function_config").envelope)
    assert "hunter2" not in shown and "DB_PASSWORD" not in shown


def test_function_config_cuts_long_reasons_to_1024_and_scrubs_them(run_script, fake_aws):
    reason = "password=hunter2 " + "x" * 3000
    fake_aws.on("lambda", "get-function-configuration", body=config(StateReason=reason))
    fn = run(run_script, "function_config").envelope["outputs"]["function"]
    assert len(fn["stateReason"]) == 1024 and fn["stateReason"].endswith("...")
    assert "hunter2" not in fn["stateReason"] and fn["textTruncated"] is True


@pytest.mark.parametrize(
    "stderr,code",
    [
        (NOT_FOUND, "not_found"),
        (DENIED, "permission_denied"),
        (
            err(
                "ExpiredTokenException",
                "GetFunctionConfiguration",
                "The security token included in the request is expired",
            ),
            "permission_denied",
        ),
        (
            err("TooManyRequestsException", "GetFunctionConfiguration", "Rate exceeded", retries=2),
            "rate_limited",
        ),
        (
            '\naws: [ERROR]: Could not connect to the endpoint URL: "https://lambda.eu-west-1.amazonaws.com/x"\n',
            "unreachable",
        ),
    ],
)
def test_a_failed_read_fails_the_task_with_the_classified_code_never_an_empty_function(
    run_script, fake_aws, stderr, code
):
    fake_aws.on("lambda", "get-function-configuration", rc=254, stderr=stderr)
    env = run(run_script, "function_config").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == [code]
    assert env["outputs"] == {}


def test_a_response_without_a_function_is_internal(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", stdout="null")
    env = run(run_script, "function_config").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["internal"]


@pytest.mark.parametrize(
    "resource",
    [
        None,
        {"type": "aws/region", "name": "eu-west-1", "path": "aws/accounts/1/regions/eu-west-1"},
        {**FUNCTION, "name": "bad name"},
        {**FUNCTION, "name": "a;b"},
        {**FUNCTION, "name": "x" * 65},
        {**FUNCTION, "region": "Europe"},
        {**FUNCTION, "region": "eu-west-1\n--profile x"},
    ],
)
def test_a_target_that_is_not_a_lambda_function_is_invalid_input_before_any_call(
    run_script, fake_aws, resource
):
    env = run(run_script, "function_config", resource=resource).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


def test_the_region_comes_from_the_path_when_the_resource_has_no_region_field(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    resource = {k: v for k, v in FUNCTION.items() if k != "region"}
    assert run(run_script, "function_config", resource=resource).envelope["status"] == "ok"
    assert fake_aws.calls[0]["region"] == REGION


# --- function-metrics --------------------------------------------------------------------------------


def test_function_metrics_sums_the_datapoints_and_weights_the_average(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws)
    out = run(run_script, "function_metrics").envelope
    assert out["status"] == "ok" and out["errors"] == []
    m = out["outputs"]["metrics"]
    assert (m["invocations"], m["errors"], m["throttles"]) == (400, 0, 0)
    assert m["duration"] == {
        "averageMs": 175.0,
        "maxMs": 900.0,
        "sampleCount": 400,
    }  # (100*100+300*200)/400
    assert m["errorRate"] == 0.0 and m["timeoutSeconds"] == 30
    assert m["datapoints"] == {"Invocations": 2, "Errors": 2, "Throttles": 1, "Duration": 2}
    assert (
        m["errorRateAboveThreshold"],
        m["throttlesAboveThreshold"],
        m["durationNearTimeout"],
    ) == (False, False, False)
    assert m["window"] == {
        "start": "2026-11-04T11:00:00Z",
        "end": "2026-11-04T12:00:00Z",
        "seconds": 3600,
    }
    assert m["periodSeconds"] == 300


def test_the_window_comes_from_the_runs_clock_and_the_since_input(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws)
    run(run_script, "function_metrics", since="6h")
    calls = [c["words"] for c in fake_aws.calls if c["words"][0] == "cloudwatch"]
    assert len(calls) == 4
    for words in calls:
        assert words[words.index("--start-time") + 1] == "2026-11-04T06:00:00Z"
        assert words[words.index("--end-time") + 1] == "2026-11-04T12:00:00Z"
        assert words[words.index("--dimensions") + 1] == "Name=FunctionName,Value=orders-api"
        assert words[words.index("--namespace") + 1] == "AWS/Lambda"
    stats = {
        w[w.index("--metric-name") + 1]: w[
            w.index("--statistics") + 1 : w.index("--statistics") + 4
        ]
        for w in calls
    }
    assert (
        stats["Duration"] == ["Average", "Maximum", "SampleCount"] and stats["Errors"][0] == "Sum"
    )


def test_a_long_window_uses_hourly_periods_to_stay_under_the_datapoint_limit():
    assert mx.period_for(3600) == 300 and mx.period_for(3 * 86400) == 300
    assert mx.period_for(3 * 86400 + 1) == 3600 and mx.period_for(14 * 86400) == 3600
    assert 14 * 86400 / 3600 <= 1440 and 3 * 86400 / 300 <= 1440


@pytest.mark.parametrize("since", ["15d", "0s", "soon", "-1h", ""])
def test_a_bad_since_is_invalid_input_before_any_call(run_script, fake_aws, since):
    env = run(run_script, "function_metrics", since=since).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


def test_function_metrics_reports_a_broken_function_and_the_threshold_booleans(
    run_script, fake_aws
):
    fake_aws.on("lambda", "get-function-configuration", body=config(Timeout=30))
    metrics_for(
        fake_aws,
        Invocations=(0, json.dumps(points("Invocations", [{"Sum": 1000.0}])), ""),
        Errors=(0, json.dumps(points("Errors", [{"Sum": 80.0}])), ""),
        Throttles=(0, json.dumps(points("Throttles", [{"Sum": 12.0}])), ""),
        Duration=(
            0,
            json.dumps(
                points("Duration", [{"SampleCount": 1000.0, "Average": 9000.0, "Maximum": 30000.0}])
            ),
            "",
        ),
    )
    m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert (m["errors"], m["errorRate"], m["throttles"]) == (80, 0.08, 12)
    assert (
        m["errorRateAboveThreshold"],
        m["throttlesAboveThreshold"],
        m["durationNearTimeout"],
    ) == (True, True, True)


def test_the_thresholds_are_settings(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config(Timeout=30))
    metrics_for(
        fake_aws,
        Errors=(0, json.dumps(points("Errors", [{"Sum": 8.0}])), ""),
        Throttles=(0, json.dumps(points("Throttles", [{"Sum": 2.0}])), ""),
        Duration=(
            0,
            json.dumps(
                points("Duration", [{"SampleCount": 400.0, "Average": 100.0, "Maximum": 20000.0}])
            ),
            "",
        ),
    )
    base = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert (
        base["errorRate"],
        base["errorRateAboveThreshold"],
        base["throttlesAboveThreshold"],
        base["durationNearTimeout"],
    ) == (0.02, False, True, False)
    strict = run(
        run_script, "function_metrics",
        settings={"errorRateThreshold": 0.01, "throttleThreshold": 3, "durationTimeoutRatio": 0.5},
    ).envelope["outputs"]["metrics"]  # fmt: skip
    assert (
        strict["errorRateAboveThreshold"],
        strict["throttlesAboveThreshold"],
        strict["durationNearTimeout"],
    ) == (True, False, True)


def test_a_function_with_no_datapoints_is_not_reported_as_healthy_zero(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    empty = (0, json.dumps({"Label": "x", "Datapoints": []}), "")
    metrics_for(fake_aws, Invocations=empty, Errors=empty, Throttles=empty, Duration=empty)
    m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert m["datapoints"] == {"Invocations": 0, "Errors": 0, "Throttles": 0, "Duration": 0}
    assert m["invocations"] == 0 and m["errorRate"] is None and m["duration"]["maxMs"] is None
    assert m["errorRateAboveThreshold"] is False and m["durationNearTimeout"] is False


def test_a_missing_function_is_not_found_not_idle(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", rc=254, stderr=NOT_FOUND)
    env = run(run_script, "function_metrics").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["not_found"]
    assert fake_aws.words()[0][:2] == ["lambda", "get-function-configuration"]
    assert not [w for w in fake_aws.words() if w[0] == "cloudwatch"]  # no metric call to misread


def test_one_failed_metric_is_a_scoped_error_and_the_rest_stand(run_script, fake_aws):
    throttled = err("Throttling", "GetMetricStatistics", "Rate exceeded", retries=2)
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws, Errors=(254, "", throttled))
    run_ = run(run_script, "function_metrics")
    env = run_.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("rate_limited", "Errors")]
    m = env["outputs"]["metrics"]
    assert m["errors"] is None and m["errorRate"] is None and m["errorRateAboveThreshold"] is None
    assert m["datapoints"]["Errors"] is None and m["invocations"] == 400 and m["throttles"] == 0


def test_when_every_metric_call_fails_the_task_fails_with_the_first_error(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    denied = (
        254,
        "",
        err(
            "AccessDeniedException",
            "GetMetricStatistics",
            "not authorized to perform: cloudwatch:GetMetricStatistics",
        ),
    )
    metrics_for(fake_aws, Invocations=denied, Errors=denied, Throttles=denied, Duration=denied)
    env = run(run_script, "function_metrics").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["permission_denied"]
    assert env["outputs"] == {}


def test_a_metric_response_without_datapoints_is_internal_not_empty(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws, Invocations=(0, json.dumps({"Label": "Invocations"}), ""))
    env = run(run_script, "function_metrics").envelope
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("internal", "Invocations")
    ]


# --- function-errors ---------------------------------------------------------------------------------

LOGS = ["logs", "filter-log-events"]


def logs_answer(fake, evs, token=None, group="/aws/lambda/orders-api"):
    fake.on(*LOGS, "--log-group-name", group, body={"events": evs, "NextToken": token})


def test_function_errors_returns_the_matching_events_oldest_first(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    logs_answer(fake_aws, events(3))
    out = run(run_script, "function_errors").envelope
    assert out["status"] == "ok" and out["errors"] == []
    logs = out["outputs"]["logs"]
    assert logs["logGroup"] == "/aws/lambda/orders-api" and logs["logGroupFound"] is True
    assert logs["matched"] == 3 and logs["truncated"] is False
    assert [e["message"] for e in logs["events"]] == [
        "[ERROR] boom 0",
        "[ERROR] boom 1",
        "[ERROR] boom 2",
    ]
    assert logs["events"][0] == {
        "timestamp": "2024-11-04T00:00:00.000Z",
        "logStream": "2026/11/04/[$LATEST]abc",
        "message": "[ERROR] boom 0",
        "truncated": False,
    }
    assert logs["window"] == {"start": "2026-11-04T11:00:00Z", "end": "2026-11-04T12:00:00Z"}
    call = next(c["words"] for c in fake_aws.calls if c["words"][:2] == LOGS)
    assert call[call.index("--start-time") + 1] == str(
        int(datetime(2026, 11, 4, 11, tzinfo=UTC).timestamp() * 1000)
    )
    assert call[call.index("--end-time") + 1] == str(int(NOW.timestamp() * 1000))
    assert call[call.index("--max-items") + 1] == "50"
    assert (
        call[call.index("--filter-pattern") + 1]
        == lg.DEFAULT_PATTERN
        == '?ERROR ?Exception ?"Task timed out" ?"Runtime.ExitError"'
    )


def test_no_matching_events_is_an_empty_list_not_a_failure_to_look(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    logs_answer(fake_aws, [])
    logs = run(run_script, "function_errors").envelope["outputs"]["logs"]
    assert (
        logs["events"] == []
        and logs["matched"] == 0
        and logs["truncated"] is False
        and logs["logGroupFound"] is True
    )


def test_the_log_group_is_the_functions_configured_one(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config(LogGroup="/custom/orders"))
    logs_answer(fake_aws, events(1), group="/custom/orders")
    assert (
        run(run_script, "function_errors").envelope["outputs"]["logs"]["logGroup"]
        == "/custom/orders"
    )


def test_a_list_cut_at_max_log_events_says_truncated(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    logs_answer(fake_aws, events(5), token="eyJuZXh0VG9rZW4iOiAiNSJ9")
    out = run(run_script, "function_errors", settings={"maxLogEvents": 5}).envelope["outputs"][
        "logs"
    ]
    assert out["matched"] == 5 and out["truncated"] is True
    call = next(c["words"] for c in fake_aws.calls if c["words"][:2] == LOGS)
    assert call[call.index("--max-items") + 1] == "5"


def test_the_event_cap_is_also_bounded_by_max_items(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    logs_answer(fake_aws, events(2), token="more")
    out = run(
        run_script, "function_errors", settings={"maxLogEvents": 100, "maxItems": 2}
    ).envelope["outputs"]["logs"]
    assert out["matched"] == 2 and out["truncated"] is True


def test_the_logs_query_keeps_next_token_so_a_cut_list_cannot_read_as_complete():
    assert "NextToken: NextToken" in lg.LOGS_QUERY
    assert "ingestionTime" not in lg.LOGS_QUERY and "eventId" not in lg.LOGS_QUERY


def test_a_logs_answer_without_next_token_is_refused(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on(*LOGS, body=events(3))  # what --query 'events[]' prints: the token was dropped
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["internal"]


def test_log_text_is_scrubbed_and_cut_and_never_repeats_a_credential(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    nasty = [
        {
            "timestamp": 1730678400000,
            "logStreamName": "s",
            "message": "connect failed password=hunter2 key AKIAIOSFODNN7EXAMPLE token=abc123",
        },
        {
            "timestamp": 1730678401000,
            "logStreamName": "s",
            "message": "[ERROR] " + "y" * 5000 + " AKIAIOSFODNN7EXAMPLE",
        },
    ]
    logs_answer(fake_aws, nasty)
    run_ = run(run_script, "function_errors")
    logs = run_.envelope["outputs"]["logs"]
    first, second = logs["events"]
    assert (
        "hunter2" not in first["message"]
        and "AKIA" not in first["message"]
        and "abc123" not in first["message"]
    )
    assert first["message"].count("[REDACTED]") == 3 and first["truncated"] is False
    assert len(second["message"]) == MAX_TEXT_CHARS and second["truncated"] is True
    assert logs["messagesTruncated"] is True
    assert "hunter2" not in json.dumps(run_.envelope) and "AKIAIOSFODNN7EXAMPLE" not in json.dumps(
        run_.envelope
    )


def test_a_log_group_that_does_not_exist_is_a_scoped_not_found_and_the_run_is_partial(
    run_script, fake_aws
):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on(*LOGS, rc=254, stderr=NO_GROUP)
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("not_found", "logs")]
    logs = env["outputs"]["logs"]
    assert logs["logGroupFound"] is False and logs["events"] == [] and logs["truncated"] is False


def test_logs_the_credential_may_not_read_are_a_scoped_permission_error(run_script, fake_aws):
    denied = err(
        "AccessDeniedException",
        "FilterLogEvents",
        "User: x is not authorized to perform: logs:FilterLogEvents",
    )
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on(*LOGS, rc=254, stderr=denied)
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "logs")]
    assert env["outputs"]["logs"]["logGroupFound"] is None  # unknown, not "missing"


def test_throttled_logs_fail_the_task_instead_of_reading_as_no_errors(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on(*LOGS, rc=254, stderr=THROTTLED)
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["rate_limited"]
    assert env["outputs"] == {}


def test_a_missing_function_is_not_found_and_the_logs_are_never_asked(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", rc=254, stderr=NOT_FOUND)
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["not_found"]
    assert [w[0] for w in fake_aws.words()] == ["lambda"]


def test_the_pattern_is_a_setting_and_a_bad_one_is_invalid_input(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    logs_answer(fake_aws, [])
    run(run_script, "function_errors", settings={"errorPattern": "?FATAL"})
    call = next(c["words"] for c in fake_aws.calls if c["words"][:2] == LOGS)
    assert call[call.index("--filter-pattern") + 1] == "?FATAL"
    for bad in ("x" * 201, "a\nb"):
        fake_aws.calls.clear()
        env = run(run_script, "function_errors", settings={"errorPattern": bad}).envelope
        assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
        assert fake_aws.calls == []


# --- the scrub ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,gone",
    [
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("password=hunter2", "hunter2"),
        ('{"api_key": "sk-123456"}', "sk-123456"),
        ("token: abcdef", "abcdef"),
        ("AWS key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("temporary ASIAIOSFODNN7EXAMPLE used", "ASIAIOSFODNN7EXAMPLE"),
        ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("AWS_SESSION_TOKEN=IQoJb3JpZ2luX2VjEXAMPLE", "IQoJb3JpZ2luX2VjEXAMPLE"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
    ],
)
def test_redact_scrubs_the_credential_shapes_a_lambda_log_holds(text, gone):
    assert gone not in redact(text) and "[REDACTED]" in redact(text)


def test_redact_leaves_ordinary_log_text_alone_and_clip_reports_a_cut():
    line = "[ERROR] KeyError: 'orderId' in handler app.py:42"
    assert redact(line) == line and clip(line) == (line, False)
    text, cut = clip("z" * 2000)
    assert len(text) == MAX_TEXT_CHARS and text.endswith("...") and cut is True


# --- what the tasks declare -----------------------------------------------------------------------------


def tasks_doc():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def test_the_tasks_are_read_only_aws_tasks_with_the_credential_and_defaults():
    doc = tasks_doc()
    assert doc["runtime"] == "aws@1" and doc["defaults"]["credentials"] == "aws/sdk"
    assert doc["settings"]["maxItems"]["default"] == 200
    assert [t["name"] for t in doc["tasks"]] == [
        "function-config",
        "function-metrics",
        "function-errors",
    ]
    for task in doc["tasks"]:
        assert task["readOnly"] is True and (SKILL / task["script"]).is_file()
    for name in ("function-metrics", "function-errors"):
        task = next(t for t in doc["tasks"] if t["name"] == name)
        assert task["inputs"]["since"]["default"] == "1h"  # classic looked back 10 minutes


def run_all_tasks(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws)
    logs_answer(fake_aws, events(2))
    calls = {}
    for script in ("function_config", "function_metrics", "function_errors"):
        before = len(fake_aws.calls)
        assert run(run_script, script).envelope["status"] == "ok"
        calls[script] = fake_aws.calls[before:]
    return calls


def test_each_task_declares_exactly_the_iam_actions_its_calls_need(run_script, fake_aws):
    calls = run_all_tasks(run_script, fake_aws)
    for task in tasks_doc()["tasks"]:
        needed = {iam_action(c["words"]) for c in calls[task["name"].replace("-", "_")]} - {None}
        assert {p["action"] for p in task["permissions"]} == needed, task["name"]
    assert {a for c in calls.values() for a in {iam_action(x["words"]) for x in c}} == {
        "lambda:GetFunctionConfiguration", "cloudwatch:GetMetricStatistics", "logs:FilterLogEvents",
    }  # fmt: skip


def test_every_declared_permission_fits_the_permission_schema():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "aws-permission.json").read_text())
    for task in tasks_doc()["tasks"]:
        for permission in task["permissions"]:
            jsonschema.validate(permission, schema)


def test_outputs_fit_their_schemas_and_the_schemas_name_no_verdict(run_script, fake_aws):
    run_all_tasks(run_script, fake_aws)
    for task, script in (
        ("function-config", "function_config"),
        ("function-metrics", "function_metrics"),
        ("function-errors", "function_errors"),
    ):
        spec = next(t for t in tasks_doc()["tasks"] if t["name"] == task)
        out = run(run_script, script).envelope["outputs"]
        for name, decl in spec["outputs"].items():
            assert name not in {"status", "errors"}
            schema = json.loads((SKILL / decl["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert not {"severity", "title", "nextSteps"} & set(schema["properties"])
            jsonschema.validate(out[name], schema)


def test_lib_and_scripts_never_read_the_wall_clock():
    for path in [*(SKILL / "lib").rglob("*.py"), *(SKILL / "scripts").glob("*.py")]:
        text = path.read_text()
        for banned in ("datetime.now(", "time.time(", "datetime.utcnow(", "date.today("):
            assert banned not in text, f"{path.name} reads the wall clock ({banned}); use ctx.now()"


def test_no_script_or_lib_shells_out_or_hides_a_failure():
    for path in [*(SKILL / "lib").rglob("*.py"), *(SKILL / "scripts").glob("*.py")]:
        text = path.read_text()
        for banned in ("shell=True", "2>/dev/null", "|| true", "os.system"):
            assert banned not in text, f"{path.name}: {banned}"


# --- against the CLI stub, as `rwtask test --record` runs it ------------------------------------------------

EXPECTED = {
    # scenario: {script: (status, error codes)}
    "healthy": {
        "function_config": ("ok", []),
        "function_metrics": ("ok", []),
        "function_errors": ("ok", []),
    },
    "broken": {
        "function_config": ("ok", []),
        "function_metrics": ("ok", []),
        "function_errors": ("ok", []),
    },
    "forbidden": {
        "function_config": ("failed", ["permission_denied"]),
        "function_metrics": ("failed", ["permission_denied"]),
        "function_errors": ("failed", ["permission_denied"]),
    },
    "not-found": {},
}


@pytest.mark.parametrize("scenario", ["healthy", "broken", "forbidden"])
def test_the_tasks_against_the_stub(run_with_stub, scenario):
    run_with_stub.use(scenario)
    for script, (status, codes) in EXPECTED[scenario].items():
        run_ = run_with_stub("aws-lambda-health", script, resource=FUNCTION)
        assert run_.envelope["status"] == status, (scenario, script, run_.envelope["errors"])
        assert [e["code"] for e in run_.envelope["errors"]] == codes


def test_the_broken_scenario_shows_what_the_tasks_detect(run_with_stub):
    run_with_stub.use("broken")
    fn = run_with_stub("aws-lambda-health", "function_config", resource=FUNCTION).envelope[
        "outputs"
    ]["function"]
    assert (fn["runtime"], fn["lastUpdateStatus"], fn["lastUpdateStatusReasonCode"]) == (
        "python3.8",
        "Failed",
        "InsufficientRolePermissions",
    )
    assert fn["environmentError"]["code"] == "AccessDeniedException" and fn["vpc"] == {
        "subnetCount": 2,
        "securityGroupCount": 1,
    }
    m = run_with_stub("aws-lambda-health", "function_metrics", resource=FUNCTION).envelope[
        "outputs"
    ]["metrics"]
    assert (m["invocations"], m["errors"], m["throttles"]) == (5400, 410, 37)
    assert (
        m["errorRateAboveThreshold"] is True
        and m["throttlesAboveThreshold"] is True
        and m["durationNearTimeout"] is True
    )
    logs = run_with_stub("aws-lambda-health", "function_errors", resource=FUNCTION).envelope[
        "outputs"
    ]["logs"]
    assert logs["matched"] == 50 and logs["truncated"] is True and logs["messagesTruncated"] is True
    shown = json.dumps(logs)
    assert "hunter2" not in shown and "AKIAIOSFODNN7EXAMPLE" not in shown and "[REDACTED]" in shown


def test_the_stub_failure_scenarios(run_with_stub):
    resource = FUNCTION
    run_with_stub.use("not-found")
    for script in ("function_config", "function_metrics", "function_errors"):
        env = run_with_stub(
            "aws-lambda-health", script, resource={**resource, "name": "nope"}
        ).envelope
        assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["not_found"]
    run_with_stub.use("no-log-group")
    other = {**resource, "name": "report-batch"}
    env = run_with_stub("aws-lambda-health", "function_errors", resource=other).envelope
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("not_found", "logs")
    ]
    m = run_with_stub("aws-lambda-health", "function_metrics", resource=other).envelope["outputs"][
        "metrics"
    ]
    assert m["invocations"] == 0 and m["datapoints"]["Invocations"] == 0  # idle, and said so
    run_with_stub.use("logs-forbidden")
    env = run_with_stub("aws-lambda-health", "function_errors", resource=resource).envelope
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("permission_denied", "logs")
    ]
    run_with_stub.use("throttled")
    env = run_with_stub("aws-lambda-health", "function_metrics", resource=resource).envelope
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("rate_limited", "Errors")
    ]
    env = run_with_stub("aws-lambda-health", "function_errors", resource=resource).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["rate_limited"]
    run_with_stub.use("expired")
    env = run_with_stub("aws-lambda-health", "function_config", resource=resource).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["permission_denied"]
