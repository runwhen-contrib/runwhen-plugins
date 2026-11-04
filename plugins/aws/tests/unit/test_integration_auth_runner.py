# ruff: noqa: F811, F401  (stub_aws is a fixture imported from test_integration_auth)
"""The auth scripts through the SDK's own script-auth runner, the way the executor runs them:
`run_script_plan` hands each script the frozen contract and nothing else (every RW_* variable but
RW_AUTH_* stripped, a private HOME and TMPDIR, the plan's inputs spelled RW_AUTH_INPUT_<NAME>),
then checks what it wrote against the credential type's `materialize.env`.

`runwhen_capability.plugin.auth_script` ships with SDK versions that have script auth plans; with an
older SDK this module is skipped. Run it with an SDK that has it (the S17 venv)."""

import os
from pathlib import Path

import pytest
import yaml

auth_script = pytest.importorskip("runwhen_capability.plugin.auth_script")
from runwhen_capability._redaction import Redactor  # noqa: E402
from test_integration_auth import (  # noqa: E402
    GOOD_REF,
    INTEGRATION,
    KEY_ID,
    ROLE,
    SECRET,
    STS_KEY,
    STS_SECRET,
    STS_TOKEN,
    TOKEN,
    ambient,
    stub_aws,  # (a fixture)
)

DOC = yaml.safe_load((INTEGRATION / "integration.yaml").read_text())
MATERIALIZE = DOC["credentialTypes"]["aws/sdk"]["materialize"]
FILES = {
    f"integration/{p.relative_to(INTEGRATION).as_posix()}": p.read_bytes()
    for p in INTEGRATION.rglob("*")
    if p.is_file() and "__pycache__" not in p.parts
}


def plan(method, inputs=None):
    return {
        "connection": "prod-account",
        "integration": "aws@0.1.0",
        "authMethod": method,
        "credentialType": "aws/sdk",
        "reference": {"kind": "k8s-secret", "ref": "rw/aws-prod", "content": "aws-access-key"},
        # relative to integration/: the SDK prepends it
        "method": {
            "script": {
                "path": DOC["authMethods"][method]["script"],
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "inputs": inputs or {},
        "materialize": MATERIALIZE,
    }


def run(tmp_path, method, reference=(), inputs=None, secrets=()):
    return auth_script.run_script_plan(
        plan(method, inputs),
        files=FILES,
        executable=frozenset(),
        reference=[(k, v.encode()) for k, v in dict(reference).items()],
        work_dir=tmp_path / "auth-0",
        timeout_s=30.0,
        redactor=Redactor(list(secrets)),
    )


@pytest.fixture(autouse=True)
def clean_host(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AWS_", "RW_")):
            monkeypatch.delenv(name)
    # the executor keeps this one for itself: a script must not need it
    monkeypatch.setenv("RW_CREDENTIALS_DIR", "/run/rw/credentials")


def test_access_key_through_the_runner_exports_the_keys_the_type_lists(tmp_path):
    result = run(tmp_path, "access-key", {**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN})
    assert result.env == {
        "AWS_ACCESS_KEY_ID": KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "AWS_SESSION_TOKEN": TOKEN,
    }
    assert set(result.env) <= set(MATERIALIZE["env"])
    assert set(result.secrets) == {KEY_ID, SECRET, TOKEN}  # what the envelope redacts


def test_access_key_with_a_bad_reference_fails_with_its_own_code_before_the_task(tmp_path):
    with pytest.raises(auth_script.AuthScriptError) as caught:
        run(tmp_path, "access-key", {"AWS_ACCESS_KEY_ID": KEY_ID})
    assert caught.value.code == "invalid_input" and "AWS_SECRET_ACCESS_KEY" in caught.value.message
    assert caught.value.scope == "auth"


def test_cloud_identity_through_the_runner_with_irsa(tmp_path, monkeypatch):
    for name, value in ambient(tmp_path).items():
        monkeypatch.setenv(name, value)
    result = run(tmp_path, "cloud-identity", inputs={"allowInstanceProfile": False})
    assert set(result.env) == {"AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"}
    assert all(Path(p).is_file() and Path(p).read_text() == "" for p in result.env.values())


def test_cloud_identity_without_an_identity_fails_the_run_naming_what_is_missing(tmp_path):
    with pytest.raises(auth_script.AuthScriptError) as caught:
        run(tmp_path, "cloud-identity", inputs={"allowInstanceProfile": False})
    assert caught.value.code == "invalid_input"
    assert "AWS_WEB_IDENTITY_TOKEN_FILE" in caught.value.message
    assert "allowInstanceProfile" in caught.value.message


def test_a_boolean_input_arrives_as_true_and_allows_the_instance_profile(tmp_path):
    # a value that is not a string arrives as JSON: True -> "true"
    result = run(tmp_path, "cloud-identity", inputs={"allowInstanceProfile": True})
    assert "AWS_CONFIG_FILE" in result.env


def test_assume_role_through_the_runner_uses_the_sdks_spelling_of_the_input_names(
    tmp_path,
    stub_aws,
    monkeypatch,
):
    monkeypatch.setenv("PATH", stub_aws.path)
    result = run(
        tmp_path,
        "assume-role",
        GOOD_REF,
        # roleArn -> RW_AUTH_INPUT_ROLE_ARN, externalId -> RW_AUTH_INPUT_EXTERNAL_ID, ...
        inputs={
            "roleArn": ROLE,
            "externalId": "ext-1",
            "sessionName": "triage",
            "allowInstanceProfile": False,
        },
        secrets=[KEY_ID, SECRET],
    )
    assert result.env == {
        "AWS_ACCESS_KEY_ID": STS_KEY,
        "AWS_SECRET_ACCESS_KEY": STS_SECRET,
        "AWS_SESSION_TOKEN": STS_TOKEN,
    }
    (call,) = stub_aws.log()
    argv = call["argv"]
    assert argv[argv.index("--role-arn") + 1] == ROLE
    assert argv[argv.index("--external-id") + 1] == "ext-1"
    assert argv[argv.index("--role-session-name") + 1] == "triage"
    assert (call["base_key"], call["base_secret"]) == (KEY_ID, SECRET)
    assert call["rw_vars"] == []  # nothing of the executor's RW_ namespace reaches the CLI
    assert all(v not in " ".join(argv) for v in (KEY_ID, SECRET, STS_KEY, STS_SECRET, STS_TOKEN))


def test_assume_role_failure_messages_pass_the_runners_redaction(tmp_path, stub_aws, monkeypatch):
    monkeypatch.setenv("PATH", stub_aws.path)
    stub_aws.mode("denied")
    with pytest.raises(auth_script.AuthScriptError) as caught:
        run(tmp_path, "assume-role", GOOD_REF, inputs={"roleArn": ROLE}, secrets=[KEY_ID, SECRET])
    assert caught.value.code == "permission_denied" and "AccessDenied" in caught.value.message
    assert KEY_ID not in caught.value.message and "LEAKEDINSTDERR" not in caught.value.message


def test_assume_role_without_its_required_input_fails_the_run(tmp_path, stub_aws, monkeypatch):
    monkeypatch.setenv("PATH", stub_aws.path)
    with pytest.raises(auth_script.AuthScriptError) as caught:
        run(tmp_path, "assume-role", GOOD_REF)
    assert caught.value.code == "invalid_input" and "roleArn" in caught.value.message


@pytest.mark.parametrize("method", ["access-key", "cloud-identity", "assume-role"])
def test_every_script_is_found_in_the_archive_where_the_plan_says(method):
    path = DOC["authMethods"][method]["script"]
    assert auth_script.script_archive_path(path) in FILES
