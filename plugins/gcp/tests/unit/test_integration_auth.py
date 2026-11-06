"""The integration's auth methods: the cloud-identity script (run as the executor runs it, with
temp dirs and a local stand-in for the metadata server) and the passthrough sa-key method, and
their fit with the auth-plan contract. Nothing here reaches the real metadata server: every run
sets GCE_METADATA_ROOT (or GCE_METADATA_HOST) to a local server or a closed port."""

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


TOKEN_REPLY = {
    "access_token": "ya29.METADATA-TOKEN-MUST-NEVER-SHOW",
    "expires_in": 3599,
    "token_type": "Bearer",
}


class Metadata:
    """A local metadata server answering GET .../service-accounts/default/email and /token."""

    def __init__(self, status=200, body=EMAIL, flavor="Google", token_status=200, token_body=None):
        outer = self
        self.requests = []
        token_body = json.dumps(TOKEN_REPLY) if token_body is None else token_body

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Metadata-Flavor")))
                is_token = self.path.endswith("/token")
                self.send_response(token_status if is_token else status)
                if flavor:
                    self.send_header("Metadata-Flavor", flavor)
                self.end_headers()
                self.wfile.write((token_body if is_token else body).encode())

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


def run_script(tmp_path, *, host, inputs=None, as_process=False, var="GCE_METADATA_ROOT"):
    """(exit code, parsed $RW_AUTH_OUT_FILE or None, error JSON or None, stdout+stderr)."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)  # cloud identity: an empty reference directory
    out_file, out_error = tmp_path / "out.json", tmp_path / "out.error"
    env = {
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_FILE": str(out_file),
        "RW_AUTH_OUT_ERROR": str(out_error),
        var: host,
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
        ("/computeMetadata/v1/instance/service-accounts/default/email", "Google"),
        ("/computeMetadata/v1/instance/service-accounts/default/token", "Google"),
    ]


def test_the_marker_file_holds_no_credential_material(tmp_path, metadata):
    server = metadata()
    run_script(tmp_path, host=server.host)
    text = (tmp_path / "out.json").read_text()
    assert set(json.loads(text)) == {"type", "serviceAccountEmail"}
    assert "private_key" not in text and "token" not in text and "ya29" not in text


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


def test_gce_metadata_root_wins_over_gce_metadata_host():
    mod = load()
    both = {"GCE_METADATA_ROOT": "root.example:80", "GCE_METADATA_HOST": "host.example:80"}
    assert mod.metadata_host(both) == "root.example:80"
    assert mod.metadata_host({"GCE_METADATA_HOST": "host.example:80"}) == "host.example:80"


def test_gce_metadata_host_still_works_when_root_is_unset(tmp_path, metadata):
    server = metadata()
    code, marker, error, _ = run_script(tmp_path, host=server.host, var="GCE_METADATA_HOST")
    assert (code, error) == (0, None) and marker["serviceAccountEmail"] == EMAIL


def test_the_metadata_server_is_asked_without_any_http_proxy(tmp_path, metadata, monkeypatch):
    # a proxy in the pod's environment must not answer for the metadata server: the opener has an
    # empty ProxyHandler, as gcloud's own metadata client does
    import urllib.request

    mod = load()
    built = []
    real = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: built.append(a) or real(*a))
    mod.default_opener()
    proxies = [h.proxies for h in built[0] if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies == [{}]
    server = metadata()
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")  # a dead proxy
    env = {"RW_AUTH_OUT_FILE": str(tmp_path / "o"), "GCE_METADATA_ROOT": server.host,
           "RW_AUTH_INPUT_PROJECT": "shop-prod-1234"}  # fmt: skip
    assert mod.main(env) == 0


def test_a_pod_that_cannot_mint_a_token_fails_here_with_a_code(tmp_path, metadata):
    server = metadata(token_status=403, token_body="no")
    code, marker, error, _ = run_script(tmp_path, host=server.host)
    assert code == 1 and marker is None and error["code"] == "permission_denied"
    assert [p for p, _ in server.requests][-1].endswith("/token")


@pytest.mark.parametrize(
    "body", ["", "not json", "[]", '{"access_token": ""}', '{"expires_in": 5}']
)
def test_a_token_reply_without_an_access_token_is_internal(tmp_path, metadata, body):
    server = metadata(token_body=body)
    _, marker, error, _ = run_script(tmp_path, host=server.host)
    assert marker is None and error["code"] == "internal"


def test_the_token_is_never_printed_written_or_put_in_a_message(tmp_path, metadata):
    server = metadata()
    code, marker, error, shown = run_script(tmp_path, host=server.host, as_process=True)
    assert code == 0
    everything = shown + json.dumps(marker) + (tmp_path / "out.json").read_text()
    assert "METADATA-TOKEN-MUST-NEVER-SHOW" not in everything
    failing = metadata(token_status=500, token_body=json.dumps(TOKEN_REPLY))
    _, _, error, shown = run_script(tmp_path, host=failing.host, as_process=True)
    assert "METADATA-TOKEN-MUST-NEVER-SHOW" not in json.dumps(error) + shown


def test_the_expected_service_account_must_match_the_pods_identity(tmp_path, metadata):
    server = metadata()
    inputs = {"PROJECT": "shop-prod-1234", "SERVICE_ACCOUNT": EMAIL}
    code, marker, error, _ = run_script(tmp_path, host=server.host, inputs=inputs)
    assert (code, error) == (0, None) and marker["serviceAccountEmail"] == EMAIL
    other = {
        "PROJECT": "shop-prod-1234",
        "SERVICE_ACCOUNT": "other@shop-prod-1234.iam.gserviceaccount.com",
    }
    (tmp_path / "second").mkdir()
    code, marker, error, _ = run_script(tmp_path / "second", host=server.host, inputs=other)
    assert code == 1 and marker is None
    assert error["code"] == "permission_denied" and set(error) == {"code", "message"}
    assert EMAIL not in error["message"] and "other@" not in error["message"]


def test_the_service_account_input_is_optional_and_checked(tmp_path, metadata):
    server = metadata()
    for value in ("not an email", EMAIL + "\nGOOGLE_CLOUD_PROJECT=x", "a" * 300 + "@b.c"):
        inputs = {"PROJECT": "shop-prod-1234", "SERVICE_ACCOUNT": value}
        code, marker, error, _ = run_script(tmp_path, host=server.host, inputs=inputs)
        assert code == 1 and marker is None and error["code"] == "invalid_input", value
    assert server.requests == []
    code, _, _, _ = run_script(
        tmp_path, host=server.host, inputs={"PROJECT": "shop-prod-1234", "SERVICE_ACCOUNT": ""}
    )
    assert code == 0


def test_the_script_reads_only_the_rw_names_an_auth_script_is_given():
    import re

    text = SCRIPT.read_text()
    names = set(re.findall(r"RW_[A-Z_]+", text))
    assert names <= {
        "RW_AUTH_OUT_FILE", "RW_AUTH_OUT_ERROR", "RW_AUTH_INPUT_PROJECT", "RW_AUTH_INPUT_SERVICE_ACCOUNT",
        "RW_AUTH_INPUT_",  # the docstring's pattern
        "RW_AUTH_REF_DIR",  # named in the docstring: it is not read
    }, names  # fmt: skip
    assert "RW_CREDENTIALS_DIR" not in text and "os.environ[" not in text.replace(
        "os.environ if", ""
    )


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
    # the project to inspect is required; the key's own project is only what it defaults to
    project = method["inputs"]["project"]
    assert project["required"] is True and project["derived"] is True
    assert "default" in project["description"] and "project_id" in project["description"]


def test_cloud_identity_is_a_script_method_matched_by_kind():
    method = integration()["authMethods"]["cloud-identity"]
    assert method["script"] == "auth/cloud-identity.py" and "native" not in method
    assert method["produces"] == "gcp/adc" and method["accepts"] == ["cloud-identity"]
    assert (INTEGRATION / method["script"]).is_file()
    assert method["inputs"]["project"]["required"] is True
    assert "required" not in method["inputs"]["serviceAccount"]  # optional


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
        declared = doc["authMethods"][method]["inputs"]
        required = {k for k, v in declared.items() if v.get("required") or v.get("derived")}
        assert required <= set(plan["inputs"]) <= set(declared)


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


# ---- as the executor runs it ------------------------------------------------------------------

S17_SDK = "runwhen_capability.plugin.auth_script"


def test_the_script_runs_under_the_executors_auth_script_runner(tmp_path, metadata, monkeypatch):
    """Through the SDK's own `run_script_plan` (CONTRACTS "Auth plan"): the child gets only
    RW_AUTH_REF_DIR, RW_AUTH_OUT_FILE, RW_AUTH_OUT_ERROR and RW_AUTH_INPUT_* (camelCase input names
    in SCREAMING_SNAKE) and the host's environment minus every other RW_* variable. Needs an SDK
    that has plugin/auth_script.py (lane-F's .venv-s17); skipped without it."""
    auth = pytest.importorskip(S17_SDK)
    from runwhen_capability._redaction import Redactor

    server = metadata()
    monkeypatch.setenv("GCE_METADATA_ROOT", server.host)
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "must-not-be-read"))  # stripped
    assert auth.input_env_var("serviceAccount") == "RW_AUTH_INPUT_SERVICE_ACCOUNT"
    doc = integration()
    plan = {
        "connection": "shop-prod", "integration": "gcp@0.1.0", "credentialType": "gcp/adc",
        "authMethod": "cloud-identity", "reference": {"kind": "cloud-identity"},
        "inputs": {"project": "shop-prod-1234", "serviceAccount": EMAIL},
        "method": {"script": {"path": "auth/cloud-identity.py"}},
        "materialize": doc["credentialTypes"]["gcp/adc"]["materialize"],
    }  # fmt: skip
    files = {
        f"integration/{p.relative_to(INTEGRATION)}": p.read_bytes()
        for p in INTEGRATION.rglob("*.py")
    }
    run = lambda work, plan=plan: auth.run_script_plan(  # noqa: E731
        plan, files=files, executable=frozenset(), reference=[], work_dir=tmp_path / work,
        timeout_s=30.0, redactor=Redactor([]),
    )  # fmt: skip
    result = run("ok")
    (path,) = result.env.values()
    assert json.loads(Path(path).read_text()) == {
        "type": "runwhen_cloud_identity", "serviceAccountEmail": EMAIL,
    }  # fmt: skip
    assert list(result.env) == ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"]  # a path, nothing else
    assert "shop-prod-1234" not in "".join(result.env.values())
    assert "METADATA-TOKEN-MUST-NEVER-SHOW" not in "".join(result.secrets)
    # the expected identity is read from the SCREAMING_SNAKE input: a mismatch fails the run
    bad = {
        **plan,
        "inputs": {
            **plan["inputs"],
            "serviceAccount": "other@shop-prod-1234.iam.gserviceaccount.com",
        },
    }
    with pytest.raises(auth.AuthScriptError) as caught:
        run("bad", bad)
    assert caught.value.code == "permission_denied"
