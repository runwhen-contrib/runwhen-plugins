"""Writes the Cloud Functions part of tests/stub/scenarios/*: the canned answers of the gcloud/curl
stub for gcp-functions-health and for the functions half of gcp-base/discover, produced by the
REAL gcloud of the pinned runtime image.

Run it through capture_functions.sh, never directly on a workstation: it runs inside
`rwp-runtime-gcp:dev` with `--network none`. It starts a local HTTP server that speaks the REST
shapes of Cloud Functions v1 and v2, Cloud Build v1 and Cloud Logging v2 (the world below: invented
project, functions, builds and log entries), points gcloud at it with
CLOUDSDK_API_ENDPOINT_OVERRIDES_*, runs each command the plugin issues and stores what gcloud
really printed: its JSON formatting, its error lines and exit codes. Nothing here contacts Google,
and no real credential exists: the access token is the fake one of identities.json.

What it writes:

* new scenarios `functions-*` (calls.yaml and the files it names), rewritten from scratch;
* the `functions list` call added to the bucket pilot's `healthy`, `broken` and `project-denied`
  scenarios (gcp-base/discover lists functions after the buckets), replacing the previous one.
  Everything else in those scenarios is left alone (capture.py owns it).

The Cloud Monitoring replies to the curl calls are not gcloud's (curl is not a Google tool): they
are written from the REST reference (timeSeries.list), with curl's own `--fail-with-body` error
behaviour for the failures. Each calls.yaml entry cites `source` (the REST reference its shape
follows) and `cli_source` (the file of the pinned SDK that formats the answer, or the curl
behaviour)."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/plugin-lib")  # the gcp-functions-health lib: URLs and argv come from its code
from gcp_functions_health import logs as plugin_logs  # noqa: E402
from gcp_functions_health import metrics, monitoring  # noqa: E402

OUT = "/stub/scenarios"
IDENT = json.load(open("/stub/identities.json"))
TOKEN = IDENT["fakeToken"]
P = "shop-prod-1234"
NUMBER = "123456789012"
SA = f"rw-reader@{P}.iam.gserviceaccount.com"
RUNTIME_SA = f"orders@{P}.iam.gserviceaccount.com"
SDK_VERSION = "gcloud 592.0.0"
SDK = "googlecloudsdk"
NOW = datetime(2026, 11, 4, 1, 0, 0, tzinfo=timezone.utc)
SINCE = 3600  # the recorded look-back: the default 1h
# CAPTURE_ONLY=functions-split,healthy rewrites just those scenarios (a full run takes minutes)
ONLY = {n for n in os.environ.get("CAPTURE_ONLY", "").split(",") if n}


def active(name):
    return not ONLY or name in ONLY


spec = importlib.util.spec_from_file_location("stublib", "/stub/stublib.py")
stublib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stublib)

# ---- the world: REST shapes ------------------------------------------------------------------

# Values that must never reach an output. They are in the canned answers of the `functions-leaky`
# scenario only (test_fixture_hygiene.py enforces where RWCANARY may appear); the other worlds
# have env values and log lines with nothing in them to scrub.
CANARY_URL = "postgres://user:RWCANARY-1@example.invalid/db"
LEAKY = {"billing-hook", "legacy-leak"}


def build_name(region, suffix):
    return f"projects/{NUMBER}/locations/{region}/builds/a1b2c3d4-0000-4000-8000-{suffix:012d}"


def fn2(name, region, *, leak=False, state="ACTIVE", event=None, suffix=1, messages=None, revision=None,
        max_instances=20, timeout=60, runtime="python312", entry="handler", updated="2026-10-30T14:02:11.123456Z"):  # fmt: skip
    """A 2nd gen function in the v2 REST shape (cloudfunctions v2 Function)."""
    row = {
        "name": f"projects/{P}/locations/{region}/functions/{name}",
        "environment": "GEN_2",
        "state": state,
        "updateTime": updated,
        "labels": {"deployment-tool": "console", "team": "web"},
        "buildConfig": {
            "build": build_name(region, suffix),
            "runtime": runtime,
            "entryPoint": entry,
            "environmentVariables": {"BUILD_FLAVOUR": "release"},
            "dockerRegistry": "ARTIFACT_REGISTRY",
        },
        "serviceConfig": {
            "service": f"projects/{P}/locations/{region}/services/{name}",
            "timeoutSeconds": timeout,
            "availableMemory": "512Mi",
            "availableCpu": "1",
            "environmentVariables": {"LOG_EXECUTION_ID": "true", "LOG_LEVEL": "info"},
            "secretEnvironmentVariables": [
                {"key": "STRIPE_KEY", "projectId": P, "secret": "stripe-key", "version": "latest"}
            ],
            "maxInstanceCount": max_instances,
            "minInstanceCount": 0,
            "maxInstanceRequestConcurrency": 80,
            "ingressSettings": "ALLOW_ALL",
            "uri": f"https://{name}-k3j2h1g0fa-uc.a.run.app",
            "serviceAccountEmail": RUNTIME_SA,
            "allTrafficOnLatestRevision": True,
            "revision": revision or f"{name}-00007-xaz",
        },
        "url": f"https://{region}-{P}.cloudfunctions.net/{name}",
    }
    if leak:
        row["serviceConfig"]["environmentVariables"].update(
            ORDERS_DB_URL=CANARY_URL, API_TOKEN="RWCANARY-2"
        )
        row["serviceConfig"]["secretEnvironmentVariables"][0]["secret"] = "RWCANARY-3-secret"
        row["buildConfig"]["environmentVariables"]["BUILD_PASSWORD"] = "RWCANARY-4"
    if max_instances is None:
        del row["serviceConfig"]["maxInstanceCount"]
    if event:
        row["eventTrigger"] = event
    if messages:
        row["stateMessages"] = messages
    return row


def fn1(
    name,
    region,
    *,
    leak=False,
    status="ACTIVE",
    suffix=100,
    event=None,
    version="12",
    runtime="nodejs20",
):
    """A 1st gen function in the v1 REST shape (cloudfunctions v1 CloudFunction)."""
    row = {
        "name": f"projects/{P}/locations/{region}/functions/{name}",
        "status": status,
        "entryPoint": "run",
        "runtime": runtime,
        "timeout": "60s",
        "availableMemoryMb": 256,
        "serviceAccountEmail": f"jobs@{P}.iam.gserviceaccount.com",
        "updateTime": "2026-09-12T08:31:40.511Z",
        "versionId": version,
        "labels": {"team": "ops"},
        "environmentVariables": {"REGION": region, "LOG_LEVEL": "info"},
        "maxInstances": 5,
        "ingressSettings": "ALLOW_ALL",
        "buildId": f"a1b2c3d4-0000-4000-8000-{suffix:012d}",
        "buildName": build_name(region, suffix),
        "sourceUploadUrl": "https://storage.googleapis.com/uploads-0000000000.us-east1.cloudfunctions.appspot.com/upload.zip",
    }
    if leak:
        row["environmentVariables"].update(REPORT_DB_URL=CANARY_URL, API_KEY="RWCANARY-2")
    if event:
        row["eventTrigger"] = event
    else:
        row["httpsTrigger"] = {
            "url": f"https://{region}-{P}.cloudfunctions.net/{name}",
            "securityLevel": "SECURE_ALWAYS",
        }
    return row


STORAGE_EVENT = {
    "trigger": f"projects/{P}/locations/europe-west1/triggers/thumbnailer-123456",
    "triggerRegion": "europe-west1",
    "eventType": "google.cloud.storage.object.v1.finalized",
    "eventFilters": [{"attribute": "bucket", "value": "shop-assets"}],
    "pubsubTopic": f"projects/{P}/topics/gcf-v2-triggers-thumbnailer",
    "serviceAccountEmail": RUNTIME_SA,
    "retryPolicy": "RETRY_POLICY_DO_NOT_RETRY",
}
PUBSUB_EVENT = {
    "eventType": "google.pubsub.topic.publish",
    "resource": f"projects/{P}/topics/nightly",
    "service": "pubsub.googleapis.com",
    "failurePolicy": {},
}

ORDERS = fn2("orders-api", "us-central1", suffix=1)
THUMBS = fn2(
    "thumbnailer", "europe-west1", suffix=2, event=STORAGE_EVENT, entry="resize", max_instances=None
)
CRON = fn1("legacy-cron", "us-east1", suffix=100, event=PUBSUB_EVENT)
CHECKOUT = fn2(
    "checkout", "us-central1", state="FAILED", suffix=3, revision="checkout-00002-pkq",
    messages=[{"severity": "ERROR", "type": "BuildFailed",
               "message": "Build failed with status: FAILURE and message: ModuleNotFoundError: No module named 'stripe_client'. Check the build logs."},
              {"severity": "WARNING", "type": "CloudRunService",
               "message": "Revision 'checkout-00003-abc' is not ready and cannot serve traffic."}],
)  # fmt: skip
REPORT = fn1("nightly-report", "us-east1", status="OFFLINE", suffix=101, version="4")
BILLING = fn2(
    "billing-hook", "us-central1", leak=True, state="FAILED", suffix=4,
    messages=[{"severity": "ERROR", "type": "BuildFailed", "message": "build failed: password=RWCANARY-6 was rejected"}],
)  # fmt: skip
LEGACY_LEAK = fn1("legacy-leak", "us-east1", leak=True, status="OFFLINE", suffix=102)
HEALTHY = [ORDERS, THUMBS, CRON]
LEAKY_WORLD = [BILLING, LEGACY_LEAK]
BROKEN = [ORDERS, CHECKOUT, THUMBS, CRON, REPORT]
SPLIT = [ORDERS, fn2("orders-api", "europe-west1", suffix=9), CRON]  # one id in two regions

# Cloud Build resources by build id: the status of each function's latest build
BUILD_FIELDS = {
    "id": None, "projectId": P, "status": "SUCCESS",
    "createTime": "2026-10-30T14:00:02.481516Z", "startTime": "2026-10-30T14:00:03.120442Z",
    "finishTime": "2026-10-30T14:02:09.909112Z",
    "logUrl": f"https://console.cloud.google.com/cloud-build/builds;region=us-central1/{{id}}?project={NUMBER}",
    "tags": ["gcf-deploy"],
    "steps": [{"name": "gcr.io/gae-runtimes/buildpacks/python312/builder",
               "args": ["/cnb/lifecycle/builder"], "env": ["GOOGLE_ENTRYPOINT=handler"],
               "status": "SUCCESS"}],
}  # fmt: skip


def build_doc(fn):
    config = fn.get("buildConfig")
    name = config["build"] if config else fn["buildName"]
    bid = name.rsplit("/", 1)[1]
    doc = dict(BUILD_FIELDS, id=bid)
    doc["logUrl"] = BUILD_FIELDS["logUrl"].format(id=bid)
    if fn.get("state") == "FAILED" or fn.get("status") == "OFFLINE":
        leaks = fn["name"].rsplit("/", 1)[1] in LEAKY
        doc.update(
            status="FAILURE", statusDetail="build failed: step exited with non-zero status: 1",
            finishTime="2026-10-30T14:01:30.000000Z",
            failureInfo={"type": "USER_BUILD_STEP",
                         "detail": "Build failed; check build logs for details"
                         + (": access_token=RWCANARY-5 was rejected" if leaks else "")},
        )  # fmt: skip
        if leaks:
            doc["steps"] = [
                dict(doc["steps"][0], env=["GOOGLE_ENTRYPOINT=handler", "DB_PASSWORD=RWCANARY-4"])
            ]
    return doc


def log_entry(fn, text, when, *, gen, http=None, payload="text"):
    region = fn["name"].split("/")[3]
    name = fn["name"].rsplit("/", 1)[1]
    entry = {
        "insertId": f"{zlib.crc32(text.encode()) % 10**9:09d}",
        "severity": "ERROR",
        "timestamp": when,
        "receiveTimestamp": when,
        "logName": f"projects/{P}/logs/cloudfunctions.googleapis.com%2Fcloud-functions"
        if gen == 1 else f"projects/{P}/logs/run.googleapis.com%2Fstderr",
        "resource": {"type": "cloud_function", "labels": {"function_name": name, "project_id": P, "region": region}}
        if gen == 1 else
        {"type": "cloud_run_revision", "labels": {"configuration_name": name, "location": region, "project_id": P,
                                                    "revision_name": fn["serviceConfig"]["revision"], "service_name": name}},
        "labels": {"execution_id": "7f1k2l9x0p3q"} if gen == 1 else {"instanceId": "0033"},
    }  # fmt: skip
    if payload == "text":
        entry["textPayload"] = text
    elif payload == "json":
        entry["jsonPayload"] = {"message": text, "environment": {"LOG_LEVEL": "info"}}
        if name in LEAKY:
            entry["jsonPayload"]["environment"]["ORDERS_DB_URL"] = CANARY_URL
    if http:
        entry["httpRequest"] = {
            "requestMethod": "POST",
            "status": http,
            "requestUrl": f"https://{name}.example.com/orders"
            + ("?token=RWCANARY-10" if name in LEAKY else ""),
        }
    return entry


LONG = "x" * 1500
LOGS = {
    "orders-api": [
        log_entry(ORDERS, "Traceback (most recent call last):\n  File \"main.py\", line 41, in handler\n    charge(order)\nstripe.error.APIConnectionError: connection reset by peer", "2026-11-04T00:41:12.481Z", gen=2),
        log_entry(ORDERS, f"order 4411 failed: db timeout while charging {LONG}", "2026-11-04T00:38:02.100Z", gen=2, payload="json", http=500),
        log_entry(ORDERS, "upstream timeout after 60s", "2026-11-04T00:22:45.907Z", gen=2, http=504),
    ],
    "checkout": [
        log_entry(CHECKOUT, "Container failed to start. Failed to start and then listen on the port defined by the PORT environment variable.", "2026-11-04T00:55:01.000Z", gen=2),
    ],
    "billing-hook": [
        log_entry(BILLING, f"charge failed: db error {CANARY_URL} password=RWCANARY-8 Authorization: Bearer RWCANARY-7", "2026-11-04T00:44:00.000Z", gen=2, payload="json", http=500),
        log_entry(BILLING, "stripe call failed STRIPE_SECRET=RWCANARY-9", "2026-11-04T00:40:00.000Z", gen=2),
    ],
    "legacy-leak": [
        log_entry(LEGACY_LEAK, "boot failed API_KEY=RWCANARY-11", "2026-11-04T00:30:00.000Z", gen=1),
    ],
    "nightly-report": [
        log_entry(REPORT, "Function execution took 60001 ms, finished with status: 'timeout'", "2026-11-04T00:30:00.250Z", gen=1),
    ],
}  # fmt: skip


def metric_doc(counts, p50, p95, p99, gen):
    """The Monitoring replies of one function: the REDUCE_SUM series by outcome and the three
    percentile series, written from the timeSeries.list reference."""
    label = "status" if gen == 1 else "response_code_class"
    count_type = (
        "cloudfunctions.googleapis.com/function/execution_count"
        if gen == 1
        else "run.googleapis.com/request_count"
    )
    lat_type = (
        "cloudfunctions.googleapis.com/function/execution_times"
        if gen == 1
        else "run.googleapis.com/request_latencies"
    )
    scale = 1e6 if gen == 1 else 1.0  # execution_times is in nanoseconds, request_latencies in ms
    interval = {"startTime": "2026-11-04T00:00:00Z", "endTime": "2026-11-04T01:00:00Z"}
    if not counts:
        return {}  # no series: the API answers {}
    docs = {
        "executions": {"timeSeries": [
            {"metric": {"labels": {label: outcome}, "type": count_type}, "resource": {"type": "global", "labels": {}},
             "metricKind": "DELTA", "valueType": "INT64",
             "points": [{"interval": interval, "value": {"int64Value": str(n)}}]}
            for outcome, n in counts.items()
        ], "unit": "1"},
    }  # fmt: skip
    for key, value in (("p50", p50), ("p95", p95), ("p99", p99)):
        docs[key] = {"timeSeries": [{"metric": {"type": lat_type}, "resource": {"type": "global", "labels": {}},
                                     "metricKind": "DELTA", "valueType": "DOUBLE",
                                     "points": [{"interval": interval, "value": {"doubleValue": value * scale}}]}],
                     "unit": "ns" if gen == 1 else "ms"}  # fmt: skip
    return docs


METRICS = {
    "healthy": {
        "orders-api": metric_doc({"2xx": 4120, "4xx": 31}, 38.0, 120.5, 310.0, 2),
        "thumbnailer": metric_doc({"2xx": 220}, 410.0, 900.0, 1350.0, 2),
        "legacy-cron": metric_doc({"ok": 24}, 1500.0, 2200.0, 2400.0, 1),
    },
    "broken": {
        "orders-api": metric_doc({"2xx": 3010, "4xx": 12, "5xx": 640}, 44.0, 980.0, 59500.0, 2),
        "checkout": metric_doc({}, 0, 0, 0, 2),
        "thumbnailer": metric_doc({"2xx": 220}, 410.0, 900.0, 1350.0, 2),
        "legacy-cron": metric_doc({"ok": 24}, 1500.0, 2200.0, 2400.0, 1),
        "nightly-report": metric_doc({"ok": 18, "timeout": 5, "error": 1}, 5200.0, 60000.0, 60000.0, 1),
    },
}  # fmt: skip

# ---- faults: REST error bodies ----------------------------------------------------------------


def disabled_message(api, host):
    return (
        f"{api} API has not been used in project {NUMBER} before or it is disabled. Enable it by "
        f"visiting https://console.developers.google.com/apis/api/{host}/overview?project={NUMBER} "
        "then retry. If you enabled this API recently, wait a few minutes for the action to "
        "propagate to our systems and retry."
    )


def error_body(code, status, message, reason=None, host=None):
    err = {"code": code, "message": message, "status": status}
    if reason:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "domain": "googleapis.com",
                           "metadata": {"consumer": f"projects/{NUMBER}", "service": host}}]  # fmt: skip
    return {"error": err}


def denied(permission, what="projects/" + P):
    return (
        403,
        error_body(
            403,
            "PERMISSION_DENIED",
            f"Permission '{permission}' denied on resource '{what}' (or it may not exist).",
        ),
    )


def quota(host):
    return (429, error_body(429, "RESOURCE_EXHAUSTED",
                            f"Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute per user' of service '{host}' for consumer 'project_number:{NUMBER}'.",
                            "RATE_LIMIT_EXCEEDED", host))  # fmt: skip


def disabled(api, host):
    return (
        403,
        error_body(403, "PERMISSION_DENIED", disabled_message(api, host), "SERVICE_DISABLED", host),
    )


FAULTS = {
    "forbidden": {"functions": {"locations": denied("cloudfunctions.locations.list"),
                                "list": denied("cloudfunctions.functions.list"),
                                "get": denied("cloudfunctions.functions.get")}, "build": denied("cloudbuild.builds.get"),
                  "logging": denied("logging.logEntries.list")},
    "throttled": {"functions": quota("cloudfunctions.googleapis.com"), "build": quota("cloudbuild.googleapis.com"),
                  "logging": quota("logging.googleapis.com")},
    "api-disabled": {"functions": disabled("Cloud Functions", "cloudfunctions.googleapis.com"),
                     "build": disabled("Cloud Build", "cloudbuild.googleapis.com"),
                     "logging": disabled("Cloud Logging", "logging.googleapis.com")},
    "build-denied": {"build": denied("cloudbuild.builds.get")},
}  # fmt: skip

# ---- the fake Google --------------------------------------------------------------------------


class World:
    def __init__(self, functions, faults=None, logs=True):
        self.functions, self.faults = functions, faults or {}
        self.unreachable = []
        self.logs = logs  # whether the functions have ERROR entries (a healthy world has none)
        # the regions the Cloud Functions locations API lists; None: those of the default world
        self.regions = None

    def fault(self, route, sub=None):
        """The (status, body) route `route` answers with, or None. A fault may differ by sub-route
        (a dict keyed by `sub`), as a denial names the permission of the call that was made."""
        fault = self.faults.get(route)
        return fault.get(sub) if isinstance(fault, dict) else fault


WORLD = World([])


def v1_view(fn):
    return fn if "status" in fn else None


def v2_of_gen1(fn):
    """What the v2 API shows for a 1st gen function (describe asks it for the generation)."""
    return {"name": fn["name"], "environment": "GEN_1", "state": "ACTIVE" if fn["status"] == "ACTIVE" else "FAILED",
            "updateTime": fn["updateTime"], "buildConfig": {"runtime": fn["runtime"], "entryPoint": fn["entryPoint"]}}  # fmt: skip


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        w = WORLD
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.split("?")[0] != "/v2/entries:list":
            return self.reply(404, {"error": {"code": 404, "message": f"no route {self.path}"}})
        fault = w.fault("logging")
        if fault:
            return self.reply(*fault)
        flt = body.get("filter", "")
        match = re.search(r'(?:function_name|service_name)="([^"]+)"', flt)
        entries = LOGS.get(match.group(1), []) if match and w.logs else []
        entries = [e for e in entries if e["resource"]["type"] in flt]
        limit = int(body.get("pageSize") or 1000)
        return self.reply(200, {"entries": entries[:limit]} if entries else {})

    def do_GET(self):
        w = WORLD
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        parts = [p for p in path.split("/") if p]
        # /v1/projects/P/locations: the regions the v1 API serves (gcloud filters its list by them)
        if parts[:1] in (["v1"], ["v2"]) and parts[-1] == "locations":
            fault = w.fault("functions", "locations")
            if fault:
                return self.reply(*fault)
            regions = w.regions or ["europe-west1", "us-central1", "us-east1"]
            return self.reply(
                200,
                {
                    "locations": [
                        {"name": f"projects/{P}/locations/{r}", "locationId": r} for r in regions
                    ]
                },
            )
        if parts[:1] == ["v1"] and "builds" in parts:
            fault = w.fault("build")
            if fault:
                return self.reply(*fault)
            build_id = parts[-1]
            for fn in w.functions:
                doc = build_doc(fn)
                if doc["id"] == build_id:
                    return self.reply(200, doc)
            return self.reply(404, error_body(404, "NOT_FOUND", "Requested entity was not found."))
        if parts[:1] in (["v1"], ["v2"]) and "functions" in parts:
            version = parts[0]
            listing = parts[-1] == "functions"
            fault = w.fault("functions", "list" if listing else "get")
            if fault:
                return self.reply(*fault)
            if listing:
                if version == "v1":
                    rows = [f for f in w.functions if "status" in f]
                    doc = {"functions": rows} if rows else {}
                else:
                    flt = (query.get("filter") or [""])[0]
                    rows = (
                        [f for f in w.functions if f.get("environment") == "GEN_2"]
                        if "GEN_2" in flt
                        else list(w.functions)
                    )
                    doc = {"functions": rows} if rows else {}
                if w.unreachable:
                    doc["unreachable"] = [f"projects/{P}/locations/{r}" for r in w.unreachable]
                return self.reply(200, doc)
            name = f"projects/{P}/locations/{parts[-3]}/functions/{parts[-1]}"
            fn = next((f for f in w.functions if f["name"] == name), None)
            if fn is None:
                return self.reply(
                    404,
                    error_body(
                        404, "NOT_FOUND", f"Function {parts[-1]} does not exist in {parts[-3]}"
                    ),
                )
            if version == "v2":
                return self.reply(200, fn if "environment" in fn else v2_of_gen1(fn))
            return self.reply(200, fn)
        return self.reply(404, {"error": {"code": 404, "message": f"no route {path}"}})


# ---- running the real gcloud ------------------------------------------------------------------


def _public_url(match):
    """The fake server's URL as the real service's: the host follows the API in the path."""
    path = match.group(1)
    if "/builds/" in path:
        return "https://cloudbuild.googleapis.com/" + path
    if path.startswith("v2/entries"):
        return "https://logging.googleapis.com/" + path
    return "https://cloudfunctions.googleapis.com/" + path


HTTP_ERROR = (
    (re.compile(r"http://127\.0\.0\.1:8099/(\S*?)(?=[?>\s])"), _public_url),
    (re.compile(r"'server': 'BaseHTTP[^']*'"), "'server': 'ESF'"),
    (re.compile(r"'date': '[^']*'"), "'date': 'Wed, 04 Nov 2026 00:00:00 GMT'"),
)
HINT = re.compile(r" ?This command is authenticated with an access token from the "
                  r"CLOUDSDK_AUTH_ACCESS_TOKEN environment variable\.?")  # fmt: skip


def gcloud(args, as_json=True, credentialed=True):
    """(rc, stdout, stderr) of the pinned gcloud against the fake server."""
    config = tempfile.mkdtemp(prefix="gcloud-config-")
    base = "http://127.0.0.1:8099/"
    env = {
        "PATH": os.environ["PATH"], "HOME": config, "TMPDIR": "/tmp", "CLOUDSDK_CONFIG": config,
        "CLOUDSDK_AUTH_ACCESS_TOKEN": TOKEN if credentialed else "",
        "CLOUDSDK_CORE_CHECK_GCE_METADATA": "false",
        "CLOUDSDK_CORE_ACCOUNT": SA if credentialed else "", "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
        "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "true", "CLOUDSDK_PYTHON": "python3",
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_CLOUDFUNCTIONS": base,
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_CLOUDBUILD": base,
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_LOGGING": base,
    }  # fmt: skip
    try:
        argv = [
            "gcloud",
            *args,
            "--billing-project=" + P,
            *(["--format=json"] if as_json else []),
            "--quiet",
        ]
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(config, ignore_errors=True)
    stderr = HINT.sub("", done.stderr)
    for pattern, replacement in HTTP_ERROR:
        stderr = pattern.sub(replacement, stderr)
    return done.returncode, done.stdout, stderr


FILES = {
    "regions": f"{SDK}/surface/functions/regions/list.py (v1 locations; --gen2: v2 locations)",
    "describe": f"{SDK}/surface/functions/describe.py and command_lib/functions/util.py (FunctionResourceCommand: the v2 API names the generation, GEN_2 prints the v2 format, else the v1 one)",
    "list": f'{SDK}/surface/functions/list.py (2nd gen from the v2 API with filter environment="GEN_2", 1st gen from v1; unreachable regions are only warned about)',
    "build": f"{SDK}/surface/builds/describe.py (--region: projects/<p>/locations/<r>/builds/<id>)",
    "logging": f"{SDK}/surface/logging/read.py and api_lib/logging/common.py (entries.list, --order, --limit, --freshness)",
    "token": "surface/auth/print_access_token.py",
}
REST = {
    "regions": "https://cloud.google.com/functions/docs/reference/rest/v2/projects.locations/list",
    "describe": "https://cloud.google.com/functions/docs/reference/rest/v2/projects.locations.functions/get",
    "list": "https://cloud.google.com/functions/docs/reference/rest/v2/projects.locations.functions/list",
    "build": "https://cloud.google.com/build/docs/api/reference/rest/v1/projects.locations.builds/get",
    "logging": "https://cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list",
    "token": "https://cloud.google.com/sdk/gcloud/reference/auth/print-access-token",
}
MON_REST = "https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list"


class Scenario:
    def __init__(self, name, world, new=True, header=None, unauthenticated=False):
        self.name, self.world = name, world
        self.unauthenticated = unauthenticated  # gcloud has no credential at all
        self.dir = os.path.join(OUT, name)
        self.calls = []
        self.header = header
        self.active = active(name)
        self.token_read = False
        if new and self.active:
            shutil.rmtree(self.dir, ignore_errors=True)
            os.makedirs(self.dir)

    def write(self, filename, text):
        if not self.active:
            return
        with open(os.path.join(self.dir, filename), "w", encoding="utf-8") as handle:
            handle.write(text)

    def gcloud_call(self, kind, args, filename, billing=True):
        global WORLD
        if not self.active:
            return None
        WORLD = self.world
        as_json = args[0] != "auth"
        rc, out, err = gcloud(args, as_json, not self.unauthenticated)
        argv = [*args, f"--billing-project={P}"] if billing else list(args)
        argv = stublib.normalise("gcloud", argv)  # the log filter's window is stored as <T>
        call = {"argv": argv, "rc": rc}
        if rc == 0 or out.strip():
            call["stdout"] = filename
            self.write(filename, out)
        if err.strip():  # also on success: gcloud warns about unreachable regions and exits 0
            call["stderr"] = err
        call["source"], call["cli_source"] = REST[kind], f"{SDK_VERSION}: {FILES[kind]}"
        self.calls.append(call)
        return rc, out, err

    def curl_call(self, flt, aggregation, filename, doc, rc=0, stderr=None):
        if not self.active:
            return
        start, end = metrics.window(NOW, SINCE)
        url = monitoring.url(P, metrics.iso(start), metrics.iso(end), flt, aggregation)
        url = "&".join(
            a if not a.startswith("interval.") else a.split("=")[0] + "=<T>" for a in url.split("&")
        )
        call = {"tool": "curl", "argv": ["-sS", "--fail-with-body", "--max-redirs", "0", "-H", f"@{monitoring.HEADER_FILE}", url], "rc": rc}  # fmt: skip
        call["stdout"] = filename
        self.write(filename, json.dumps(doc, indent=2, sort_keys=True) + "\n")
        if stderr:
            call["stderr"] = stderr
        call["source"] = MON_REST
        call["cli_source"] = (
            "curl 8.14.1 (runtime image): -sS --fail-with-body keeps the error body on stdout, rc 22"
        )
        self.calls.append(call)

    # -- the plugin's calls ------------------------------------------------------------------
    def list_call(self):
        self.gcloud_call("list", ["functions", "list", f"--project={P}"], "functions-list.json")

    def regions_calls(self):
        """`functions regions list` (v1 locations) and `--gen2` (v2 locations): discover reads the
        regions that exist, to sweep an empty one."""
        self.gcloud_call(
            "regions", ["functions", "regions", "list", f"--project={P}"], "regions-v1.json"
        )
        self.gcloud_call(
            "regions",
            ["functions", "regions", "list", f"--project={P}", "--gen2"],
            "regions-v2.json",
        )

    def describe_call(self, fn):
        _, region, name = stublib_name(fn)
        self.gcloud_call(
            "describe",
            ["functions", "describe", name, f"--region={region}", f"--project={P}"],
            f"describe-{region}-{name}.json",
        )

    def build_call(self, fn):
        _, region, name = stublib_name(fn)
        config = fn.get("buildConfig")
        bid = (config["build"] if config else fn["buildName"]).rsplit("/", 1)[1]
        self.gcloud_call(
            "build",
            ["builds", "describe", bid, f"--region={region}", f"--project={P}"],
            f"build-{region}-{name}.json",
        )

    def logs_call(self, fn, limit=50):
        _, region, name = stublib_name(fn)
        gen = 2 if fn.get("environment") == "GEN_2" else 1
        start, end = plugin_logs.window(NOW, SINCE)
        flt = plugin_logs.log_filter(gen, name, region, name, start, end)
        self.gcloud_call("logging", ["logging", "read", flt, f"--project={P}",
                                     f"--limit={limit + 1}", "--order=desc"], f"logs-{name}.json")  # fmt: skip

    def metrics_calls(self, fn, docs=None, faults=None):
        """The token and the four Monitoring queries of the function. `docs` maps a query to its
        reply; `faults` maps a query to (body, curl stderr) for a failing one."""
        _, region, name = stublib_name(fn)
        gen = 2 if fn.get("environment") == "GEN_2" else 1
        sp = metrics.spec_for(gen)
        series_name = name if gen == 1 else name
        if not self.token_read:  # one token for the run, like the plugin
            self.gcloud_call("token", ["auth", "print-access-token"], "token.txt")
            self.token_read = True
        for query, metric, aggregation in metrics.queries(sp, SINCE):
            flt = metrics.metric_filter(sp, metric, series_name, region)
            if faults and query in faults:
                body, err = faults[query]
                self.curl_call(
                    flt, aggregation, f"mon-{name}-{query}.json", body, rc=22, stderr=err
                )
            else:
                self.curl_call(
                    flt, aggregation, f"mon-{name}-{query}.json", (docs or {}).get(query, {})
                )

    def save(self):
        if not self.active:
            return
        lines = self.header or [
            f"# Canned answers of scenario '{self.name}', written by tests/stub/capture/capture_functions.py from the",
            f"# real {SDK_VERSION} in the runtime image (see ../../README.md). Project, functions and members are invented.",
            "# Every value is one line of JSON.",
        ]
        lines = list(lines)
        for call in self.calls:
            first = True
            for key in ("tool", "argv", "rc", "stdout", "stderr", "source", "cli_source"):
                if key in call:
                    lines.append(("- " if first else "  ") + f"{key}: {json.dumps(call[key])}")
                    first = False
        self.write("calls.yaml", "\n".join(lines) + "\n")


def stublib_name(fn):
    parts = fn["name"].split("/")
    return parts[1], parts[3], parts[5]


def curl_error(status, message, reason=None, host="monitoring.googleapis.com"):
    codes = {403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED"}
    return error_body(
        status, codes[status], message, reason, host
    ), f"curl: (22) The requested URL returned error: {status}\n"


def world_scenario(name, functions, key, faults=None, logs=True):
    """A scenario in which every task works on every function of `functions`."""
    sc = Scenario(name, World(functions, faults, logs))
    sc.list_call()
    for fn in functions:
        fname = stublib_name(fn)[2]
        sc.describe_call(fn)
        sc.build_call(fn)
        sc.logs_call(fn)
        sc.metrics_calls(fn, METRICS[key][fname])
    return sc


def failing_scenario(name, kind):
    """Every plugin call that reaches the fault fails; the target is orders-api."""
    sc = Scenario(name, World(BROKEN, FAULTS[kind]))
    sc.list_call()
    sc.describe_call(ORDERS)
    sc.build_call(ORDERS)
    sc.logs_call(ORDERS)
    return sc


def append_functions_list(name, world, unauthenticated=False):
    """Replace the `functions list` call of an existing (bucket pilot) scenario with this world's."""
    if not active(name):
        return
    directory = os.path.join(OUT, name)
    path = os.path.join(directory, "calls.yaml")
    text = open(path, encoding="utf-8").read()
    header = [line for line in text.splitlines() if line.startswith("#")]
    calls = [
        c
        for c in stublib.parse_calls(text)
        if c["argv"][:2] != ["functions", "list"]
        and c["argv"][:3] != ["functions", "regions", "list"]
    ]
    sc = Scenario(name, world, new=False, header=header, unauthenticated=unauthenticated)
    sc.calls = calls
    sc.list_call()
    if "functions" not in world.faults and not unauthenticated:  # read only after a good list
        sc.regions_calls()
    sc.save()


def discover_scenario(name, world, buckets_from="healthy"):
    """The bucket pilot's bucket-list answer (of `buckets_from`) and project answer (healthy), then
    this world's functions list: what gcp-base/discover calls."""
    if not active(name):
        return

    def calls_of(scenario, wanted):
        text = open(os.path.join(OUT, scenario, "calls.yaml")).read()
        return [(scenario, c) for c in stublib.parse_calls(text) if c["argv"][:3] == wanted]

    calls = calls_of(buckets_from, ["storage", "buckets", "list"]) + calls_of(
        "healthy", ["projects", "describe", P]
    )
    sc = Scenario(name, world)
    for scenario, call in calls:
        if call.get("stdout"):
            shutil.copy(
                os.path.join(OUT, scenario, call["stdout"]), os.path.join(sc.dir, call["stdout"])
            )
    sc.calls = [c for _, c in calls]
    sc.list_call()
    if "functions" not in world.faults:
        sc.regions_calls()
    sc.save()


def main():
    server = HTTPServer(("127.0.0.1", 8099), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    scenarios = []

    scenarios.append(world_scenario("functions-healthy", HEALTHY, "healthy", logs=False))
    scenarios.append(world_scenario("functions-broken", BROKEN, "broken"))

    # the failing ones: functions API, Cloud Build and Cloud Logging all answer with the same fault
    for kind in ("forbidden", "throttled", "api-disabled"):
        scenarios.append(failing_scenario(f"functions-{kind}", kind))
    # the function is there, Cloud Build is denied: the state stands, the build is null (partial)
    sc = Scenario("functions-build-denied", World(BROKEN, FAULTS["build-denied"]))
    sc.list_call()
    sc.describe_call(CHECKOUT)
    sc.build_call(CHECKOUT)
    scenarios.append(sc)

    # a function that does not exist: describe and the list agree
    sc = Scenario("functions-not-found", World(HEALTHY))
    sc.list_call()
    sc.gcloud_call(
        "describe",
        ["functions", "describe", "nope", "--region=us-central1", f"--project={P}"],
        "describe-nope.json",
    )
    scenarios.append(sc)

    # a project with no function at all: the list is empty ("[]"), not an error
    sc = Scenario("functions-empty", World([]))
    sc.list_call()
    scenarios.append(sc)

    # the list worked but gcloud could not reach a region: it exits 0 and warns
    world = World(HEALTHY)
    world.unreachable = ["asia-east1", "southamerica-east1"]
    sc = Scenario("functions-unreachable", world)
    sc.list_call()
    sc.build_call(ORDERS)
    scenarios.append(sc)

    # the same id in two regions
    sc = Scenario("functions-split", World(SPLIT))
    sc.list_call()
    for fn in SPLIT[:2]:
        sc.describe_call(fn)
        sc.build_call(fn)
    scenarios.append(sc)

    # secrets in every place a task reads (env values, secret references, build steps and failure
    # detail, state messages, log lines, structured payloads): none may reach an output
    sc = Scenario("functions-leaky", World(LEAKY_WORLD))
    sc.list_call()
    for fn in LEAKY_WORLD:
        sc.describe_call(fn)
        sc.build_call(fn)
        sc.logs_call(fn)
    scenarios.append(sc)

    # the function is readable but this identity may not read its logs / metrics
    sc = Scenario(
        "functions-logging-denied", World(BROKEN, {"logging": denied("logging.logEntries.list")})
    )
    sc.describe_call(ORDERS)
    sc.logs_call(ORDERS)
    scenarios.append(sc)

    sc = Scenario("functions-monitoring-denied", World(HEALTHY))
    sc.describe_call(ORDERS)
    body, err = curl_error(
        403,
        "Permission monitoring.timeSeries.list denied (or the resource may not exist).",
        None,
    )
    sc.metrics_calls(ORDERS, faults={q: (body, err) for q in ("executions", "p50", "p95", "p99")})
    scenarios.append(sc)

    # Cloud Monitoring: no series for orders-api (idle), the API disabled, one query throttled
    sc = Scenario("functions-idle", World(HEALTHY))
    sc.describe_call(ORDERS)
    sc.metrics_calls(ORDERS, metric_doc({}, 0, 0, 0, 2))
    scenarios.append(sc)

    sc = Scenario("functions-monitoring-disabled", World(HEALTHY))
    sc.describe_call(ORDERS)
    body, err = curl_error(
        403, disabled_message("Cloud Monitoring", "monitoring.googleapis.com"), "SERVICE_DISABLED"
    )
    sc.metrics_calls(ORDERS, faults={q: (body, err) for q in ("executions", "p50", "p95", "p99")})
    scenarios.append(sc)

    sc = Scenario("functions-partial-metrics", World(HEALTHY))
    sc.describe_call(ORDERS)
    body, err = curl_error(
        429,
        "Quota exceeded for quota metric 'Time series queries' and limit 'Time series queries per minute' of service 'monitoring.googleapis.com'",
        "RATE_LIMIT_EXCEEDED",
    )
    sc.metrics_calls(
        ORDERS, METRICS["healthy"]["orders-api"], faults={"p95": (body, err), "p99": (body, err)}
    )
    scenarios.append(sc)

    # discover: the bucket pilot's scenarios get the functions list; two more for a failing one
    for sc in scenarios:
        sc.save()
    append_functions_list("healthy", World(HEALTHY))
    append_functions_list("broken", World(BROKEN))
    append_functions_list("project-denied", World(BROKEN))
    # the bucket pilot's failing scenarios: every API fails the same way, so the functions list does
    append_functions_list("forbidden", World(BROKEN, FAULTS["forbidden"]))
    append_functions_list("throttled", World(BROKEN, FAULTS["throttled"]))
    append_functions_list("api-disabled", World(BROKEN, FAULTS["api-disabled"]))
    append_functions_list("unauthenticated", World(BROKEN), unauthenticated=True)
    discover_scenario("functions-discover-denied", World(BROKEN, FAULTS["forbidden"]))
    discover_scenario("functions-discover-disabled", World(BROKEN, FAULTS["api-disabled"]))
    unreachable = World(HEALTHY)
    unreachable.unreachable = ["asia-east1", "southamerica-east1"]
    discover_scenario("functions-discover-unreachable", unreachable)
    discover_scenario("functions-discover-split", World(SPLIT))
    # the last function of europe-north1 was deleted: the region is listed, empty and swept
    deleted = World(HEALTHY)
    deleted.regions = ["europe-north1", "europe-west1", "us-central1", "us-east1"]
    discover_scenario("functions-discover-deleted", deleted)
    # the bucket list is denied and the functions list works: the other half of discover stands
    discover_scenario("functions-discover-buckets-denied", World(BROKEN), buckets_from="forbidden")


if __name__ == "__main__":
    main()
