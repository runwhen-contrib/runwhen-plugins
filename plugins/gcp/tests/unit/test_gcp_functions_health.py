"""gcp-functions-health: function-state, function-errors and function-metrics, run through the real
v13 context with gcloud and curl answered by the stub's canned scenarios (the data a fixture is
recorded from), plus per-test overrides for the cases a scenario does not cover."""

import json
from datetime import datetime, timezone

import jsonschema
import pytest
import yaml
from conftest import PLUGIN, bucket_target, function_target
from gcp_functions_health import fnlist, logs, metrics, monitoring
from gcp_functions_health import logtext as lt
from gcp_functions_health import target as tg
from gcp_functions_health.errors import RwError
from gcp_functions_health.gcloud import classify_error

SKILL = PLUGIN / "skills" / "gcp-functions-health"
P = "shop-prod-1234"
BILL = f"--billing-project={P}"
CANARIES = (
    "canary-db-pass",
    "canary-query-token",
    "canary-build-token",
    "canary-pass",
    "STRIPE_KEY",
    "stripe-key",
)
ORDERS = function_target("orders-api", region="us-central1")
BID = "a1b2c3d4-0000-4000-8000-{:012d}"


def describe_argv(name, region):
    return ["functions", "describe", name, f"--region={region}", f"--project={P}", BILL]


def build_argv(number, region):
    return ["builds", "describe", BID.format(number), f"--region={region}", f"--project={P}", BILL]


FLIST = ["functions", "list", f"--project={P}", BILL]
TOKEN = ["auth", "print-access-token"]


def logs_argv(flt, limit=51, freshness="3600s"):
    return [
        "logging",
        "read",
        flt,
        f"--project={P}",
        f"--freshness={freshness}",
        f"--limit={limit}",
        "--order=desc",
        BILL,
    ]


GEN2_FILTER = 'resource.type="cloud_run_revision" AND resource.labels.service_name="orders-api" AND resource.labels.location="us-central1" AND severity>=ERROR'
GEN1_FILTER = 'resource.type="cloud_function" AND resource.labels.function_name="nightly-report" AND resource.labels.region="us-east1" AND severity>=ERROR'


def run(run_script, task, stub, resource=ORDERS, **kwargs):
    return run_script(task, skill="gcp-functions-health", stub=stub, resource=resource, **kwargs)


def schema_of(name):
    return json.loads((SKILL / "schemas" / f"{name}.v1.json").read_text())


def check_schema(name, value):
    schema = schema_of(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(value, schema)


def assert_no_secrets(*things):
    text = json.dumps(things, default=str)
    for canary in CANARIES:
        assert canary not in text, canary


# ---- the target ---------------------------------------------------------------------------------


def test_the_target_is_the_path_project_and_name_with_an_optional_region():
    assert tg.function_target(ORDERS) == (P, "orders-api", "us-central1")
    assert tg.function_target(function_target("checkout")) == (P, "checkout", None)


@pytest.mark.parametrize(
    "resource",
    [
        bucket_target("shop-assets"),
        {"path": f"gcp/projects/{P}", "type": "gcp/project", "name": P},
        function_target("orders api"),
        function_target("-flag"),
        function_target("x" * 64),
        function_target("orders-api", region="US CENTRAL"),
        function_target("orders-api", project="Bad_Project"),
        {"path": f"gcp/projects/{P}/buckets/orders-api", "type": "gcp/cloud-function", "name": "x"},
        None,
    ],
)
@pytest.mark.parametrize("task", ["function_state", "function_errors", "function_metrics"])
def test_a_target_that_is_not_a_function_is_invalid_input_before_any_call(
    run_script, stub_run, task, resource
):
    stub = stub_run("functions-healthy")
    out = run(run_script, task, stub, resource=resource)
    assert out.envelope["status"] == "failed" and stub.log == []
    assert [e["code"] for e in out.envelope["errors"]] == ["invalid_input"]


# ---- function-state -----------------------------------------------------------------------------


def state(run_script, stub_run, name, region, scenario="functions-healthy", **kwargs):
    stub = stub_run(scenario)
    out = run(
        run_script, "function_state", stub, resource=function_target(name, region=region), **kwargs
    )
    return out, stub


def test_a_second_gen_function_is_reported_with_its_service_and_latest_build(run_script, stub_run):
    out, stub = state(run_script, stub_run, "orders-api", "us-central1")
    env = out.envelope
    assert env["status"] == "ok" and out.errors == []
    fn = env["outputs"]["function"]
    assert fn["generation"] == 2 and fn["state"] == "ACTIVE" and fn["region"] == "us-central1"
    assert fn["runService"] == "orders-api"  # the Cloud Run service behind the function
    assert fn["revision"] == "orders-api-00007-xaz" and fn["versionId"] is None
    assert (fn["runtime"], fn["entryPoint"], fn["timeoutSeconds"]) == ("python312", "handler", 60)
    assert (fn["memory"], fn["cpu"], fn["minInstances"], fn["maxInstances"]) == (
        "512Mi",
        "1",
        0,
        20,
    )
    assert fn["maxRequestConcurrency"] == 80 and fn["ingress"] == "ALLOW_ALL"
    assert fn["serviceAccount"] == f"orders@{P}.iam.gserviceaccount.com"
    assert fn["trigger"] == {
        "kind": "http",
        "eventType": None,
        "url": "https://orders-api-k3j2h1g0fa-uc.a.run.app",
    }
    assert fn["labels"] == {"deployment-tool": "console", "team": "web"}
    assert fn["stateMessages"] == [] and fn["stateMessageCount"] == 0
    assert fn["deployment"] == {"buildId": BID.format(1), "buildLocation": "us-central1"}
    assert fn["build"]["status"] == "SUCCESS" and fn["build"]["id"] == BID.format(1)
    assert fn["build"]["failureType"] is None and fn["build"]["finishTime"].startswith(
        "2026-10-30T14:02"
    )
    check_schema("function", fn)
    assert stub.asked() == [
        describe_argv("orders-api", "us-central1"),
        build_argv(1, "us-central1"),
    ]


def test_environment_variables_and_secret_references_are_counted_never_returned(
    run_script, stub_run
):
    out, stub = state(run_script, stub_run, "orders-api", "us-central1")
    fn = out.envelope["outputs"]["function"]
    assert fn["environmentVariableCount"] == 2 and fn["secretReferenceCount"] == 1
    # the canned describe holds a connection string with a password and a secret reference
    assert "canary-db-pass-7f3a" in (stub.directory / "describe-orders-api.json").read_text()
    assert_no_secrets(out.envelope)
    assert "ORDERS_DB_URL" not in json.dumps(out.envelope) and "LOG_EXECUTION_ID" not in json.dumps(
        out.envelope
    )


def test_an_event_triggered_second_gen_function_has_no_url_and_no_instance_cap(
    run_script, stub_run
):
    out, _ = state(run_script, stub_run, "thumbnailer", "europe-west1")
    fn = out.envelope["outputs"]["function"]
    assert fn["trigger"] == {
        "kind": "event",
        "eventType": "google.cloud.storage.object.v1.finalized",
        "url": None,
    }
    assert fn["maxInstances"] is None  # no cap set: null, not 0
    assert (
        fn["build"]["status"] == "SUCCESS" and fn["deployment"]["buildLocation"] == "europe-west1"
    )


def test_a_first_gen_function_is_read_from_its_own_format(run_script, stub_run):
    out, stub = state(run_script, stub_run, "legacy-cron", "us-east1")
    fn = out.envelope["outputs"]["function"]
    assert out.envelope["status"] == "ok"
    assert fn["generation"] == 1 and fn["state"] == "ACTIVE"  # 1st gen `status`
    assert fn["versionId"] == "12" and fn["revision"] is None and fn["runService"] is None
    assert (fn["timeoutSeconds"], fn["memory"], fn["maxInstances"]) == (60, "256M", 5)
    assert fn["trigger"] == {
        "kind": "event",
        "eventType": "google.pubsub.topic.publish",
        "url": None,
    }
    assert fn["serviceAccount"] == f"jobs@{P}.iam.gserviceaccount.com"
    assert fn["environmentVariableCount"] == 2 and fn["stateMessages"] == []
    assert fn["build"]["status"] == "SUCCESS" and fn["deployment"]["buildLocation"] == "us-east1"
    check_schema("function", fn)
    assert stub.asked()[1] == build_argv(100, "us-east1")
    assert_no_secrets(out.envelope)


def test_a_failed_second_gen_function_shows_its_messages_and_its_failed_build(run_script, stub_run):
    out, _ = state(run_script, stub_run, "checkout", "us-central1", scenario="functions-broken")
    fn = out.envelope["outputs"]["function"]
    assert (
        out.envelope["status"] == "ok"
    )  # the task read everything; the function is what is broken
    assert fn["state"] == "FAILED" and fn["stateMessageCount"] == 2
    assert [(m["severity"], m["type"]) for m in fn["stateMessages"]] == [
        ("ERROR", "BuildFailed"),
        ("WARNING", "CloudRunService"),
    ]
    assert "No module named 'stripe_client'" in fn["stateMessages"][0]["message"]
    build = fn["build"]
    assert build["status"] == "FAILURE" and build["failureType"] == "USER_BUILD_STEP"
    assert "was rejected" in build["failureDetail"] and "canary-build-token" not in json.dumps(
        build
    )  # scrubbed
    assert build["logUrl"].startswith("https://console.cloud.google.com/cloud-build/builds")
    assert "steps" not in build and "env" not in json.dumps(build)
    check_schema("function", fn)
    assert_no_secrets(out.envelope)


def test_an_offline_first_gen_function_and_its_failed_build(run_script, stub_run):
    out, _ = state(run_script, stub_run, "nightly-report", "us-east1", scenario="functions-broken")
    fn = out.envelope["outputs"]["function"]
    assert (fn["generation"], fn["state"], fn["versionId"]) == (1, "OFFLINE", "4")
    assert fn["trigger"]["kind"] == "http" and fn["trigger"]["url"].endswith("/nightly-report")
    assert fn["build"]["status"] == "FAILURE"


def test_the_function_stands_when_its_build_cannot_be_read(run_script, stub_run):
    out, _ = state(
        run_script, stub_run, "checkout", "us-central1", scenario="functions-build-denied"
    )
    env = out.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "build")]
    fn = env["outputs"]["function"]
    assert fn["state"] == "FAILED" and fn["build"] is None
    assert fn["deployment"] == {
        "buildId": BID.format(3),
        "buildLocation": "us-central1",
    }  # the build is named


def test_a_function_that_names_no_build_has_a_null_deployment_and_no_error(run_script, stub_run):
    stub = stub_run("functions-healthy")
    row = json.loads((stub.directory / "describe-orders-api.json").read_text())
    del row["buildConfig"]["build"]
    stub.set(describe_argv("orders-api", "us-central1"), stdout=json.dumps(row))
    out = run(run_script, "function_state", stub)
    fn = out.envelope["outputs"]["function"]
    assert out.envelope["status"] == "ok" and fn["deployment"] is None and fn["build"] is None
    assert stub.asked() == [describe_argv("orders-api", "us-central1")]


def test_a_row_with_no_state_is_null_never_active(run_script, stub_run):
    stub = stub_run("functions-healthy")
    row = json.loads((stub.directory / "describe-orders-api.json").read_text())
    del row["state"]
    stub.set(describe_argv("orders-api", "us-central1"), stdout=json.dumps(row))
    fn = run(run_script, "function_state", stub).envelope["outputs"]["function"]
    assert fn["state"] is None and fn["generation"] == 2


def test_state_messages_are_capped_and_each_message_is_scrubbed_and_cut(run_script, stub_run):
    stub = stub_run("functions-broken")
    row = json.loads((stub.directory / "describe-checkout.json").read_text())
    row["stateMessages"] = [
        {"severity": "ERROR", "type": "T", "message": "password=canary-pass-9 " + "y" * 2000}
        for _ in range(60)
    ]
    stub.set(describe_argv("checkout", "us-central1"), stdout=json.dumps(row))
    fn = run(
        run_script,
        "function_state",
        stub,
        resource=function_target("checkout", region="us-central1"),
    ).envelope["outputs"]["function"]
    assert len(fn["stateMessages"]) == 50 and fn["stateMessageCount"] == 60
    first = fn["stateMessages"][0]
    assert (
        len(first["message"]) == 1024
        and first["messageTruncated"]
        and "canary-pass" not in first["message"]
    )


# -- finding the region ----------------------------------------------------------------------------


def test_without_a_region_the_function_is_found_in_the_project_list(run_script, stub_run):
    stub = stub_run("functions-healthy")
    out = run(run_script, "function_state", stub, resource=function_target("legacy-cron"))
    fn = out.envelope["outputs"]["function"]
    assert out.envelope["status"] == "ok" and (fn["region"], fn["generation"]) == ("us-east1", 1)
    assert stub.asked() == [
        FLIST,
        build_argv(100, "us-east1"),
    ]  # the list row is the function: no describe


def test_an_id_in_two_regions_needs_the_region(run_script, stub_run):
    stub = stub_run("functions-split")
    out = run(run_script, "function_state", stub, resource=function_target("orders-api"))
    env = out.envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert (
        "europe-west1, us-central1" in env["errors"][0]["message"]
        and "region" in env["errors"][0]["message"]
    )
    # with the region it is the one describe
    stub = stub_run("functions-split")
    ok = run(
        run_script,
        "function_state",
        stub,
        resource=function_target("orders-api", region="europe-west1"),
    )
    assert (
        ok.envelope["status"] == "ok"
        and ok.envelope["outputs"]["function"]["region"] == "europe-west1"
    )


def test_a_function_missing_from_the_list_is_not_found_unless_regions_were_unreachable(
    run_script, stub_run
):
    out = run(
        run_script,
        "function_state",
        stub_run("functions-healthy"),
        resource=function_target("nope"),
    )
    assert [e["code"] for e in out.envelope["errors"]] == ["not_found"]
    out = run(
        run_script, "function_state", stub_run("functions-empty"), resource=function_target("nope")
    )
    assert [e["code"] for e in out.envelope["errors"]] == [
        "not_found"
    ]  # an empty project: not a pass
    out = run(
        run_script,
        "function_state",
        stub_run("functions-unreachable"),
        resource=function_target("nope"),
    )
    assert [e["code"] for e in out.envelope["errors"]] == ["unreachable"]
    assert "asia-east1" in out.envelope["errors"][0]["message"]
    found = run(
        run_script,
        "function_state",
        stub_run("functions-unreachable"),
        resource=function_target("orders-api"),
    )
    assert found.envelope["status"] == "ok"  # found in what was listed


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("functions-forbidden", "permission_denied"),
        ("functions-throttled", "rate_limited"),
        ("functions-api-disabled", "dependency"),
    ],
)
@pytest.mark.parametrize("region", ["us-central1", None])
def test_a_failed_read_fails_the_task_with_the_classified_code(
    run_script, stub_run, scenario, code, region
):
    out = run(
        run_script,
        "function_state",
        stub_run(scenario),
        resource=function_target("orders-api", region=region),
    )
    env = out.envelope
    assert env["status"] == "failed" and "function" not in env["outputs"]
    assert [e["code"] for e in env["errors"]] == [code]
    assert "ERROR: (gcloud.functions." in env["errors"][0]["message"]


def test_a_function_that_does_not_exist_in_the_region_is_not_found(run_script, stub_run):
    out = run(
        run_script,
        "function_state",
        stub_run("functions-not-found"),
        resource=function_target("nope", region="us-central1"),
    )
    assert out.envelope["status"] == "failed" and [e["code"] for e in out.envelope["errors"]] == [
        "not_found"
    ]


def test_every_call_is_billed_to_the_target_project_with_the_credential_env_neutralised(
    run_script, stub_run
):
    stub = stub_run("functions-healthy")
    run(run_script, "function_state", stub)
    assert all(c["timeout"] == 60.0 for c in stub.log)
    for call in stub.log:
        assert BILL in call["argv"] and call["argv"][-1] == "--quiet"
        assert (
            call["env"]["CLOUDSDK_AUTH_ACCESS_TOKEN"] == ""
            and call["env"]["CLOUDSDK_CORE_ACCOUNT"] == ""
        )


@pytest.mark.parametrize("bad", [0, 10001, True, "5"])
def test_a_bad_max_items_is_invalid_input_before_any_call(run_script, stub_run, bad):
    stub = stub_run("functions-healthy")
    out = run(run_script, "function_state", stub, settings={"maxItems": bad})
    assert out.envelope["errors"][0]["code"] == "invalid_input" and stub.log == []


# ---- function-errors ----------------------------------------------------------------------------


def errors_run(run_script, stub_run, scenario="functions-broken", resource=ORDERS, **kwargs):
    stub = stub_run(scenario)
    return run(run_script, "function_errors", stub, resource=resource, **kwargs), stub


def test_error_entries_come_newest_first_scrubbed_and_cut(run_script, stub_run):
    out, stub = errors_run(run_script, stub_run)
    env = out.envelope
    assert env["status"] == "ok" and out.errors == []
    result = env["outputs"]["logs"]
    check_schema("logs", result)
    assert result["filter"] == GEN2_FILTER and result["order"] == "newest-first"
    assert (result["matched"], result["truncated"], result["messagesTruncated"]) == (3, False, True)
    assert result["window"]["seconds"] == 3600 and result["window"]["end"].endswith("Z")
    first, second, third = result["entries"]
    assert [e["timestamp"] for e in result["entries"]] == sorted(
        (e["timestamp"] for e in result["entries"]), reverse=True
    )
    assert first["payload"] == "textPayload" and "APIConnectionError" in first["message"]
    assert first["revision"] == "orders-api-00007-xaz" and first["httpStatus"] is None
    assert second["payload"] == "jsonPayload" and second["httpStatus"] == 500
    assert second["messageTruncated"] and len(second["message"]) == 1024
    assert "[REDACTED]" in second["message"] and "canary-db-pass" not in second["message"]
    assert (third["httpStatus"], third["message"]) == (504, "upstream timeout after 60s")
    assert_no_secrets(env)
    # a structured payload is never dumped: the environment inside jsonPayload is not in the output
    assert "environment" not in json.dumps(env) and "ORDERS_DB_URL" not in json.dumps(env)
    assert stub.asked() == [describe_argv("orders-api", "us-central1"), logs_argv(GEN2_FILTER)]


def test_a_first_gen_function_is_read_from_the_cloud_function_resource(run_script, stub_run):
    out, stub = errors_run(
        run_script, stub_run, resource=function_target("nightly-report", region="us-east1")
    )
    result = out.envelope["outputs"]["logs"]
    assert result["filter"] == GEN1_FILTER
    assert result["entries"][0]["message"].startswith("Function execution took 60001 ms")
    assert (
        result["entries"][0]["executionId"] == "7f1k2l9x0p3q"
        and result["entries"][0]["revision"] is None
    )
    assert stub.asked()[1] == logs_argv(GEN1_FILTER)


def test_a_healthy_function_has_no_entries_and_says_only_that(run_script, stub_run):
    out, _ = errors_run(run_script, stub_run, scenario="functions-healthy")
    result = out.envelope["outputs"]["logs"]
    assert out.envelope["status"] == "ok"
    assert (result["entries"], result["matched"], result["truncated"]) == ([], 0, False)


def test_a_list_longer_than_max_log_entries_is_cut_and_says_so(run_script, stub_run):
    stub = stub_run("functions-broken")
    stub.set(
        logs_argv(GEN2_FILTER, limit=3),
        stdout=(stub.directory / "logs-orders-api.json").read_text(),
    )
    out = run(run_script, "function_errors", stub, settings={"maxLogEntries": 2})
    result = out.envelope["outputs"]["logs"]
    assert (result["matched"], result["truncated"]) == (2, True)
    assert result["entries"][1]["httpStatus"] == 500  # the oldest entry is the one left out


def test_since_is_the_freshness_and_is_bounded(run_script, stub_run):
    stub = stub_run("functions-broken")
    stub.set(logs_argv(GEN2_FILTER, freshness="86400s"), stdout="[]\n")
    out = run(run_script, "function_errors", stub, since="1d")
    assert out.envelope["outputs"]["logs"]["window"]["seconds"] == 86400
    for bad in ("15d", "30s", "soon", "0s"):
        stub = stub_run("functions-broken")
        out = run(run_script, "function_errors", stub, since=bad)
        assert out.envelope["errors"][0]["code"] == "invalid_input" and stub.log == [], bad


def test_an_entry_without_text_has_a_null_message_and_no_payload_dump(run_script, stub_run):
    stub = stub_run("functions-broken")
    entry = {
        "severity": "ERROR",
        "timestamp": "2026-11-04T00:10:00Z",
        "jsonPayload": {"code": 7, "env": {"K": "canary-pass-1"}},
    }
    stub.set(
        logs_argv(GEN2_FILTER),
        stdout=json.dumps(
            [
                entry,
                {
                    "severity": "CRITICAL",
                    "protoPayload": {"status": {"message": "deadline exceeded"}},
                },
            ]
        ),
    )
    result = run(run_script, "function_errors", stub).envelope["outputs"]["logs"]
    assert (
        result["entries"][0]["message"] is None and result["entries"][0]["payload"] == "jsonPayload"
    )
    assert (
        result["entries"][1]["message"] == "deadline exceeded"
        and result["entries"][1]["payload"] == "protoPayload"
    )
    assert "canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("functions-forbidden", "permission_denied"),
        ("functions-throttled", "rate_limited"),
        ("functions-api-disabled", "dependency"),
    ],
)
def test_a_failed_function_read_fails_the_task(run_script, stub_run, scenario, code):
    out, _ = errors_run(run_script, stub_run, scenario=scenario)
    assert out.envelope["status"] == "failed" and [e["code"] for e in out.envelope["errors"]] == [
        code
    ]


def test_a_failed_log_read_fails_the_task_never_an_empty_list(run_script, stub_run):
    stub = stub_run("functions-healthy")
    stub.set(
        logs_argv(GEN2_FILTER),
        rc=1,
        stderr="ERROR: (gcloud.logging.read) PERMISSION_DENIED: Permission 'logging.logEntries.list' denied on resource 'projects/shop-prod-1234'.\n",
    )
    out = run(run_script, "function_errors", stub)
    assert out.envelope["status"] == "failed" and "logs" not in out.envelope["outputs"]
    assert [e["code"] for e in out.envelope["errors"]] == ["permission_denied"]


def test_a_missing_function_is_not_found_not_a_log_with_no_errors(run_script, stub_run):
    out, stub = errors_run(
        run_script,
        stub_run,
        scenario="functions-not-found",
        resource=function_target("nope", region="us-central1"),
    )
    assert [e["code"] for e in out.envelope["errors"]] == ["not_found"]
    assert not any(a[0] == "logging" for a in stub.asked())


def test_the_log_text_scrub_removes_the_google_credential_shapes_and_cuts(run_script, stub_run):
    samples = {
        "ya29.a0AfH6SMBabcdefghijklmnop": "ya29",
        "AIza" + "x" * 35: "AIza",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123": "abcdefghijklmnopqrstuvwxyz0123",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig": "eyJhbGci",
        "-----BEGIN PRIVATE KEY-----\nMIIEvQ": "BEGIN PRIVATE KEY",
        '{"private_key": "abc", "client_secret": "def"}': '"abc"',
        "psql postgres://u:hunter2@db.example.com/x": "hunter2",
        "API_KEY=abcd1234 token: 'zzzz9999'": "abcd1234",
        "environ({'A': 'canary-env-1', 'B': 'canary-env-2'})": "canary-env",
    }
    for text, leaked in samples.items():
        assert leaked not in lt.redact(text), text
    clipped, cut = lt.clip("a" * 5000)
    assert len(clipped) == 1024 and cut and clipped.endswith("...")
    assert lt.clip("short") == ("short", False)


# ---- function-metrics ---------------------------------------------------------------------------


def metrics_run(run_script, stub_run, scenario="functions-broken", resource=ORDERS, **kwargs):
    stub = stub_run(scenario)
    return run(run_script, "function_metrics", stub, resource=resource, **kwargs), stub


def test_a_second_gen_function_reads_cloud_run_request_metrics(run_script, stub_run):
    out, stub = metrics_run(run_script, stub_run)
    env = out.envelope
    assert env["status"] == "ok" and out.errors == []
    m = env["outputs"]["metrics"]
    check_schema("metrics", m)
    assert (m["generation"], m["hasData"], m["idle"]) == (2, True, False)
    assert (m["executions"], m["errors"]) == (3662, 640)
    assert m["byOutcome"] == {
        "2xx": 3010,
        "4xx": 12,
        "5xx": 640,
    }  # a 4xx is the caller's, not an error
    assert m["errorRate"] == round(640 / 3662, 4) and m["errorRateAboveThreshold"] is True
    assert m["latency"] == {"p50Ms": 44.0, "p95Ms": 980.0, "p99Ms": 59500.0}
    assert (
        m["timeoutSeconds"] == 60 and m["latencyNearTimeout"] is True
    )  # p99 59.5s of a 60s timeout
    assert m["series"] == {"executions": 3, "p50": 1, "p95": 1, "p99": 1}
    assert m["window"]["seconds"] == 3600 and m["window"]["end"].endswith("Z")
    assert stub.asked("gcloud") == [describe_argv("orders-api", "us-central1"), TOKEN]
    assert len(stub.asked("curl")) == 4
    first = stub.log[2]["argv"]
    assert (
        first[0] == "curl"
        and "--fail-with-body" in first
        and first[first.index("-H") + 1] == "@.gcp-monitoring-header"
    )


def test_the_token_is_only_in_a_header_file_that_is_gone_afterwards(run_script, stub_run):
    out, stub = metrics_run(run_script, stub_run)
    token = json.loads((PLUGIN / "tests" / "stub" / "identities.json").read_text())["fakeToken"]
    assert (
        len(stub.headers) == 4
    )  # StubRun checked the file at every call: mode 0600, the fake token
    assert all(
        f"Authorization: Bearer {token}\n" in h and f"X-Goog-User-Project: {P}" in h
        for h in stub.headers
    )
    assert all(token not in " ".join(c["argv"]) for c in stub.log)
    assert out.workdir_dotfiles == []  # deleted in finally
    assert token not in json.dumps(out.envelope)


def test_a_first_gen_function_reads_execution_count_by_status_and_converts_nanoseconds(
    run_script, stub_run
):
    out, stub = metrics_run(
        run_script, stub_run, resource=function_target("nightly-report", region="us-east1")
    )
    m = out.envelope["outputs"]["metrics"]
    assert m["generation"] == 1 and m["byOutcome"] == {"error": 1, "ok": 18, "timeout": 5}
    assert (m["executions"], m["errors"]) == (24, 6)  # every status but ok
    assert m["latency"] == {"p50Ms": 5200.0, "p95Ms": 60000.0, "p99Ms": 60000.0}  # ns -> ms
    assert m["latencyNearTimeout"] is True and m["errorRateAboveThreshold"] is True
    urls = [c["argv"][-1] for c in stub.log if c["tool"] == "curl"]
    assert all("cloudfunctions.googleapis.com%2Ffunction%2F" in u for u in urls)
    assert all("cloud_function" in u and "cloud_run_revision" not in u for u in urls)


def test_a_healthy_function_is_below_every_threshold(run_script, stub_run):
    out, _ = metrics_run(run_script, stub_run, scenario="functions-healthy")
    m = out.envelope["outputs"]["metrics"]
    assert (m["executions"], m["errors"], m["errorRate"]) == (4151, 0, 0.0)
    assert (m["errorRateAboveThreshold"], m["latencyNearTimeout"]) == (False, False)
    assert m["latency"]["p99Ms"] == 310.0


def test_a_window_with_no_series_is_idle_and_says_nothing_about_errors(run_script, stub_run):
    out, _ = metrics_run(run_script, stub_run, scenario="functions-idle")
    env = out.envelope
    m = env["outputs"]["metrics"]
    assert env["status"] == "ok"
    assert (m["hasData"], m["idle"], m["executions"]) == (False, True, 0)
    # no data is not zero errors and not healthy: null, not 0 and not false
    assert (m["errors"], m["errorRate"], m["errorRateAboveThreshold"], m["latencyNearTimeout"]) == (
        None,
        None,
        None,
        None,
    )
    assert m["latency"] == {"p50Ms": None, "p95Ms": None, "p99Ms": None} and m["byOutcome"] == {}
    assert m["series"] == {"executions": 0, "p50": 0, "p95": 0, "p99": 0}
    check_schema("metrics", m)


def test_a_query_that_fails_leaves_its_numbers_null_and_the_rest_stand(run_script, stub_run):
    out, _ = metrics_run(run_script, stub_run, scenario="functions-partial-metrics")
    env = out.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [
        ("rate_limited", "p95"),
        ("rate_limited", "p99"),
    ]
    m = env["outputs"]["metrics"]
    assert m["executions"] == 4151 and m["latency"] == {"p50Ms": 38.0, "p95Ms": None, "p99Ms": None}
    assert m["series"] == {"executions": 2, "p50": 1, "p95": None, "p99": None}
    assert m["latencyNearTimeout"] is None  # no p99: not false
    check_schema("metrics", m)


def test_the_executions_query_failing_alone_nulls_everything_that_needs_it(run_script, stub_run):
    stub = stub_run("functions-healthy")
    stub.set(
        [a for a in stub.calls if a.get("tool") == "curl"][0]["argv"],
        tool="curl",
        rc=22,
        stdout="",
        stderr="curl: (22) The requested URL returned error: 429\n",
    )
    out = run(run_script, "function_metrics", stub)
    m = out.envelope["outputs"]["metrics"]
    assert (
        out.envelope["status"] == "partial" and out.envelope["errors"][0]["scope"] == "executions"
    )
    assert (
        m["hasData"],
        m["idle"],
        m["executions"],
        m["errors"],
        m["errorRate"],
        m["byOutcome"],
    ) == (None,) * 6
    assert m["latency"]["p99Ms"] == 310.0


def test_monitoring_disabled_fails_the_task_with_dependency(run_script, stub_run):
    out, _ = metrics_run(run_script, stub_run, scenario="functions-monitoring-disabled")
    env = out.envelope
    assert env["status"] == "failed" and "metrics" not in env["outputs"]
    assert [e["code"] for e in env["errors"]] == ["dependency"]
    assert "Monitoring" in env["errors"][0]["message"]


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("functions-forbidden", "permission_denied"),
        ("functions-throttled", "rate_limited"),
        ("functions-api-disabled", "dependency"),
    ],
)
def test_a_failed_function_read_fails_the_metrics_task(run_script, stub_run, scenario, code):
    out, stub = metrics_run(run_script, stub_run, scenario=scenario)
    assert [e["code"] for e in out.envelope["errors"]] == [code]
    assert stub.asked("curl") == []  # no Monitoring read for a function that could not be read


def test_a_missing_function_is_not_found_not_idle(run_script, stub_run):
    out, stub = metrics_run(
        run_script,
        stub_run,
        scenario="functions-not-found",
        resource=function_target("nope", region="us-central1"),
    )
    assert [e["code"] for e in out.envelope["errors"]] == ["not_found"] and stub.asked("curl") == []


def test_a_failed_token_read_fails_the_task_and_leaves_no_file(run_script, stub_run):
    stub = stub_run("functions-healthy")
    stub.set(
        TOKEN,
        rc=1,
        stderr="ERROR: (gcloud.auth.print-access-token) You do not currently have an active account selected.\n",
    )
    out = run(run_script, "function_metrics", stub)
    assert [e["code"] for e in out.envelope["errors"]] == ["permission_denied"]
    assert out.workdir_dotfiles == [] and stub.asked("curl") == []


@pytest.mark.parametrize("bad", ["59s", "15d", "soon"])
def test_a_bad_since_is_invalid_input_before_any_call(run_script, stub_run, bad):
    stub = stub_run("functions-healthy")
    out = run(run_script, "function_metrics", stub, since=bad)
    assert out.envelope["errors"][0]["code"] == "invalid_input" and stub.log == []


@pytest.mark.parametrize(
    "setting, bad",
    [
        ("errorRateThreshold", 1.5),
        ("errorRateThreshold", True),
        ("latencyTimeoutRatio", 0.05),
        ("maxLogEntries", 0),
    ],
)
def test_a_bad_threshold_setting_is_invalid_input(run_script, stub_run, setting, bad):
    stub = stub_run("functions-healthy")
    out = run(run_script, "function_metrics", stub, settings={setting: bad})
    assert out.envelope["errors"][0]["code"] == "invalid_input" and stub.log == []


def test_thresholds_are_strict_and_come_from_the_settings(run_script, stub_run):
    stub = stub_run("functions-broken")
    low = run(
        run_script,
        "function_metrics",
        stub,
        settings={"errorRateThreshold": 0.9, "latencyTimeoutRatio": 1.0},
    )
    m = low.envelope["outputs"]["metrics"]
    assert (
        m["errorRateAboveThreshold"] is False and m["latencyNearTimeout"] is False
    )  # 0.175 <= 0.9; 59.5s < 60s


def test_the_monitoring_url_is_one_function_aggregated_by_monitoring_itself():
    sp = metrics.spec_for(2)
    names = [q for q, _, _ in metrics.queries(sp, 7200)]
    assert names == ["executions", "p50", "p95", "p99"]
    _, metric, agg = metrics.queries(sp, 7200)[0]
    assert metric == "run.googleapis.com/request_count"
    assert dict(agg) == {
        "alignmentPeriod": "7200s",
        "perSeriesAligner": "ALIGN_DELTA",
        "crossSeriesReducer": "REDUCE_SUM",
        "groupByFields": "metric.label.response_code_class",
    }
    flt = metrics.metric_filter(sp, metric, "orders-api", "us-central1")
    assert (
        flt
        == 'metric.type = "run.googleapis.com/request_count" AND resource.type="cloud_run_revision" AND resource.labels.service_name="orders-api" AND resource.labels.location="us-central1"'
    )
    url = monitoring.url(P, "2026-11-04T00:00:00Z", "2026-11-04T01:00:00Z", flt, agg)
    assert url.startswith(f"https://monitoring.googleapis.com/v3/projects/{P}/timeSeries?filter=")
    assert "aggregation.groupByFields=metric.label.response_code_class" in url
    with pytest.raises(RwError):
        metrics.spec_for(None)


def test_the_metrics_budget_matches_the_task_timeout():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "function-metrics")
    assert task["timeout"] == f"{metrics.TASK_BUDGET}s"


def test_a_budget_that_runs_out_before_a_page_is_a_timeout_never_a_partial_series(tmp_path):
    class Ctx:
        workdir = tmp_path

    reader = monitoring.Reader(Ctx(), "fake-token-12345", P, 60, deadline=100.0, clock=lambda: 99.0)
    try:
        with pytest.raises(RwError) as exc:  # 1s left: under the 5s a call needs
            reader.series("2026-11-04T00:00:00Z", "2026-11-04T01:00:00Z", "f", [])
        assert exc.value.code == "timeout"
        assert (tmp_path / monitoring.HEADER_FILE).stat().st_mode & 0o777 == 0o600
    finally:
        reader.close()
    assert not (tmp_path / monitoring.HEADER_FILE).exists()


# ---- the shared reader, and what the tasks declare ----------------------------------------------


def test_the_generation_and_state_of_each_shape():
    gen2 = {"environment": "GEN_2", "state": "FAILED"}
    gen1 = {"status": "OFFLINE"}
    assert (fnlist.generation(gen2), fnlist.state(gen2)) == (2, "FAILED")
    assert (fnlist.generation(gen1), fnlist.state(gen1)) == (1, "OFFLINE")
    assert (
        fnlist.generation({"environment": "GEN_1", "state": "ACTIVE"}),
        fnlist.generation({}),
    ) == (1, None)
    assert (
        fnlist.state({"state": "ACTIVE"}) is None
    )  # no environment: 1st gen has `status`, which is missing
    assert fnlist.state({"environment": "GEN_2"}) is None


def test_unreachable_regions_are_read_from_the_warning():
    v2 = "WARNING: The following regions were fully or partially unreachable for query: projects/p/locations/asia-east1, projects/p/locations/us-west2\nThis could be due to permission setup. Additional information can be found in: https://cloud.google.com/functions/docs/troubleshooting\n"
    v1 = "WARNING: The following regions were fully or partially unreachable for query: europe-north1This could be due to permission setup.\n"
    assert fnlist.unreachable_regions(v2) == ["asia-east1", "us-west2"]
    assert fnlist.unreachable_regions(v1) == ["europe-north1"]
    assert fnlist.unreachable_regions("WARNING: something else") == []


def test_the_real_gcloud_warning_of_the_stub_is_understood(stub_run):
    stub = stub_run("functions-unreachable")
    call = next(c for c in stub.calls if c["argv"][:2] == ["functions", "list"])
    assert fnlist.unreachable_regions(call["stderr"]) == ["asia-east1", "southamerica-east1"]


def test_classification_of_the_functions_errors_gcloud_prints():
    assert (
        classify_error(
            "ResponseError: status=[404], code=[], message=[Function nope does not exist in us-central1]"
        )
        == "not_found"
    )
    assert (
        classify_error(
            "ResponseError: status=[403], code=[], message=[Permission 'x' denied on resource]"
        )
        == "permission_denied"
    )
    assert (
        classify_error(
            "PERMISSION_DENIED: Cloud Build API has not been used in project 1 before or it is disabled."
        )
        == "dependency"
    )
    assert (
        classify_error(
            "ResponseError: status=[429], code=[], message=[Quota exceeded for quota metric]"
        )
        == "rate_limited"
    )


READ_ONLY = {
    ("functions", "describe"): {"cloudfunctions.functions.get"},
    ("functions", "list"): {"cloudfunctions.functions.list", "cloudfunctions.locations.list"},
    ("builds", "describe"): {"cloudbuild.builds.get"},
    ("logging", "read"): {"logging.logEntries.list"},
    ("auth", "print-access-token"): set(),
}


def needed(stub):
    out = {"serviceusage.services.use"}
    for call in stub.log:
        if call["tool"] == "curl":
            out.add("monitoring.timeSeries.list")
        else:
            out |= READ_ONLY[tuple(call["asked"][:2])]
    return out


@pytest.mark.parametrize(
    "task, script, resource, kwargs",
    [
        ("function-state", "function_state", ORDERS, {}),
        ("function-state", "function_state", function_target("orders-api"), {}),
        ("function-errors", "function_errors", ORDERS, {}),
        ("function-errors", "function_errors", function_target("orders-api"), {}),
        ("function-metrics", "function_metrics", ORDERS, {}),
        ("function-metrics", "function_metrics", function_target("orders-api"), {}),
    ],
)
def test_every_call_a_task_makes_is_a_permission_it_declares(
    run_script, stub_run, task, script, resource, kwargs
):
    stub = stub_run("functions-broken")
    out = run(run_script, script, stub, resource=resource, **kwargs)
    assert out.envelope["status"] in ("ok", "partial"), out.envelope["errors"]
    declared = {
        p["permission"]
        for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
        if t["name"] == task
        for p in t["permissions"]
    }
    assert needed(stub) <= declared, needed(stub) - declared


def test_each_task_is_read_only_and_the_text_limits_are_the_documented_ones():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    assert [t["name"] for t in doc["tasks"]] == [
        "function-state",
        "function-errors",
        "function-metrics",
    ]
    assert all(t["readOnly"] is True for t in doc["tasks"])
    assert lt.MAX_TEXT_CHARS == 1024 and doc["settings"]["maxLogEntries"]["default"] == 50
    skill = (SKILL / "SKILL.md").read_text()
    assert len(skill.splitlines()) < 150
    front = yaml.safe_load(skill.split("---")[1])
    assert front["name"] == "gcp-functions-health" and len(front["description"]) <= 1024
    assert front["runwhen"]["appliesTo"]["types"] == ["gcp/cloud-function"]


def test_the_window_helpers_use_the_runs_reference_time():
    now = datetime(2026, 11, 4, 1, 0, 0, 123456, tzinfo=timezone.utc)
    start, end = logs.window(now, 3600)
    assert (start.isoformat(), end.isoformat()) == (
        "2026-11-04T00:00:00+00:00",
        "2026-11-04T01:00:00+00:00",
    )
    assert metrics.window(now, 60)[0].isoformat() == "2026-11-04T00:59:00+00:00"
