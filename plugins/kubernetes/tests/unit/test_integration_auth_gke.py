"""The kubernetes integration's gke auth method: its script (run as the executor runs it, as a
subprocess in temp dirs, with a fake `gke-gcloud-auth-plugin` on PATH and a fake container API on
localhost), its fit with the auth-plan contract, and the runtime that has to carry the plugin."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from runwhen_capability.contracts import validate

# The closed D64 error enum (contracts error.schema.json `code`).
ENUM = {
    "permission_denied",
    "not_found",
    "unreachable",
    "timeout",
    "rate_limited",
    "storage",
    "invalid_input",
    "dependency",
    "internal",
}

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
SCRIPT = INTEGRATION / "auth" / "gke.py"
RUNTIME = Path(__file__).parents[4] / "runtimes" / "kubernetes"

TOKEN = "ya29.FAKE-ACCESS-TOKEN-0123456789"
KEY_SECRET = "-----BEGIN PRIVATE KEY-----FAKEKEYMATERIAL-----END PRIVATE KEY-----"
STDERR_SECRET = "STDERR-SECRET-9f3a"
FAKE_CA = "RkFLRS1DQS1DRVJUSUZJQ0FURS1GT1ItVEVTVFM="  # base64 of "FAKE-CA-CERTIFICATE-FOR-TESTS"
SA_KEY = json.dumps(
    {
        "type": "service_account",
        "project_id": "demo-project-1",
        "client_email": "robot@demo-project-1.iam.gserviceaccount.com",
        "private_key": KEY_SECRET,
    }
)
INPUTS = {"PROJECT": "demo-project-1", "LOCATION": "us-central1", "CLUSTER": "prod-1"}
API_PATH = "/v1/projects/demo-project-1/locations/us-central1/clusters/prod-1"
CLUSTER_BODY = {"endpoint": "34.1.2.3", "masterAuth": {"clusterCaCertificate": FAKE_CA}}

# The fake plugin records its argv and the credentials variable, then behaves as MODE says.
FAKE_PLUGIN = """#!/bin/sh
{
  echo "args=$*"
  echo "gac=${GOOGLE_APPLICATION_CREDENTIALS-unset}"
} >> "$FAKE_PLUGIN_LOG"
case "$FAKE_PLUGIN_MODE" in
  ok)
    printf '{"kind":"ExecCredential","apiVersion":"client.authentication.k8s.io/v1beta1","spec":{"interactive":false},"status":{"expirationTimestamp":"2030-01-01T00:00:00Z","token":"%s"}}\\n' "$FAKE_TOKEN"
    ;;
  fail)
    echo "F1104 cred.go:166] print credential failed: $FAKE_STDERR" >&2
    echo "second line $FAKE_STDERR" >&2
    exit 1
    ;;
  unreachable)
    echo "F1104 cred.go:166] failed: Get http://metadata.google.internal: dial tcp: i/o timeout" >&2
    exit 1
    ;;
  misconfig)
    echo "F1104 cred.go:166] compute: metadata server answered 403 Forbidden: scope missing" >&2
    exit 1
    ;;
  garbage)
    echo "this is not json $FAKE_TOKEN"
    ;;
  notoken)
    echo '{"kind":"ExecCredential","status":{}}'
    ;;
  hang)
    exec sleep 30
    ;;
esac
"""


def load():
    spec = importlib.util.spec_from_file_location("k8s_auth_gke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    """A container API on localhost: records each request, answers `status` with `body`."""

    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, self.headers.get("Authorization")))
                payload = json.dumps(
                    outer.body if outer.body is not None else CLUSTER_BODY
                ).encode()
                self.send_response(outer.status)
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    servers = []

    def make(**kwargs):
        servers.append(FakeApi(**kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def run_script(
    tmp_path,
    server,
    *,
    ref=None,
    inputs=None,
    mode="ok",
    timeout=60,
    extra_env=None,
):
    """Run gke.py as a process. ref: {file: content} for $RW_AUTH_REF_DIR (None: one key.json
    holding the SA key; {} for cloud identity). Returns a namespace with the exit code, the parsed
    kubeconfig or None, the error JSON or None, stdout+stderr, the plugin's call log and the
    API requests."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)
    for key, content in (ref if ref is not None else {"key.json": SA_KEY}).items():
        (ref_dir / key).write_text(content, encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    plugin = fakebin / "gke-gcloud-auth-plugin"
    plugin.write_text(FAKE_PLUGIN)
    plugin.chmod(plugin.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "plugin.log"
    out_file, out_error = tmp_path / "kubeconfig", tmp_path / "out.error"
    env = {
        "PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_FILE": str(out_file),
        "RW_AUTH_OUT_ERROR": str(out_error),
        "RW_GKE_API_BASE": server.base,
        "FAKE_PLUGIN_MODE": mode,
        "FAKE_PLUGIN_LOG": str(log),
        "FAKE_TOKEN": TOKEN,
        "FAKE_STDERR": STDERR_SECRET,
    }
    given = INPUTS if inputs is None else inputs
    env.update({f"RW_AUTH_INPUT_{k}": v for k, v in given.items()})
    env.update(extra_env or {})
    done = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        cwd=PLUGIN,
        timeout=timeout,
    )

    class Result:
        code = done.returncode
        shown = done.stdout + done.stderr
        kubeconfig = json.loads(out_file.read_text()) if out_file.exists() else None
        error = json.loads(out_error.read_text()) if out_error.exists() else None
        plugin_log = log.read_text() if log.exists() else ""
        requests = server.requests
        out = out_file
        ref = ref_dir
        raw_out = out_file.read_text() if out_file.exists() else ""
        raw_error = out_error.read_text() if out_error.exists() else ""

    return Result


def assert_no_secret_anywhere(result):
    seen = result.shown + result.raw_out + result.raw_error
    for secret in (TOKEN, KEY_SECRET, STDERR_SECRET, "FAKEKEYMATERIAL"):
        assert secret not in seen


# ---- the success paths ----------------------------------------------------------------------


def test_sa_key_mode_writes_a_kubeconfig_whose_exec_names_the_key_and_holds_no_token(tmp_path, api):
    server = api()
    r = run_script(tmp_path, server)
    assert (r.code, r.error, r.shown) == (0, None, "")
    key_path = str((tmp_path / "ref" / "key.json").resolve())
    config = r.kubeconfig
    assert config["apiVersion"] == "v1" and config["kind"] == "Config"
    (cluster,) = config["clusters"]
    assert cluster["cluster"] == {
        "server": "https://34.1.2.3",
        "certificate-authority-data": FAKE_CA,
    }
    (user,) = config["users"]
    exec_entry = user["user"]["exec"]
    assert exec_entry["apiVersion"] == "client.authentication.k8s.io/v1beta1"
    assert exec_entry["command"] == "gke-gcloud-auth-plugin"
    assert exec_entry["provideClusterInfo"] is True
    assert exec_entry["interactiveMode"] == "Never"
    # without this flag the plugin shells out to `gcloud`, which the runtime does not carry
    assert exec_entry["args"] == ["--use_application_default_credentials"]
    assert exec_entry["env"] == [{"name": "GOOGLE_APPLICATION_CREDENTIALS", "value": key_path}]
    (context,) = config["contexts"]
    assert config["current-context"] == context["name"]
    assert context["context"] == {"cluster": cluster["name"], "user": user["name"]}
    # the token is minted on use: it is nowhere in the file, and no key material either
    assert_no_secret_anywhere(r)
    assert "token" not in r.raw_out.lower()
    assert yaml.safe_load(r.raw_out) == config  # JSON is a valid kubeconfig and valid YAML


def test_the_token_is_asked_for_with_the_key_and_sent_to_the_container_api(tmp_path, api):
    server = api()
    r = run_script(tmp_path, server)
    key_path = str((tmp_path / "ref" / "key.json").resolve())
    assert "args=--use_application_default_credentials" in r.plugin_log
    assert f"gac={key_path}" in r.plugin_log
    assert r.requests == [(API_PATH, f"Bearer {TOKEN}")]


def test_cloud_identity_mode_has_no_env_in_the_exec_entry_and_no_key_for_the_plugin(tmp_path, api):
    server = api()
    r = run_script(tmp_path, server, ref={}, extra_env={"GOOGLE_APPLICATION_CREDENTIALS": "/x"})
    assert (r.code, r.error) == (0, None)
    exec_entry = r.kubeconfig["users"][0]["user"]["exec"]
    assert "env" not in exec_entry
    assert exec_entry["command"] == "gke-gcloud-auth-plugin"
    assert exec_entry["args"] == ["--use_application_default_credentials"]
    assert "unset" in r.plugin_log or "gac=/x" in r.plugin_log  # the ambient env is not ours
    assert_no_secret_anywhere(r)


def test_cloud_identity_without_a_reference_directory(tmp_path, api):
    server = api()
    mod = load()
    env = {
        "PATH": os.environ["PATH"],
        "RW_AUTH_OUT_FILE": str(tmp_path / "kc"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "e"),
        **{f"RW_AUTH_INPUT_{k}": v for k, v in INPUTS.items()},
    }
    assert mod.read_key_file(env.get("RW_AUTH_REF_DIR")) is None
    assert mod.read_key_file(str(tmp_path / "does-not-exist")) is None
    assert server.requests == []


@pytest.mark.parametrize("name", ["value", "key.json", "anything"])
def test_a_single_key_file_is_used_whatever_its_name(tmp_path, api, name):
    server = api()
    r = run_script(tmp_path, server, ref={name: SA_KEY})
    assert (r.code, r.error) == (0, None)
    env = r.kubeconfig["users"][0]["user"]["exec"]["env"]
    assert env[0]["value"] == str((tmp_path / "ref" / name).resolve())


def test_the_kubeconfig_names_follow_gcloud_and_the_file_is_0600(tmp_path, api):
    r = run_script(tmp_path, api())
    assert r.kubeconfig["current-context"] == "gke_demo-project-1_us-central1_prod-1"
    assert stat.S_IMODE(r.out.stat().st_mode) == 0o600


def test_a_pre_existing_output_file_ends_up_0600_too(tmp_path, api):
    (tmp_path / "kubeconfig").write_text("STALE-CONTENT-XYZ")
    (tmp_path / "kubeconfig").chmod(0o644)
    r = run_script(tmp_path, api())
    assert r.code == 0 and stat.S_IMODE(r.out.stat().st_mode) == 0o600
    assert "STALE-CONTENT-XYZ" not in r.raw_out


# ---- failures -------------------------------------------------------------------------------


def test_a_403_is_permission_denied_in_the_error_file(tmp_path, api):
    server = api(status=403, body={"error": {"message": f"denied {TOKEN}"}})
    r = run_script(tmp_path, server)
    assert r.code == 1 and r.kubeconfig is None
    assert r.error["code"] == "permission_denied" and set(r.error) == {"code", "message"}
    assert "container.clusters.get" in r.error["message"]
    assert_no_secret_anywhere(r)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "permission_denied"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "dependency"),
        (503, "dependency"),
        (400, "dependency"),
    ],
)
def test_an_api_status_maps_to_the_closed_error_enum(tmp_path, api, status, code):
    r = run_script(tmp_path, api(status=status, body={"error": {"message": "x"}}))
    assert r.code == 1 and r.error["code"] == code and r.kubeconfig is None
    assert r.error["code"] in ENUM


def test_an_unreachable_api_is_unreachable(tmp_path):
    class Gone:
        base = "http://127.0.0.1:9"  # discard port: nothing listens
        requests = []

    r = run_script(tmp_path, Gone)
    assert r.code == 1 and r.error["code"] == "unreachable"
    assert_no_secret_anywhere(r)


@pytest.mark.parametrize(
    "body",
    [
        {"endpoint": "34.1.2.3"},
        {"masterAuth": {"clusterCaCertificate": FAKE_CA}},
        {"endpoint": "evil.example/x", "masterAuth": {"clusterCaCertificate": FAKE_CA}},
        {"endpoint": "34.1.2.3", "masterAuth": {"clusterCaCertificate": "not base64!!"}},
        {"endpoint": "34.1.2.3\nserver: x", "masterAuth": {"clusterCaCertificate": FAKE_CA}},
        {"endpoint": 5, "masterAuth": {"clusterCaCertificate": FAKE_CA}},
    ],
)
def test_a_cluster_response_without_a_usable_endpoint_and_ca_is_a_dependency_error(
    tmp_path, api, body
):
    r = run_script(tmp_path, api(body=body))
    assert r.code == 1 and r.error["code"] == "dependency" and r.kubeconfig is None


def test_a_redirect_from_the_api_is_not_followed_so_the_token_stays_put(tmp_path, api):
    elsewhere = api()
    server = api(status=302, headers={"Location": elsewhere.base + "/stolen"})
    r = run_script(tmp_path, server)
    assert r.code == 1 and r.error["code"] == "dependency"
    assert elsewhere.requests == []


def test_a_plugin_failure_is_a_credential_error_with_no_token_or_stderr_in_it(tmp_path, api):
    server = api()
    r = run_script(tmp_path, server, mode="fail")
    assert r.code == 1 and r.kubeconfig is None
    assert r.error["code"] == "permission_denied" and r.error["code"] in ENUM
    assert "gke-gcloud-auth-plugin" in r.error["message"]
    assert_no_secret_anywhere(r)
    assert "cred.go" not in r.raw_error and "second line" not in r.shown
    assert server.requests == []  # no token, no API call


def test_a_plugin_that_cannot_reach_the_metadata_server_is_unreachable(tmp_path, api):
    r = run_script(tmp_path, api(), ref={}, mode="unreachable")
    assert r.code == 1 and r.error["code"] == "unreachable"
    assert "metadata.google.internal" not in r.raw_error  # stderr is classified, never echoed


def test_a_permanent_metadata_misconfiguration_is_not_labelled_unreachable(tmp_path, api):
    r = run_script(tmp_path, api(), ref={}, mode="misconfig")
    assert r.code == 1 and r.error["code"] == "permission_denied"


def test_an_unexpected_exception_is_internal_with_only_its_class_name(tmp_path):
    mod = load()

    def boom(env):
        raise RuntimeError(f"details with {TOKEN} and {KEY_SECRET}")

    mod.build = boom
    env = {"RW_AUTH_OUT_FILE": str(tmp_path / "kc"), "RW_AUTH_OUT_ERROR": str(tmp_path / "e")}
    assert mod.main(env) == 1
    error = json.loads((tmp_path / "e").read_text())
    assert error == {"code": "internal", "message": "RuntimeError"}
    assert not (tmp_path / "kc").exists()


def test_an_unwritable_output_file_is_internal_not_a_traceback(tmp_path, api):
    r = run_script(
        tmp_path, api(), extra_env={"RW_AUTH_OUT_FILE": str(tmp_path / "no-such-dir" / "kc")}
    )
    assert r.code == 1 and r.error == {"code": "internal", "message": "FileNotFoundError"}
    assert "Traceback" not in r.shown
    assert_no_secret_anywhere(r)


@pytest.mark.parametrize("mode", ["garbage", "notoken"])
def test_a_plugin_output_without_a_token_is_a_dependency_error(tmp_path, api, mode):
    server = api()
    r = run_script(tmp_path, server, mode=mode)
    assert r.code == 1 and r.error["code"] == "dependency"
    assert_no_secret_anywhere(r)
    assert server.requests == []


def test_a_missing_plugin_is_a_dependency_error(tmp_path, api):
    server = api()
    mod = load()
    env = {
        "PATH": str(tmp_path / "empty"),
        "RW_AUTH_OUT_FILE": str(tmp_path / "kc"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "e"),
        "RW_GKE_API_BASE": server.base,
        **{f"RW_AUTH_INPUT_{k}": v for k, v in INPUTS.items()},
    }
    old = os.environ.get("PATH")
    os.environ["PATH"] = env["PATH"]
    try:
        assert mod.main(env) == 1
    finally:
        os.environ["PATH"] = old
    assert json.loads((tmp_path / "e").read_text())["code"] == "dependency"
    assert server.requests == []


def test_a_hanging_plugin_times_out(tmp_path, api):
    mod = load()
    mod.PLUGIN_TIMEOUT = 0.5  # the real limit is 30 seconds
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    plugin = fakebin / "gke-gcloud-auth-plugin"
    plugin.write_text(FAKE_PLUGIN)
    plugin.chmod(0o755)
    ref = tmp_path / "ref"
    ref.mkdir()
    server = api()
    env = {
        "RW_AUTH_REF_DIR": str(ref),
        "RW_AUTH_OUT_FILE": str(tmp_path / "kc"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "e"),
        "RW_GKE_API_BASE": server.base,
        **{f"RW_AUTH_INPUT_{k}": v for k, v in INPUTS.items()},
    }
    old = dict(os.environ)
    os.environ.update(
        PATH=f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin",
        FAKE_PLUGIN_MODE="hang",
        FAKE_PLUGIN_LOG=str(tmp_path / "log"),
    )
    try:
        assert mod.main(env) == 1
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert json.loads((tmp_path / "e").read_text())["code"] == "timeout"
    assert server.requests == []


# ---- input and reference validation: no calls are made ----------------------------------------

BAD_INPUTS = [
    {**INPUTS, "PROJECT": "Demo-Project"},
    {**INPUTS, "PROJECT": "demo"},  # too short
    {**INPUTS, "PROJECT": "1demo-project"},
    {**INPUTS, "PROJECT": "demo-project-"},
    {**INPUTS, "PROJECT": "demo-project-1\n"},
    {**INPUTS, "PROJECT": "demo_project/../x"},
    {**INPUTS, "LOCATION": "us central1"},
    {**INPUTS, "LOCATION": "../us"},
    {**INPUTS, "LOCATION": "US-CENTRAL1"},
    {**INPUTS, "LOCATION": "us-central1\r"},
    {**INPUTS, "CLUSTER": "Prod"},
    {**INPUTS, "CLUSTER": "prod-"},
    {**INPUTS, "CLUSTER": "1prod"},
    {**INPUTS, "CLUSTER": "a" * 41},
    {**INPUTS, "CLUSTER": "prod\nserver: x"},
    {**INPUTS, "CLUSTER": "prod/../../x"},
    {k: v for k, v in INPUTS.items() if k != "PROJECT"},
    {k: v for k, v in INPUTS.items() if k != "CLUSTER"},
    {**INPUTS, "LOCATION": ""},
]


@pytest.mark.parametrize("inputs", BAD_INPUTS)
def test_a_bad_project_location_or_cluster_is_invalid_input_without_any_call(tmp_path, api, inputs):
    server = api()
    r = run_script(tmp_path, server, inputs=inputs)
    assert r.code == 1 and r.kubeconfig is None
    assert r.error["code"] == "invalid_input"
    assert r.plugin_log == "" and server.requests == []
    assert_no_secret_anywhere(r)


@pytest.mark.parametrize(
    "inputs",
    [
        {"PROJECT": "demo-project-1", "LOCATION": "us-central1", "CLUSTER": "prod-1"},
        {"PROJECT": "demo-project-1", "LOCATION": "europe-west1-b", "CLUSTER": "a"},
        {"PROJECT": "abcde1", "LOCATION": "global", "CLUSTER": "a" * 40},
        {"PROJECT": "a" + "b" * 28 + "c", "LOCATION": "us", "CLUSTER": "c1"},
    ],
)
def test_the_valid_forms_pass(tmp_path, api, inputs):
    r = run_script(tmp_path, api(), inputs=inputs)
    assert (r.code, r.error) == (0, None)


def test_more_than_one_reference_file_is_invalid_input(tmp_path, api):
    server = api()
    r = run_script(tmp_path, server, ref={"key.json": SA_KEY, "value": SA_KEY})
    assert r.code == 1 and r.error["code"] == "invalid_input"
    assert "exactly one" in r.error["message"]
    assert r.plugin_log == "" and server.requests == []
    assert_no_secret_anywhere(r)


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"type": "authorized_user", "refresh_token": KEY_SECRET}),
        json.dumps({"type": "external_account", "private_key": KEY_SECRET}),
        json.dumps({"private_key": KEY_SECRET}),
        json.dumps([KEY_SECRET]),
        json.dumps(KEY_SECRET),
        "not json " + KEY_SECRET,
        "",
        "\n",
    ],
)
def test_a_key_that_is_not_a_service_account_json_is_invalid_input(tmp_path, api, content):
    server = api()
    r = run_script(tmp_path, server, ref={"key.json": content})
    assert r.code == 1 and r.error["code"] == "invalid_input"
    assert r.plugin_log == "" and server.requests == []
    assert_no_secret_anywhere(r)


def test_a_reference_entry_that_is_a_directory_is_invalid_input(tmp_path, api):
    server = api()
    ref = tmp_path / "ref"
    (ref / "sub").mkdir(parents=True)
    r = run_script(tmp_path, server, ref={})
    assert r.code == 1 and r.error["code"] == "invalid_input"
    assert server.requests == []


def test_nothing_secret_is_printed_on_any_path(tmp_path, api):
    for n, kwargs in enumerate(
        [
            {},
            {"mode": "fail"},
            {"mode": "garbage"},
            {"ref": {"a": SA_KEY, "b": SA_KEY}},
            {"ref": {"key.json": KEY_SECRET}},
        ]
    ):
        sub = tmp_path / str(n)
        sub.mkdir()
        assert_no_secret_anywhere(run_script(sub, api(), **kwargs))


# ---- the test-only API override ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example",
        "http://evil.example:8080",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "http://user@evil.example",
        "file:///etc/passwd",
        "",
    ],
)
def test_the_api_override_is_ignored_unless_it_points_at_loopback(value):
    assert load().api_base({"RW_GKE_API_BASE": value}) == "https://container.googleapis.com"


@pytest.mark.parametrize("value", ["http://127.0.0.1:8123", "http://localhost:8123"])
def test_the_api_override_is_honored_for_loopback(value):
    assert load().api_base({"RW_GKE_API_BASE": value}) == value


def test_the_default_api_is_the_google_one():
    assert load().api_base({}) == "https://container.googleapis.com"


def test_it_runs_as_a_plain_script_with_python3_and_prints_nothing_on_success(tmp_path, api):
    r = run_script(tmp_path, api())
    assert r.code == 0 and r.shown == ""


# ---- the method against the contracts ---------------------------------------------------------


def integration():
    return yaml.safe_load((INTEGRATION / "integration.yaml").read_text())


def test_the_method_is_a_script_method_with_the_agreed_shape():
    method = integration()["authMethods"]["gke"]
    # relPath is "relative to its file's directory": integration.yaml sits in integration/
    assert method["script"] == "auth/gke.py"
    assert "native" not in method
    assert (INTEGRATION / method["script"]).is_file()
    assert method["produces"] == "kubernetes/kubeconfig"
    assert method["accepts"] == ["gcp-sa-key", "cloud-identity"]  # vault refs match by content
    assert method["inputs"] == {
        "project": {"type": "string", "required": True},
        "location": {"type": "string", "required": True},
        "cluster": {"type": "string", "required": True},
    }
    assert {"kubeconfig", "in-cluster", "gke"} <= set(integration()["authMethods"])


def test_the_integration_fits_the_integration_schema():
    assert validate("integration", integration()) == []


def test_the_error_codes_the_script_uses_are_in_the_closed_enum():
    source = SCRIPT.read_text()
    used = set(re.findall(r'AuthError\(\s*"([a-z_]+)"', source))
    assert used and used <= ENUM, used - ENUM


def test_a_compiled_auth_plan_for_the_method_fits_the_auth_plan_schema():
    doc = integration()
    method = doc["authMethods"]["gke"]
    plan = {
        "connection": "prod-gke",
        "integration": "kubernetes@0.1.0",
        "authMethod": "gke",
        "credentialType": method["produces"],
        "reference": {"kind": "vault", "ref": "secret/data/gke/prod-sa"},
        "method": {
            "script": {
                "path": f"integration/{method['script']}",
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "inputs": {"project": "demo-project-1", "location": "us-central1", "cluster": "prod-1"},
        "materialize": doc["credentialTypes"][method["produces"]]["materialize"],
    }
    assert validate("auth-plan", plan) == []
    assert set(plan["inputs"]) == set(method["inputs"])
    cloud = {**plan, "reference": {"kind": "cloud-identity"}}
    assert validate("auth-plan", cloud) == []


# ---- the runtime that must carry the plugin --------------------------------------------------


def test_the_runtime_installs_the_plugin_from_a_pinned_checksummed_release():
    dockerfile = (RUNTIME / "Dockerfile").read_text()
    assert "gke-gcloud-auth-plugin" in dockerfile
    assert "sha256sum -c" in dockerfile
    assert "dl.google.com/dl/cloudsdk/channels/rapid/components/" in dockerfile


def test_the_runtime_lists_and_smoke_tests_the_plugin():
    runtime = yaml.safe_load((RUNTIME / "runtime.yaml").read_text())
    assert "gke-gcloud-auth-plugin" in runtime["tools"]
    assert "gke-gcloud-auth-plugin --version" in (RUNTIME / "smoke.sh").read_text()
