"""The integration's auth methods: the cloud-identity script (run as the executor runs it, with
temp dirs and a local stand-in for the metadata server) and the passthrough sa-key method, and
their fit with the auth-plan contract. Nothing here reaches the real metadata server: every run
sets GCE_METADATA_HOST."""

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
SCRIPT = INTEGRATION / "auth" / "cloud-identity.py"
EMAIL = "runner@shop-prod-1234.iam.gserviceaccount.com"
CLOSED = "127.0.0.1:1"  # nothing listens on port 1


def load():
    spec = importlib.util.spec_from_file_location("gcp_auth_cloud_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Metadata:
    """A local metadata server answering GET .../service-accounts/default/email."""

    def __init__(self, status=200, body=EMAIL, flavor="Google"):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Metadata-Flavor")))
                self.send_response(status)
                if flavor:
                    self.send_header("Metadata-Flavor", flavor)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_port}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def metadata():
    made = []

    def make(**kwargs):
        made.append(Metadata(**kwargs))
        return made[-1]

    yield make
    for server in made:
        server.close()


def run_script(tmp_path, *, host, inputs=None, as_process=False):
    """(exit code, parsed $RW_AUTH_OUT_FILE or None, error JSON or None, stdout+stderr)."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)  # cloud identity: an empty reference directory
    out_file, out_error = tmp_path / "out.json", tmp_path / "out.error"
    env = {
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_FILE": str(out_file),
        "RW_AUTH_OUT_ERROR": str(out_error),
        "GCE_METADATA_HOST": host,
    }
    given = {"PROJECT": "shop-prod-1234"} if inputs is None else inputs
    env.update({f"RW_AUTH_INPUT_{k}": v for k, v in given.items()})
    if as_process:
        done = subprocess.run(
            [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, cwd=INTEGRATION
        )
        code, shown = done.returncode, done.stdout + done.stderr
    else:
        code, shown = load().main(env), ""
    parsed = json.loads(out_file.read_text()) if out_file.exists() else None
    error = json.loads(out_error.read_text()) if out_error.exists() else None
    return code, parsed, error, shown


def test_a_reachable_metadata_server_gives_the_marker_file(tmp_path, metadata):
    server = metadata()
    code, marker, error, _ = run_script(tmp_path, host=server.host)
    assert (code, error) == (0, None)
    assert marker == {"type": "runwhen_cloud_identity", "serviceAccountEmail": EMAIL}
    assert server.requests == [
        ("/computeMetadata/v1/instance/service-accounts/default/email", "Google")
    ]


def test_the_marker_file_holds_no_credential_material(tmp_path, metadata):
    server = metadata()
    run_script(tmp_path, host=server.host)
    text = (tmp_path / "out.json").read_text()
    assert set(json.loads(text)) == {"type", "serviceAccountEmail"}
    assert "private_key" not in text and "token" not in text


@pytest.mark.parametrize(
    "status, code",
    [(404, "not_found"), (403, "permission_denied"), (401, "permission_denied"), (500, "internal")],
)
def test_a_metadata_server_error_is_a_classified_failure(tmp_path, metadata, status, code):
    server = metadata(status=status, body="no")
    exit_code, marker, error, _ = run_script(tmp_path, host=server.host)
    assert exit_code == 1 and marker is None
    assert error["code"] == code and set(error) == {"code", "message"}


def test_no_metadata_server_is_unreachable(tmp_path):
    code, marker, error, _ = run_script(tmp_path, host=CLOSED)
    assert code == 1 and marker is None
    assert error["code"] == "unreachable" and "Workload Identity" in error["message"]


def test_a_server_that_is_not_google_is_unreachable(tmp_path, metadata):
    server = metadata(flavor="")
    _, marker, error, _ = run_script(tmp_path, host=server.host)
    assert marker is None and error["code"] == "unreachable" and "not a Google" in error["message"]


@pytest.mark.parametrize("body", ["", "not an email", "a@b\nc@d", "x" * 300 + "@y.z"])
def test_a_reply_that_is_not_an_email_is_internal(tmp_path, metadata, body):
    server = metadata(body=body)
    _, marker, error, _ = run_script(tmp_path, host=server.host)
    assert marker is None and error["code"] == "internal"


@pytest.mark.parametrize(
    "inputs",
    [
        {},  # no project
        {"PROJECT": ""},
        {"PROJECT": "shop-prod-1234\nGOOGLE_CLOUD_PROJECT=other"},  # a newline adds a variable
        {"PROJECT": "shop-prod-1234\r"},
        {"PROJECT": "Shop-Prod"},  # upper case
        {"PROJECT": "short"},  # under 6 characters
        {"PROJECT": "shop-prod-"},  # trailing hyphen
        {"PROJECT": "1shop-prod"},  # starts with a digit
        {"PROJECT": "a" * 31},
        {"PROJECT": "shop prod 1"},
    ],
)
def test_a_bad_project_is_invalid_input_and_writes_no_file(tmp_path, metadata, inputs):
    server = metadata()
    code, marker, error, _ = run_script(tmp_path, host=server.host, inputs=inputs)
    assert code == 1 and marker is None
    assert error["code"] == "invalid_input"
    assert server.requests == [], "the input is checked before the metadata server is asked"


@pytest.mark.parametrize("host", ["evil.example/path", "a b", "h:99999999", "u@h", "h\nx"])
def test_a_bad_metadata_host_is_invalid_input(tmp_path, host):
    _, marker, error, _ = run_script(tmp_path, host=host)
    assert marker is None and error["code"] == "invalid_input"


def test_the_default_host_is_googles_and_the_script_does_not_follow_redirects():
    mod = load()
    assert mod.DEFAULT_METADATA_HOST == "metadata.google.internal"
    assert mod.metadata_host({}) == "metadata.google.internal"
    assert mod._NoRedirect().redirect_request() is None


def test_it_runs_as_a_plain_script_with_python3(tmp_path, metadata):
    server = metadata()
    code, marker, _, shown = run_script(tmp_path, host=server.host, as_process=True)
    assert code == 0 and marker["serviceAccountEmail"] == EMAIL and shown == ""


def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path):
    code, _, error, shown = run_script(tmp_path, host=CLOSED, as_process=True)
    assert code == 1 and error["code"] == "unreachable" and "auth/cloud-identity.py" in shown


def test_the_reference_directory_is_never_read(tmp_path, metadata):
    # a cloud-identity reference has no material; even if one is there, nothing of it is used
    server = metadata()
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "value").write_text("SECRET-KEY-MATERIAL")
    code, marker, error, shown = run_script(tmp_path, host=server.host, as_process=True)
    assert code == 0 and "SECRET-KEY-MATERIAL" not in json.dumps(marker) + shown


# ---- the methods and the credential type, against the contracts ------------------------------


def integration():
    return yaml.safe_load((INTEGRATION / "integration.yaml").read_text())


def test_the_credential_type_is_one_file_that_gcloud_reads():
    ctype = integration()["credentialTypes"]["gcp/adc"]
    assert ctype["materialize"] == {
        "file": {"env": "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "mode": "0600"}
    }
    assert ctype["content"] == "gcp-sa-key"  # what the passthrough method needs a reference to say


def test_sa_key_is_a_native_passthrough_method():
    method = integration()["authMethods"]["sa-key"]
    assert method["native"] == "passthrough" and "script" not in method
    assert method["produces"] == "gcp/adc"
    assert method["accepts"] == ["k8s-secret", "vault", "file"]
    assert method["inputs"]["project"].get("derived") is True


def test_cloud_identity_is_a_script_method_matched_by_kind():
    method = integration()["authMethods"]["cloud-identity"]
    assert method["script"] == "auth/cloud-identity.py" and "native" not in method
    assert method["produces"] == "gcp/adc" and method["accepts"] == ["cloud-identity"]
    assert (INTEGRATION / method["script"]).is_file()
    assert method["inputs"]["project"]["required"] is True


PLAN = {
    "connection": "shop-prod",
    "integration": "gcp@0.1.0",
    "credentialType": "gcp/adc",
    "inputs": {"project": "shop-prod-1234"},
}


def test_compiled_auth_plans_for_both_methods_fit_the_auth_plan_schema():
    doc = integration()
    materialize = doc["credentialTypes"]["gcp/adc"]["materialize"]
    sa_key = {
        **PLAN,
        "authMethod": "sa-key",
        "reference": {"kind": "k8s-secret", "ref": "shop/gcp-key", "content": "gcp-sa-key"},
        "method": {"native": "passthrough"},
        "materialize": materialize,
    }
    ambient = {
        **PLAN,
        "authMethod": "cloud-identity",
        "reference": {"kind": "cloud-identity"},
        "method": {
            "script": {
                "path": "auth/cloud-identity.py",
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "materialize": materialize,
    }
    assert validate("auth-plan", sa_key) == []
    assert validate("auth-plan", ambient) == []
    for plan, method in ((sa_key, "sa-key"), (ambient, "cloud-identity")):
        assert set(plan["inputs"]) == set(doc["authMethods"][method]["inputs"])


def test_the_permission_schema_takes_iam_permission_names():
    import jsonschema

    schema = json.loads((INTEGRATION / "schemas" / "gcp-permission.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    check = jsonschema.Draft202012Validator(schema)
    for good in ("storage.buckets.get", "storage.buckets.getIamPolicy", "iam.serviceAccounts.get"):
        assert not list(check.iter_errors({"permission": good})), good
    for bad in (
        {"permission": "storage"},
        {"permission": "storage.buckets"},
        {"permission": "Storage.buckets.get"},
        {"permission": "roles/storage.admin"},
        {"permission": "storage.buckets.get", "scope": "x"},
        {},
    ):
        assert list(check.iter_errors(bad)), bad
