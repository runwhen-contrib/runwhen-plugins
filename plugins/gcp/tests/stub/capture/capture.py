"""Writes tests/stub/scenarios/*: the canned answers of the gcloud/curl stub, produced by the REAL
gcloud of the pinned runtime image.

Run it through capture.sh, never directly on a workstation: it runs inside
`rwp-runtime-gcp:dev` with `--network none`. It starts a local HTTP server that speaks the REST
shapes of the Cloud Storage JSON API and Resource Manager v1 (the world below: invented project,
buckets and members), points gcloud at it with CLOUDSDK_API_ENDPOINT_OVERRIDES_*, runs each
command the plugin issues and stores what gcloud really printed: its JSON formatting, its
`--raw` rendering of timestamps, its `du` padding, its error lines and exit codes. Nothing here
contacts Google, and no real credential exists: the access token is the fake one of identities.json.

The one answer not produced by gcloud is the Cloud Monitoring reply to the curl call (curl is not
a Google tool): it is written from the REST reference, with curl's own `--fail-with-body` error
behaviour for the failures.

Each calls.yaml entry cites `source` (the REST reference its shape follows) and `cli_source` (the
file of the pinned SDK that formats the answer, or the curl behaviour)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(
    0, "/plugin-lib"
)  # the gcp-bucket-health lib: the Monitoring URL is built by the plugin's own code
from gcp_bucket_health import monitoring  # noqa: E402

OUT = "/stub/scenarios"
IDENT = json.load(open("/stub/identities.json"))
TOKEN = IDENT["fakeToken"]
P = "shop-prod-1234"
NUMBER = "123456789012"
SA = f"rw-reader@{P}.iam.gserviceaccount.com"
T0 = "2025-03-14T09:21:05.123Z"
SDK = "googlecloudsdk"
STORAGE_REST = "https://cloud.google.com/storage/docs/json_api/v1/buckets"
MON_REST = "https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list"

# ---- the world: REST shapes ------------------------------------------------------------------


def acl_entry(bucket, entity, role, kind="bucketAccessControl"):
    return {"kind": f"storage#{kind}", "bucket": bucket, "entity": entity, "role": role, "etag": "CAM=",
            "id": f"{bucket}/{entity}"}  # fmt: skip


def bucket(name, *, storage_class="STANDARD", location="US-CENTRAL1", location_type="region",
           versioning=False, prevention="inherited", uniform=True, labels=None, lifecycle=0,
           retention=None, kms=None, acl=None):  # fmt: skip
    row = {
        "kind": "storage#bucket",
        "selfLink": f"https://www.googleapis.com/storage/v1/b/{name}",
        "id": name,
        "name": name,
        "projectNumber": NUMBER,
        "metageneration": "3",
        "location": location,
        "storageClass": storage_class,
        "etag": "CAM=",
        "timeCreated": T0,
        "updated": "2026-09-01T12:00:00.000Z",
        "locationType": location_type,
        "rpo": "DEFAULT",
        "softDeletePolicy": {"retentionDurationSeconds": "604800", "effectiveTime": T0},
        "iamConfiguration": {
            "bucketPolicyOnly": {"enabled": uniform},
            "uniformBucketLevelAccess": {"enabled": uniform},
            "publicAccessPrevention": prevention,
        },
    }
    if uniform:
        locked = {"lockedTime": "2025-06-12T09:21:05.123Z"}
        row["iamConfiguration"]["bucketPolicyOnly"].update(locked)
        row["iamConfiguration"]["uniformBucketLevelAccess"].update(locked)
    if versioning:
        row["versioning"] = {"enabled": True}
    if labels:
        row["labels"] = labels
    if lifecycle:
        row["lifecycle"] = {"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30 * (i + 1)}}
                                     for i in range(lifecycle)]}  # fmt: skip
    if retention:
        row["retentionPolicy"] = {
            "retentionPeriod": str(retention),
            "effectiveTime": T0,
            "isLocked": False,
        }
    if kms:
        row["encryption"] = {"defaultKmsKeyName": kms}
    if acl is not None:  # the FULL projection lists ACLs for a bucket that has them
        row["acl"] = acl
        row["defaultObjectAcl"] = [
            acl_entry(name, f"project-owners-{NUMBER}", "OWNER", "objectAccessControl"),
            acl_entry(name, f"project-viewers-{NUMBER}", "READER", "objectAccessControl"),
        ]
    return row


def legacy_acl(name, public):
    entries = [acl_entry(name, f"project-owners-{NUMBER}", "OWNER"),
               acl_entry(name, f"project-editors-{NUMBER}", "OWNER"),
               acl_entry(name, f"project-viewers-{NUMBER}", "READER")]  # fmt: skip
    return entries + ([acl_entry(name, "allUsers", "READER")] if public else [])


def policy(extra=()):
    """What `buckets get-iam-policy` asks for: fields=bindings,etag (client.py), so no kind,
    resourceId or version."""
    return {
        "bindings": [
            {"role": "roles/storage.legacyBucketOwner",
             "members": [f"projectEditor:{P}", f"projectOwner:{P}"]},
            {"role": "roles/storage.legacyBucketReader", "members": [f"projectViewer:{P}"]},
            {"role": "roles/storage.objectViewer", "members": [f"serviceAccount:{SA}"]},
            *extra,
        ],
        "etag": "CAI=",
    }  # fmt: skip


KMS = f"projects/{P}/locations/us/keyRings/shop/cryptoKeys/backups"
HEALTHY = {
    "shop-assets": bucket("shop-assets", versioning=True, prevention="enforced", lifecycle=1,
                          labels={"env": "prod", "team": "web"}),
    "shop-backups": bucket("shop-backups", storage_class="COLDLINE", location="US",
                           location_type="multi-region", versioning=True, prevention="enforced",
                           lifecycle=1, kms=KMS),
    "shop-logs": bucket("shop-logs", storage_class="NEARLINE", lifecycle=2, retention=2592000),
}  # fmt: skip
BROKEN = {
    **HEALTHY,
    "shop-public-assets": bucket("shop-public-assets"),
    "shop-legacy": bucket("shop-legacy", uniform=False, acl=legacy_acl("shop-legacy", True)),
    "shop-archive": bucket("shop-archive", prevention="enforced"),
}
POLICIES = {name: policy() for name in BROKEN}
POLICIES["shop-public-assets"] = policy(
    [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}]
)
POLICIES["shop-legacy"] = policy([{
    "role": "roles/storage.objectViewer", "members": ["allAuthenticatedUsers"],
    "condition": {"title": "until-2027", "expression": "request.time < timestamp('2027-01-01T00:00:00Z')"},
}])  # fmt: skip
GIB = 1024**3
TIB = 1024**4
# bytes by object type, as Cloud Monitoring reports them (storage/v2/total_bytes)
SIZES = {
    "shop-assets": {"live-object": 660602880, "noncurrent-object": 73400320},
    "shop-backups": {"live-object": 193273528320, "noncurrent-object": 21474836480},
    "shop-logs": {"live-object": 53687091200},
    "shop-public-assets": {"live-object": GIB},
    "shop-legacy": {"live-object": 2 * GIB},
    "shop-archive": {"live-object": int(1.5 * TIB)},
}
PROJECT_V1 = {"projectNumber": NUMBER, "projectId": P, "lifecycleState": "ACTIVE", "name": "Shop Production",
              "createTime": "2024-01-15T10:00:00.123Z", "labels": {"env": "prod"},
              "parent": {"type": "organization", "id": IDENT["projectNumbers"][1]}}  # fmt: skip

# ---- faults: REST error bodies ----------------------------------------------------------------


def storage_error(code, message, reason, status=None, extra=None):
    err = {
        "code": code,
        "message": message,
        "errors": [{"message": message, "domain": "global", "reason": reason}],
    }
    if status:
        err["status"] = status
    err.update(extra or {})
    return {"error": err}


def denied(permission, what="Google Cloud Storage bucket"):
    return (403, storage_error(403, f"{SA} does not have {permission} access to the {what}. "
                               f"Permission '{permission}' denied on resource (or it may not exist).", "forbidden"))  # fmt: skip


QUOTA = (429, storage_error(
    429, "Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute per user' "
    f"of service 'storage.googleapis.com' for consumer 'project_number:{NUMBER}'.", "rateLimitExceeded",
    "RESOURCE_EXHAUSTED"))  # fmt: skip
DISABLED_MSG = (
    f"Cloud Storage API has not been used in project {NUMBER} before or it is disabled. Enable it by "
    f"visiting https://console.developers.google.com/apis/api/storage.googleapis.com/overview?project={NUMBER} "
    "then retry. If you enabled this API recently, wait a few minutes for the action to propagate to our "
    "systems and retry."
)
DISABLED = (403, {"error": {
    "code": 403, "message": DISABLED_MSG, "status": "PERMISSION_DENIED",
    "errors": [{"message": DISABLED_MSG, "domain": "usageLimits", "reason": "accessNotConfigured",
                "extendedHelp": "https://console.developers.google.com"}],
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com",
                 "metadata": {"consumer": f"projects/{NUMBER}", "service": "storage.googleapis.com"}}],
}})  # fmt: skip
PROJECT_DENIED = (403, {"error": {"code": 403, "message": "The caller does not have permission",
                                  "status": "PERMISSION_DENIED"}})  # fmt: skip
PROJECT_DISABLED = (403, {"error": {
    "code": 403, "status": "PERMISSION_DENIED",
    "message": DISABLED_MSG.replace("Cloud Storage API", "Cloud Resource Manager API").replace(
        "storage.googleapis.com", "cloudresourcemanager.googleapis.com"),
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com", "metadata": {"consumer": f"projects/{NUMBER}",
                                                          "service": "cloudresourcemanager.googleapis.com"}}],
}})  # fmt: skip
PROJECT_QUOTA = (429, {"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": "Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute per user' "
               f"of service 'cloudresourcemanager.googleapis.com' for consumer 'project_number:{NUMBER}'.",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED",
                 "domain": "googleapis.com", "metadata": {"consumer": f"projects/{NUMBER}",
                                                          "service": "cloudresourcemanager.googleapis.com"}}],
}})  # fmt: skip
NOT_FOUND = (404, storage_error(404, "The specified bucket does not exist.", "notFound"))

# ---- the fake Google --------------------------------------------------------------------------


class World:
    """buckets: {name: REST bucket}; faults: {route: (status, body)}; a route is `list`, `project`,
    `describe`, `iam`, `objects`, optionally `route:bucket` for one bucket."""

    def __init__(self, buckets, faults=None, objects=None):
        self.buckets, self.faults, self.objects = buckets, faults or {}, objects or {}

    def fault(self, route, name=None):
        return self.faults.get(f"{route}:{name}") or self.faults.get(route)


WORLD = World({})


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

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        w = WORLD
        if parts[:3] == ["storage", "v1", "b"]:
            name = parts[3] if len(parts) > 3 else None
            tail = parts[4] if len(parts) > 4 else None
            route = (
                "list" if name is None else {None: "describe", "iam": "iam", "o": "objects"}[tail]
            )
            fault = w.fault(route, name)
            if fault:
                return self.reply(*fault)
            if name is None:
                return self.reply(
                    200, {"kind": "storage#buckets", "items": list(w.buckets.values())}
                )
            if name not in w.buckets:
                return self.reply(*NOT_FOUND)
            if tail is None:
                return self.reply(200, w.buckets[name])
            if tail == "iam":
                return self.reply(200, POLICIES[name])
            size = w.objects.get(name, 0)
            items = [
                {
                    "kind": "storage#object",
                    "name": "data.bin",
                    "bucket": name,
                    "size": str(size),
                    "generation": "1",
                }
            ]
            return self.reply(
                200,
                {"kind": "storage#objects", "items": items}
                if size
                else {"kind": "storage#objects"},
            )
        if parts[:2] == ["v1", "projects"]:
            fault = w.fault("project")
            return self.reply(*(fault or (200, PROJECT_V1)))
        return self.reply(404, {"error": {"code": 404, "message": f"no route {path}"}})


# ---- running the real gcloud ------------------------------------------------------------------


def gcloud(args, token, as_json):
    """(rc, stdout, stderr) of the pinned gcloud against the fake server."""
    config = tempfile.mkdtemp(prefix="gcloud-config-")
    env = {
        "PATH": os.environ["PATH"], "HOME": config, "TMPDIR": "/tmp", "CLOUDSDK_CONFIG": config,
        "CLOUDSDK_AUTH_ACCESS_TOKEN": token, "CLOUDSDK_CORE_CHECK_GCE_METADATA": "false",
        # the account only names the identity in gcloud's messages, as a key's e-mail would
        "CLOUDSDK_CORE_ACCOUNT": SA if token else "",
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1", "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
        "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "true", "CLOUDSDK_PYTHON": "python3",
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE": "http://127.0.0.1:8099/storage/v1/",
        "CLOUDSDK_API_ENDPOINT_OVERRIDES_CLOUDRESOURCEMANAGER": "http://127.0.0.1:8099/",
    }  # fmt: skip
    try:
        argv = [
            "gcloud",
            *args,
            f"--billing-project={P}",
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


# The one edit to what gcloud printed: gcloud appends this sentence to an authorization error when
# the credential is an access token from the environment, as it is here; the plugin's gcloud child
# has a key (or the metadata server) and never prints it.
HINT = re.compile(r" ?This command is authenticated with an access token from the "
                  r"CLOUDSDK_AUTH_ACCESS_TOKEN environment variable\.?")  # fmt: skip
# gcloud's Resource Manager client prints a 429 as a bare HttpError with the request URL and the
# response headers; the fake server's address, banner and date are replaced by Google's.
HTTP_ERROR = (
    (re.compile(r"http://127\.0\.0\.1:8099/"), "https://cloudresourcemanager.googleapis.com/"),
    (re.compile(r"'server': 'BaseHTTP[^']*'"), "'server': 'ESF'"),
    (re.compile(r"'date': '[^']*'"), "'date': 'Wed, 04 Nov 2026 00:00:00 GMT'"),
)  # fmt: skip
SDK_FILES = {
    "describe": f"{SDK}/command_lib/storage/resources/resource_util.py (get_display_dict_for_resource, --raw) "
    "and surface/storage/buckets/describe.py (FULL projection)",
    "list": f"{SDK}/command_lib/storage/resources/resource_util.py (get_display_dict_for_resource, --raw) "
    "and surface/storage/buckets/list.py (FULL projection)",
    "iam": f"surface/storage/buckets/get_iam_policy.py and {SDK}/api_lib/storage/gcs_json/client.py "
    "(get_bucket_iam_policy: fields=bindings,etag)",
    "du": f"{SDK}/command_lib/storage/du_command_util.py (DuExecutor: '{{size:<13}}{{url}}')",
    "project": f"surface/projects/describe.py and {SDK}/api_lib/cloudresourcemanager/projects_api.py (Resource Manager v1)",
    "token": "surface/auth/print_access_token.py",
}
REST = {
    "describe": f"{STORAGE_REST}/get",
    "list": f"{STORAGE_REST}/list",
    "iam": f"{STORAGE_REST}/getIamPolicy",
    "du": "https://cloud.google.com/storage/docs/json_api/v1/objects/list",
    "project": "https://cloud.google.com/resource-manager/reference/rest/v1/projects/get",
    "token": "https://cloud.google.com/sdk/gcloud/reference/auth/print-access-token",
}
SDK_VERSION = "gcloud 592.0.0"


class Scenario:
    def __init__(self, name, world, token=TOKEN):
        self.name, self.world, self.token = name, world, token
        self.dir = os.path.join(OUT, name)
        self.calls = []
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)

    def write(self, filename, text):
        with open(os.path.join(self.dir, filename), "w", encoding="utf-8") as handle:
            handle.write(text)

    def gcloud_call(self, kind, args, filename):
        """Run `gcloud <args> --billing-project --format=json --quiet` (no --format for the
        commands that print text) and store what it printed."""
        global WORLD
        WORLD = self.world
        as_json = args[0] != "auth" and args[:2] != ["storage", "du"]
        rc, out, err = gcloud(args, self.token, as_json)
        call = {"argv": [*args, f"--billing-project={P}"], "rc": rc}
        if rc == 0 or out.strip():  # a list verb prints [] before its error
            call["stdout"] = filename
            self.write(filename, out)
        if rc != 0:
            call["stderr"] = err
        call["source"], call["cli_source"] = REST[kind], f"{SDK_VERSION}: {SDK_FILES[kind]}"
        self.calls.append(call)

    def curl_call(self, bucket_name, filename, rc=0, body=None, stderr=None):
        when = datetime(2026, 11, 4, 1, 0, 0, tzinfo=timezone.utc)
        url = monitoring.url(P, when, bucket_name)
        url = "&".join(
            a if not a.startswith("interval.") else a.split("=")[0] + "=<T>" for a in url.split("&")
        )
        call = {"tool": "curl", "argv": ["-sS", "--fail-with-body", "--max-redirs", "0", "-H",
                                         f"@{monitoring.HEADER_FILE}", url], "rc": rc}  # fmt: skip
        call["stdout"] = filename
        self.write(filename, json.dumps(body, indent=2, sort_keys=True) + "\n")
        if stderr:
            call["stderr"] = stderr
        call["source"] = MON_REST
        call["cli_source"] = (
            "curl 8.14.1 (runtime image): -sS --fail-with-body keeps the error body on stdout, rc 22"
        )
        self.calls.append(call)

    def save(self):
        lines = [
            f"# Canned answers of scenario '{self.name}', written by tests/stub/capture/capture.sh from the real",
            f"# {SDK_VERSION} in the runtime image (see ../../README.md). Project, buckets and members are invented.",
            "# Every value is one line of JSON.",
        ]
        for call in self.calls:
            first = True
            for key in ("tool", "argv", "rc", "stdout", "stderr", "source", "cli_source"):
                if key in call:
                    lines.append(("- " if first else "  ") + f"{key}: {json.dumps(call[key])}")
                    first = False
        self.write("calls.yaml", "\n".join(lines) + "\n")


def monitoring_doc(names):
    series = []
    for name in names:
        for kind, size in SIZES[name].items():
            cls = BROKEN[name]["storageClass"]
            series.append({
                "metric": {"labels": {"storage_class": cls, "type": kind}, "type": monitoring.METRIC},
                "resource": {"type": "gcs_bucket", "labels": {
                    "bucket_name": name, "location": BROKEN[name]["location"].lower(), "project_id": P}},
                "metricKind": "GAUGE", "valueType": "DOUBLE",
                "points": [  # newest first, as the API returns them
                    {"interval": {"startTime": "2026-11-04T00:00:00Z", "endTime": "2026-11-04T01:00:00Z"},
                     "value": {"doubleValue": float(size)}},
                    {"interval": {"startTime": "2026-11-03T23:00:00Z", "endTime": "2026-11-04T00:00:00Z"},
                     "value": {"doubleValue": float(size)}},
                ],
            })  # fmt: skip
    return {"timeSeries": series, "unit": "By"}


def curl_error(status, message, reason=None):
    err = {"code": status, "message": message,
           "status": {403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED"}[status]}  # fmt: skip
    if reason:
        err["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                           "domain": "googleapis.com", "metadata": {
                               "consumer": f"projects/{NUMBER}", "service": "monitoring.googleapis.com"}}]  # fmt: skip
    return {"error": err}, f"curl: (22) The requested URL returned error: {status}\n"


def healthy_like(name, buckets, with_curl=True):
    world = World(buckets)
    sc = Scenario(name, world)
    sc.gcloud_call("project", ["projects", "describe", P], "project.json")
    sc.gcloud_call(
        "list", ["storage", "buckets", "list", f"--project={P}", "--raw"], "buckets-list.json"
    )
    for b in buckets:
        sc.gcloud_call(
            "describe", ["storage", "buckets", "describe", f"gs://{b}", "--raw"], f"bucket-{b}.json"
        )
        sc.gcloud_call(
            "iam", ["storage", "buckets", "get-iam-policy", f"gs://{b}"], f"iam-{b}.json"
        )
    sc.gcloud_call("token", ["auth", "print-access-token"], "token.txt")
    sc.curl_call(None, "monitoring-project.json", body=monitoring_doc(list(buckets)))
    for b in buckets:
        sc.curl_call(b, f"monitoring-{b}.json", body=monitoring_doc([b]))
    return sc


def failing(name, faults, token=TOKEN, buckets=BROKEN, project_fault=None, names=None):
    """Every command of the plugin fails in `faults`' way."""
    world = World(buckets, faults)
    sc = Scenario(name, world, token)
    sc.gcloud_call("project", ["projects", "describe", P], "project.json")
    sc.gcloud_call(
        "list", ["storage", "buckets", "list", f"--project={P}", "--raw"], "buckets-list.json"
    )
    for b in names or buckets:
        sc.gcloud_call(
            "describe", ["storage", "buckets", "describe", f"gs://{b}", "--raw"], f"bucket-{b}.json"
        )
        sc.gcloud_call(
            "iam", ["storage", "buckets", "get-iam-policy", f"gs://{b}"], f"iam-{b}.json"
        )
    return sc


def main():
    server = HTTPServer(("127.0.0.1", 8099), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    scenarios = []

    scenarios.append(healthy_like("healthy", HEALTHY))
    scenarios.append(healthy_like("broken", BROKEN))

    all_denied = {"list": denied("storage.buckets.list", "Google Cloud project"), "describe": denied("storage.buckets.get"),
                  "iam": denied("storage.buckets.getIamPolicy"), "objects": denied("storage.objects.list"),
                  "project": PROJECT_DENIED}  # fmt: skip
    scenarios.append(failing("forbidden", all_denied))
    scenarios.append(
        failing(
            "throttled",
            {
                "list": QUOTA,
                "describe": QUOTA,
                "iam": QUOTA,
                "objects": QUOTA,
                "project": PROJECT_QUOTA,
            },
        )
    )
    scenarios.append(failing("api-disabled", {"list": DISABLED, "describe": DISABLED, "iam": DISABLED,
                                              "objects": DISABLED, "project": PROJECT_DISABLED}))  # fmt: skip
    scenarios.append(failing("unauthenticated", {}, token=""))

    # a bucket that does not exist: the only real 404 (a missing project answers 403)
    sc = Scenario("not-found", World({}))
    sc.gcloud_call(
        "describe", ["storage", "buckets", "describe", "gs://nope", "--raw"], "bucket-nope.json"
    )
    sc.gcloud_call("iam", ["storage", "buckets", "get-iam-policy", "gs://nope"], "iam-nope.json")
    scenarios.append(sc)

    # buckets list works, the project resource is denied: the run is partial
    sc = Scenario("project-denied", World(BROKEN, {"project": PROJECT_DENIED}))
    sc.gcloud_call("project", ["projects", "describe", P], "project.json")
    sc.gcloud_call(
        "list", ["storage", "buckets", "list", f"--project={P}", "--raw"], "buckets-list.json"
    )
    scenarios.append(sc)

    # one bucket's IAM read is throttled while the rest answer
    sc = Scenario("partial-throttled", World(BROKEN, {"iam:shop-logs": QUOTA}))
    sc.gcloud_call(
        "list", ["storage", "buckets", "list", f"--project={P}", "--raw"], "buckets-list.json"
    )
    for b in BROKEN:
        sc.gcloud_call(
            "iam", ["storage", "buckets", "get-iam-policy", f"gs://{b}"], f"iam-{b}.json"
        )
    scenarios.append(sc)

    # Monitoring has no series for a bucket: bucket-size falls back to du (a single-bucket target)
    big = {"shop-assets": 10**12 + 5}
    sc = Scenario("du-fallback", World({"shop-assets": HEALTHY["shop-assets"]}, objects=big))
    sc.gcloud_call(
        "describe",
        ["storage", "buckets", "describe", "gs://shop-assets", "--raw"],
        "bucket-shop-assets.json",
    )
    sc.gcloud_call("token", ["auth", "print-access-token"], "token.txt")
    sc.curl_call("shop-assets", "monitoring-shop-assets.json", body={})
    sc.gcloud_call("du", ["storage", "du", "--summarize", "gs://shop-assets"], "du-shop-assets.txt")
    scenarios.append(sc)

    # the project is fine but the Cloud Monitoring API is disabled
    sc = Scenario("monitoring-disabled", World(BROKEN))
    sc.gcloud_call(
        "list", ["storage", "buckets", "list", f"--project={P}", "--raw"], "buckets-list.json"
    )
    sc.gcloud_call("token", ["auth", "print-access-token"], "token.txt")
    body, err = curl_error(
        403,
        DISABLED_MSG.replace("Cloud Storage API", "Cloud Monitoring API").replace(
            "storage.googleapis.com", "monitoring.googleapis.com"
        ),
        "SERVICE_DISABLED",
    )
    sc.curl_call(None, "monitoring-project.json", rc=22, body=body, stderr=err)
    scenarios.append(sc)

    for sc in scenarios:
        sc.save()
    return scenarios


if __name__ == "__main__":
    main()
