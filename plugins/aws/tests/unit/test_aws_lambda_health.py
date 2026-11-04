"""aws-lambda-health: function-config, function-metrics and function-errors through their
scripts, with aws answered by a fake and by the CLI stub. The classic bundle's false passes are
the point: a failed call, a missing function or log group, and a truncated log list must not read
as a healthy function."""

import json
from datetime import UTC, datetime

import jsonschema
import pytest
import yaml
from aws_lambda_health import logs as lg
from aws_lambda_health import metrics as mx
from aws_lambda_health.config import CONFIG_QUERY, PROVISIONED_QUERY
from aws_lambda_health.logtext import MAX_TEXT_CHARS, clip, redact
from aws_lambda_health.target import lambda_target
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


def concurrency(fake, reserved=None, provisioned=(), then=None):
    """Answers the two concurrency reads of function-config (the CLI's output after --query)."""
    fake.on(
        "lambda",
        "get-function-concurrency",
        body={} if reserved is None else {"ReservedConcurrentExecutions": reserved},
    )
    fake.on(
        "lambda",
        "list-provisioned-concurrency-configs",
        body={"ProvisionedConcurrencyConfigs": list(provisioned), "NextToken": None},
    )


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
        "ConcurrentExecutions": points(
            "ConcurrentExecutions", [{"Maximum": 7.0}, {"Maximum": 12.0}]
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


ARN = f"arn:aws:lambda:{REGION}:{ACCT}:function:orders-api"


def run(run_script, script, **kw):
    return run_script(
        "aws-lambda-health", script, resource=kw.pop("resource", FUNCTION), now=NOW, **kw
    )


# --- function-config ---------------------------------------------------------------------------------


def test_function_config_reports_what_lambda_says_as_plain_facts(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    concurrency(fake_aws)
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
    # the configuration narrowed, then the two concurrency reads: every call by the function's full
    # ARN (built from the target's account, region and name) and for the function's own region
    assert fake_aws.words() == [
        ["lambda", "get-function-configuration", "--function-name", ARN, "--query", CONFIG_QUERY],
        ["lambda", "get-function-concurrency", "--function-name", ARN],
        [
            "lambda",
            "list-provisioned-concurrency-configs",
            "--function-name",
            ARN,
            "--query",
            PROVISIONED_QUERY,
            "--max-items",
            "50",
        ],
    ]
    assert {c["region"] for c in fake_aws.calls} == {REGION}


def test_function_config_reports_a_broken_function(run_script, fake_aws):
    concurrency(fake_aws)
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
    concurrency(fake_aws)
    shown = json.dumps(run(run_script, "function_config").envelope)
    assert "hunter2" not in shown and "DB_PASSWORD" not in shown


def test_function_config_cuts_long_reasons_to_1024_and_scrubs_them(run_script, fake_aws):
    reason = "password=hunter2 " + "x" * 3000
    fake_aws.on("lambda", "get-function-configuration", body=config(StateReason=reason))
    concurrency(fake_aws)
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
    concurrency(fake_aws)
    resource = {k: v for k, v in FUNCTION.items() if k not in ("region", "account")}
    assert run(run_script, "function_config", resource=resource).envelope["status"] == "ok"
    assert fake_aws.calls[0]["region"] == REGION
    assert fake_aws.words()[0][3] == ARN  # the account too comes from the path


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
    assert m["datapoints"] == {
        "Invocations": 2, "Errors": 2, "Throttles": 1, "Duration": 2, "ConcurrentExecutions": 2,
    }  # fmt: skip
    assert (m["hasData"], m["idle"], m["concurrentExecutionsMax"]) == (True, False, 12)
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
    assert len(calls) == 5
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
    assert stats["ConcurrentExecutions"][0] == "Maximum"


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
    assert (m["hasData"], m["idle"]) == (True, False)


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


EMPTY = (0, json.dumps({"Label": "x", "Datapoints": []}), "")
ALL_EMPTY = {
    m: EMPTY for m in ("Invocations", "Errors", "Throttles", "Duration", "ConcurrentExecutions")
}


def test_a_window_with_no_invocation_datapoints_is_idle_and_says_nothing_about_errors(
    run_script, fake_aws
):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws, **ALL_EMPTY)
    m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert m["datapoints"] == dict.fromkeys(
        ("Invocations", "Errors", "Throttles", "Duration", "ConcurrentExecutions"), 0
    )
    assert (m["invocations"], m["hasData"], m["idle"]) == (0, False, True)
    # no data is not "no errors": every number and every verdict that needs data is null
    assert (m["errors"], m["throttles"], m["errorRate"]) == (None, None, None)
    assert (m["errorRateAboveThreshold"], m["throttlesAboveThreshold"]) == (None, None)
    assert m["durationNearTimeout"] is None and m["duration"]["maxMs"] is None
    assert m["concurrentExecutionsMax"] is None


def test_a_metric_without_datapoints_counts_as_zero_only_when_invocations_has_datapoints(
    run_script, fake_aws
):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    # the function ran (Invocations has data); Lambda published no Errors or Throttles series
    metrics_for(fake_aws, Errors=EMPTY, Throttles=EMPTY)
    m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert (m["hasData"], m["idle"], m["errors"], m["throttles"]) == (True, False, 0, 0)
    assert (m["errorRateAboveThreshold"], m["throttlesAboveThreshold"]) == (False, False)


def test_a_function_throttled_completely_reports_its_throttles_though_it_has_no_invocations(
    run_script, fake_aws
):
    # throttled calls are not invocations: reserved concurrency 0 leaves Throttles and no Invocations
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(
        fake_aws,
        **{**ALL_EMPTY, "Throttles": (0, json.dumps(points("Throttles", [{"Sum": 40.0}])), "")},
    )
    m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
    assert (m["hasData"], m["idle"], m["throttles"], m["throttlesAboveThreshold"]) == (
        False, True, 40, True,
    )  # fmt: skip
    assert m["errors"] is None and m["errorRateAboveThreshold"] is None


def test_when_the_invocations_call_fails_nothing_is_claimed_about_data_or_idleness(
    run_script, fake_aws
):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    slow = (254, "", err("Throttling", "GetMetricStatistics", "Rate exceeded", retries=2))
    metrics_for(fake_aws, Invocations=slow, Throttles=EMPTY)
    run_ = run(run_script, "function_metrics")
    m = run_.envelope["outputs"]["metrics"]
    assert (m["invocations"], m["hasData"], m["idle"]) == (None, None, None)
    assert m["throttles"] is None and m["errorRate"] is None  # Throttles is empty and unproven
    assert m["errors"] == 0  # Errors has its own datapoints


def test_the_error_rate_threshold_is_exclusive(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    for errors, expected in ((5.0, False), (6.0, True)):  # 5/100 = 0.05 is not above 0.05
        metrics_for(
            fake_aws,
            Invocations=(0, json.dumps(points("Invocations", [{"Sum": 100.0}])), ""),
            Errors=(0, json.dumps(points("Errors", [{"Sum": errors}])), ""),
        )
        m = run(run_script, "function_metrics").envelope["outputs"]["metrics"]
        assert m["errorRateAboveThreshold"] is expected
        fake_aws.handlers.clear()
        fake_aws.on("lambda", "get-function-configuration", body=config())


def test_the_metric_dimension_is_the_function_name_not_the_arn(run_script, fake_aws):
    # CloudWatch publishes AWS/Lambda under FunctionName=<name>; an ARN matches nothing
    fake_aws.on("lambda", "get-function-configuration", body=config())
    metrics_for(fake_aws)
    run(run_script, "function_metrics")
    assert fake_aws.words()[0][3] == ARN
    for words in [w for w in fake_aws.words() if w[0] == "cloudwatch"]:
        assert words[words.index("--dimensions") + 1] == "Name=FunctionName,Value=orders-api"


def test_concurrent_executions_is_its_own_scoped_failure(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    denied = (254, "", err("AccessDeniedException", "GetMetricStatistics", "not authorized"))
    metrics_for(fake_aws, ConcurrentExecutions=denied)
    env = run(run_script, "function_metrics").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("permission_denied", "ConcurrentExecutions")
    ]
    m = env["outputs"]["metrics"]
    assert m["concurrentExecutionsMax"] is None and m["datapoints"]["ConcurrentExecutions"] is None
    assert m["invocations"] == 400


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
    metrics_for(
        fake_aws,
        Invocations=denied, Errors=denied, Throttles=denied, Duration=denied,
        ConcurrentExecutions=denied,
    )  # fmt: skip
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
    concurrency(fake_aws)
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
        "lambda:GetFunctionConfiguration", "lambda:GetFunctionConcurrency",
        "lambda:ListProvisionedConcurrencyConfigs", "cloudwatch:GetMetricStatistics",
        "logs:FilterLogEvents",
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
    assert m["hasData"] is True and m["idle"] is False and m["concurrentExecutionsMax"] > 0
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
    # idle, and said so: no data is neither healthy nor an error count of 0
    assert (m["invocations"], m["hasData"], m["idle"], m["errors"]) == (0, False, True, None)
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


# --- the function ARN, M5 -------------------------------------------------------------------------------


@pytest.mark.parametrize("script", ["function_config", "function_metrics", "function_errors"])
def test_every_task_passes_the_full_arn_built_from_the_target_never_the_credentials_account(
    run_script, fake_aws, script
):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    concurrency(fake_aws)
    metrics_for(fake_aws)
    logs_answer(fake_aws, events(1))
    run(run_script, script)
    named = [w[w.index("--function-name") + 1] for w in fake_aws.words() if "--function-name" in w]
    assert named and set(named) == {ARN}
    assert not [w for w in fake_aws.words() if w[0] == "sts"]  # the target decides, not sts


@pytest.mark.parametrize(
    "region,partition",
    [
        ("eu-west-1", "aws"),
        ("us-gov-west-1", "aws-us-gov"),
        ("cn-north-1", "aws-cn"),
        ("us-iso-east-1", "aws-iso"),
        ("eusc-de-east-1", "aws-eusc"),
    ],
)
def test_the_arn_partition_follows_the_region(region, partition):
    resource = {**FUNCTION, "region": region}
    target = lambda_target(resource)
    assert target.arn == f"arn:{partition}:lambda:{region}:{ACCT}:function:orders-api"
    assert (target.region, target.name, target.account) == (region, "orders-api", ACCT)


@pytest.mark.parametrize("account", ["12345", "12345678901a", "", "1234567890123"])
def test_a_target_without_a_12_digit_account_is_invalid_input_before_any_call(
    run_script, fake_aws, account
):
    resource = {**FUNCTION, "account": account, "path": "aws/functions/orders-api"}
    env = run(run_script, "function_config", resource=resource).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


# --- concurrency, minor 7 ------------------------------------------------------------------------------


def provisioned(qualifier="live", **kw):
    return {
        "FunctionArn": f"{ARN}:{qualifier}",
        "Requested": 10,
        "Available": 10,
        "Allocated": 10,
        "Status": "READY",
        "StatusReason": None,
        **kw,
    }


def test_function_config_reports_reserved_and_provisioned_concurrency(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    concurrency(
        fake_aws,
        reserved=0,
        provisioned=[
            provisioned("live"),
            provisioned("v7", Available=0, Allocated=0, Status="FAILED", StatusReason="x" * 3000),
        ],
    )
    fn = run(run_script, "function_config").envelope["outputs"]["function"]
    # a reserved concurrency of 0 throttles every call: configured, and zero
    assert fn["reservedConcurrency"] == {"configured": True, "executions": 0}
    pc = fn["provisionedConcurrency"]
    assert (pc["count"], pc["truncated"]) == (2, False)
    assert pc["configs"][0] == {
        "qualifier": "live", "requested": 10, "allocated": 10, "available": 10,
        "status": "READY", "statusReason": None,
    }  # fmt: skip
    assert pc["configs"][1]["status"] == "FAILED" and len(pc["configs"][1]["statusReason"]) == 1024


def test_no_reserved_concurrency_is_configured_false_not_null(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    concurrency(fake_aws)  # the CLI prints {} for a function with none
    fn = run(run_script, "function_config").envelope["outputs"]["function"]
    assert fn["reservedConcurrency"] == {"configured": False, "executions": None}
    assert fn["provisionedConcurrency"] == {"configs": [], "count": 0, "truncated": False}


def test_the_cli_prints_nothing_for_a_function_with_no_reserved_concurrency(run_script, fake_aws):
    # verified against the pinned CLI (tests/stub/verify): an empty response has an empty stdout
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on("lambda", "get-function-concurrency", stdout="")
    fake_aws.on(
        "lambda",
        "list-provisioned-concurrency-configs",
        body={"ProvisionedConcurrencyConfigs": [], "NextToken": None},
    )
    fn = run(run_script, "function_config").envelope["outputs"]["function"]
    assert fn["reservedConcurrency"] == {"configured": False, "executions": None}


def test_a_forbidden_concurrency_read_is_a_scoped_error_and_the_configuration_stands(
    run_script, fake_aws
):
    denied = err("AccessDeniedException", "GetFunctionConcurrency", "not authorized")
    throttled = err("TooManyRequestsException", "ListProvisionedConcurrencyConfigs", "x", retries=2)
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on("lambda", "get-function-concurrency", rc=254, stderr=denied)
    fake_aws.on("lambda", "list-provisioned-concurrency-configs", rc=254, stderr=throttled)
    env = run(run_script, "function_config").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("permission_denied", "reservedConcurrency"),
        ("rate_limited", "provisionedConcurrency"),
    ]
    fn = env["outputs"]["function"]
    assert fn["reservedConcurrency"] is None and fn["provisionedConcurrency"] is None  # not read
    assert fn["runtime"] == "python3.12"


def test_provisioned_concurrency_is_cut_at_50_and_says_so(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on("lambda", "get-function-concurrency", body={})
    fake_aws.on(
        "lambda",
        "list-provisioned-concurrency-configs",
        body={
            "ProvisionedConcurrencyConfigs": [provisioned(f"v{i}") for i in range(50)],
            "NextToken": "more",
        },
    )
    pc = run(run_script, "function_config").envelope["outputs"]["function"][
        "provisionedConcurrency"
    ]
    assert (pc["count"], pc["truncated"]) == (50, True)


# --- function-errors: a timed out page, a leading dash, minor 6 -----------------------------------------


def test_a_page_that_times_out_keeps_the_matches_already_found(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    page = json.dumps({"events": events(3), "NextToken": "t1"})
    fake_aws.on(*LOGS, stdout=page, then=[("timeout", "", "")])
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("timeout", "logs")]
    assert "3 matching events" in env["errors"][0]["message"]
    logs = env["outputs"]["logs"]
    assert (logs["matched"], logs["truncated"], logs["logGroupFound"]) == (3, True, True)


def test_a_first_page_that_times_out_has_nothing_to_keep_and_fails_the_task(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config())
    fake_aws.on(*LOGS, rc="timeout")
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["timeout"]


def test_a_page_that_is_throttled_after_matches_still_fails_the_task(run_script, fake_aws):
    # only a timeout keeps the partial list; a throttle is retried by the next run
    fake_aws.on("lambda", "get-function-configuration", body=config())
    page = json.dumps({"events": events(3), "NextToken": "t1"})
    fake_aws.on(*LOGS, stdout=page, then=[(254, "", THROTTLED)])
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["rate_limited"]


@pytest.mark.parametrize("pattern", ["-ERROR", "--profile", "-"])
def test_a_pattern_that_starts_with_a_dash_is_invalid_input_before_any_call(
    run_script, fake_aws, pattern
):
    env = run(run_script, "function_errors", settings={"errorPattern": pattern}).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert "'-'" in env["errors"][0]["message"] and fake_aws.calls == []


def test_a_log_group_that_starts_with_a_dash_is_refused_before_the_logs_call(run_script, fake_aws):
    fake_aws.on("lambda", "get-function-configuration", body=config(LogGroup="-x"))
    env = run(run_script, "function_errors").envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert [w[0] for w in fake_aws.words()] == ["lambda"]


# --- the scrub, M4: one test per shape --------------------------------------------------------------------

SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text,gone",
    [
        # quotes around keys and values: a Python dict repr and JSON with a spaced colon
        ("{'password': 'hunter2'}", "hunter2"),
        ("""{'api_key':'sk-123456'}""", "sk-123456"),
        ('{"token" : "abcdef123"}', "abcdef123"),
        ("""{'aws_secret_access_key': 'wJalrXUtnFEMI/K7MDENG'}""", "wJalrXUtnFEMI"),
        ("""{"AWS_SESSION_TOKEN": "IQoJb3JpZ2luX2VjEXAMPLE"}""", "IQoJb3JpZ2luX2VjEXAMPLE"),
        ("""password = 'hunter2'""", "hunter2"),
    ],
)
def test_redact_handles_quoted_keys_and_values(text, gone):
    assert gone not in redact(text) and "[REDACTED]" in redact(text)


def test_redact_scrubs_a_connection_string_with_a_password():
    out = redact("connect to postgres://admin:s3cr3tPw@db.internal:5432/orders failed")
    assert "s3cr3tPw" not in out and "admin" not in out
    assert out == "connect to postgres://[REDACTED]@db.internal:5432/orders failed"
    # no password, no userinfo: a plain URL is untouched
    assert (
        redact("GET https://api.example.com:8443/v1/x") == "GET https://api.example.com:8443/v1/x"
    )


def test_redact_scrubs_a_jwt():
    jwt = (
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEifQ."
        "eyJzdWIiOiJzeXN0ZW06c2VydmljZWFjY291bnQ6ZGVmYXVsdDpyZWFkZXIifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = redact(f"id token {jwt} rejected")
    assert "eyJ" not in out and out == "id token [REDACTED] rejected"


@pytest.mark.parametrize(
    "text,gone",
    [
        (
            "Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261104/eu-west-1/"
            "lambda/aws4_request, SignedHeaders=host;x-amz-date, "
            "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7",
            "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7",
        ),
        (
            "GET https://b.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature="
            "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7&X-Amz-Expires=60",
            "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7",
        ),
    ],
)
def test_redact_scrubs_a_sigv4_signature(text, gone):
    out = redact(text)
    assert gone not in out and "Signature=[REDACTED]" in out
    assert "X-Amz-Expires=60" in out or "SignedHeaders=host;x-amz-date" in out  # the rest stays


@pytest.mark.parametrize(
    "text",
    [
        f"aws_secret_access_key {SECRET_KEY}",  # no = or : between the name and the value
        f"aws secret: {SECRET_KEY} (rotated)",
        f"AWS_SECRET -> {SECRET_KEY}",
        f"AKIAIOSFODNN7EXAMPLE, {SECRET_KEY}",  # a CSV row of an access key
        f"AKIAIOSFODNN7EXAMPLE {SECRET_KEY}",
    ],
)
def test_redact_scrubs_a_bare_40_character_secret_key_beside_its_name_or_key_id(text):
    out = redact(text)
    assert SECRET_KEY not in out and "wJalr" not in out and "[REDACTED]" in out


def test_redact_leaves_a_40_character_value_that_is_not_beside_a_secret_name_alone():
    sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # a git sha
    assert redact(f"deployed commit {sha}") == f"deployed commit {sha}"


def test_redact_scrubs_a_printed_python_environ():
    line = (
        "ctx = environ({'PATH': '/usr/bin', 'AWS_ACCESS_KEY_ID': 'ASIAIOSFODNN7EXAMPLE', "
        "'DB_URL': 'x', 'LAMBDA_TASK_ROOT': '/var/task'}) in handler"
    )
    out = redact(line)
    assert out == "ctx = environ({[REDACTED]}) in handler"
    # a line cut before its closing "})" is redacted to the end of the line
    cut = redact("environ({'PATH': '/usr/bin', 'AWS_SECRET_ACCESS_KEY': 'abc\nnext line")
    assert cut.startswith("environ({[REDACTED]})") and "abc" not in cut
    assert cut.endswith("next line")
