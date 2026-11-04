"""The integration's two auth methods: their scripts (run as the executor runs them, with temp
dirs), their fit with the auth-plan contract, the types, and the permission schema."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
AUTH = INTEGRATION / "auth"

KEY_ID = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TOKEN = "IQoJb3JpZ2luX2VjEXAMPLE+token/with=padding=="
ENV_NAMES = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION"]
GOOD_REF = {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}


def load(name):
    spec = importlib.util.spec_from_file_location(f"aws_auth_{name}", AUTH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(tmp_path, name, ref=None, inputs=None, *, as_process=False):
    """(exit code, parsed $RW_AUTH_OUT_ENV or None, error JSON or None, stdout+stderr)."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)
    for key, value in (GOOD_REF if ref is None else ref).items():
        (ref_dir / key).write_text(value, encoding="utf-8")
    out_env, out_error = tmp_path / "out.env", tmp_path / "out.error"
    env = {
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(out_env),
        "RW_AUTH_OUT_ERROR": str(out_error),
    }
    env.update({f"RW_AUTH_INPUT_{k}": v for k, v in (inputs or {}).items()})
    if as_process:
        done = subprocess.run(
            [sys.executable, str(AUTH / f"{name}.py")],
            env=env,
            capture_output=True,
            text=True,
            cwd=INTEGRATION,
        )
        code, shown = done.returncode, done.stdout + done.stderr
    else:
        code, shown = load(name).main(env), ""
    parsed = None
    if out_env.exists():
        parsed = dict(line.split("=", 1) for line in out_env.read_text().splitlines())
    error = json.loads(out_error.read_text()) if out_error.exists() else None
    return code, parsed, error, shown


# ---- access-key ------------------------------------------------------------------------------


def test_access_key_writes_the_keys_and_the_default_region_in_the_types_names(tmp_path):
    code, env, error, _ = run_script(tmp_path, "access_key")
    assert (code, error) == (0, None)
    assert env == {
        "AWS_ACCESS_KEY_ID": KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "AWS_REGION": "us-east-1",
    }
    assert list(env) == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"]


def test_access_key_passes_a_session_token_through_when_the_reference_has_one(tmp_path):
    ref = {**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}
    code, env, _, _ = run_script(tmp_path, "access_key", ref=ref, inputs={"REGION": "eu-west-1"})
    assert code == 0
    assert list(env) == ENV_NAMES  # the order of the credential type's materialize.env
    assert env["AWS_SESSION_TOKEN"] == TOKEN and env["AWS_REGION"] == "eu-west-1"


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_one_trailing_newline_is_not_part_of_a_key(tmp_path, ending):
    ref = {k: v + ending for k, v in GOOD_REF.items()}
    _, env, _, _ = run_script(tmp_path, "access_key", ref=ref)
    assert (env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]) == (KEY_ID, SECRET)


@pytest.mark.parametrize(
    "ref,why",
    [
        ({}, "no key AWS_ACCESS_KEY_ID"),
        ({"AWS_ACCESS_KEY_ID": KEY_ID}, "no key AWS_SECRET_ACCESS_KEY"),
        ({"AWS_SECRET_ACCESS_KEY": SECRET}, "no key AWS_ACCESS_KEY_ID"),
        ({"value": SECRET}, "no key AWS_ACCESS_KEY_ID"),  # the unnamed value is not a key pair
        ({**GOOD_REF, "AWS_ACCESS_KEY_ID": ""}, "empty"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": "\n"}, "empty"),
        ({**GOOD_REF, "AWS_SESSION_TOKEN": ""}, "empty"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + "\nAWS_REGION=x"}, "single line"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + "\n\n"}, "single line"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": "a\x00" + SECRET}, "single line"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + " x"}, "single line"),
        ({**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN + "\r"}, "single line"),
        ({**GOOD_REF, "AWS_ACCESS_KEY_ID": "akia" + KEY_ID[4:]}, "single line"),
        ({**GOOD_REF, "AWS_ACCESS_KEY_ID": "AKIA SHORT"}, "single line"),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": "short"}, "single line"),
        ({**GOOD_REF, "AWS_SESSION_TOKEN": "'" + TOKEN}, "single line"),
    ],
)
def test_a_bad_reference_is_invalid_input_and_writes_no_env(tmp_path, ref, why):
    code, env, error, _ = run_script(tmp_path, "access_key", ref=ref)
    assert code == 1 and env is None
    assert error["code"] == "invalid_input" and why in error["message"]
    assert set(error) == {"code", "message"}


@pytest.mark.parametrize(
    "region", ["us_east_1", "US-EAST-1", "us-east", "eu-west-1\nAWS_X=1", "x" * 80, "us-east-100"]
)
@pytest.mark.parametrize("name", ["access_key", "cloud_identity"])
def test_a_bad_region_is_invalid_input_and_writes_no_env(tmp_path, name, region):
    code, env, error, _ = run_script(tmp_path, name, inputs={"REGION": region})
    assert code == 1 and env is None and error["code"] == "invalid_input"


@pytest.mark.parametrize(
    "region",
    ["us-east-1", "eu-central-2", "ap-southeast-4", "us-gov-west-1", "cn-north-1", "us-iso-east-1"],
)
def test_the_partition_regions_are_accepted(tmp_path, region):
    for name in ("access_key", "cloud_identity"):
        sub = tmp_path / name
        sub.mkdir()
        code, env, _, _ = run_script(sub, name, inputs={"REGION": region})
        assert code == 0 and env["AWS_REGION"] == region


def test_a_missing_reference_directory_is_invalid_input(tmp_path):
    env = {"RW_AUTH_OUT_ENV": str(tmp_path / "o"), "RW_AUTH_OUT_ERROR": str(tmp_path / "e")}
    assert load("access_key").main(env) == 1
    assert json.loads((tmp_path / "e").read_text())["code"] == "invalid_input"


def test_a_missing_out_env_is_internal(tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    for k, v in GOOD_REF.items():
        (ref / k).write_text(v)
    env = {"RW_AUTH_REF_DIR": str(ref), "RW_AUTH_OUT_ERROR": str(tmp_path / "e")}
    assert load("access_key").main(env) == 1
    assert json.loads((tmp_path / "e").read_text())["code"] == "internal"


def test_no_key_is_ever_printed_or_put_in_an_error(tmp_path):
    cases = [
        (None, None),  # success
        ({**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}, None),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + "\nmore"}, None),
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + " "}, None),
        ({**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN + " "}, None),
        ({**GOOD_REF, "AWS_ACCESS_KEY_ID": KEY_ID.lower()}, None),
        ({**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}, {"REGION": SECRET}),
    ]
    for i, (ref, inputs) in enumerate(cases):
        sub = tmp_path / str(i)
        sub.mkdir()
        _, _, error, shown = run_script(sub, "access_key", ref, inputs, as_process=True)
        for secret in (SECRET, TOKEN, KEY_ID, KEY_ID.lower()):
            assert secret not in shown and secret not in json.dumps(error)


def test_access_key_runs_as_a_plain_script_with_python3(tmp_path):
    code, env, _, shown = run_script(tmp_path, "access_key", as_process=True)
    assert code == 0 and env["AWS_ACCESS_KEY_ID"] == KEY_ID and shown == ""


def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path):
    code, _, error, _ = run_script(tmp_path, "access_key", ref={}, as_process=True)
    assert code == 1 and error["code"] == "invalid_input"


# ---- cloud-identity --------------------------------------------------------------------------


def test_cloud_identity_writes_only_the_region_and_ignores_any_reference(tmp_path):
    code, env, error, shown = run_script(
        tmp_path, "cloud_identity", ref={"AWS_SECRET_ACCESS_KEY": SECRET}, as_process=True
    )
    assert (code, error, shown) == (0, None, "")
    assert env == {"AWS_REGION": "us-east-1"}  # no key: the CLI finds the ambient identity itself


def test_cloud_identity_takes_the_region_input(tmp_path):
    _, env, _, _ = run_script(tmp_path, "cloud_identity", inputs={"REGION": "ap-south-1"})
    assert env == {"AWS_REGION": "ap-south-1"}


def test_cloud_identity_works_with_no_reference_directory(tmp_path):
    env = {"RW_AUTH_OUT_ENV": str(tmp_path / "o"), "RW_AUTH_OUT_ERROR": str(tmp_path / "e")}
    assert load("cloud_identity").main(env) == 0
    assert (tmp_path / "o").read_text() == "AWS_REGION=us-east-1\n"


# ---- the methods and the credential type, against the contracts ------------------------------


def integration():
    return yaml.safe_load((INTEGRATION / "integration.yaml").read_text())


def test_both_methods_are_script_methods_that_produce_aws_sdk():
    methods = integration()["authMethods"]
    assert set(methods) == {"access-key", "cloud-identity"}
    for name, script, accepts in (
        ("access-key", "auth/access_key.py", ["aws-access-key"]),
        ("cloud-identity", "auth/cloud_identity.py", ["cloud-identity"]),
    ):
        method = methods[name]
        assert method["script"] == script and "native" not in method
        assert method["produces"] == "aws/sdk" and method["accepts"] == accepts
        assert (INTEGRATION / script).is_file()
        assert set(method["inputs"]) == {"region"}
        assert method["inputs"]["region"]["default"] == load(Path(script).stem).DEFAULT_REGION


def test_the_credential_type_is_env_only_in_the_names_the_aws_cli_reads(tmp_path):
    ctype = integration()["credentialTypes"]["aws/sdk"]
    assert ctype["materialize"] == {"env": ENV_NAMES}  # one target per plan: env, no file
    assert "content" not in ctype  # a script method matches references by kind or content token
    _, env, _, _ = run_script(tmp_path, "access_key", ref={**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN})
    assert list(env) == ctype["materialize"]["env"]


@pytest.mark.parametrize("method", ["access-key", "cloud-identity"])
def test_a_compiled_auth_plan_for_each_method_fits_the_auth_plan_schema(method):
    doc = integration()
    spec = doc["authMethods"][method]
    plan = {
        "connection": "prod-account",
        "integration": "aws@0.1.0",
        "authMethod": method,
        "credentialType": spec["produces"],
        "reference": (
            {"kind": "cloud-identity"}
            if method == "cloud-identity"
            else {"kind": "k8s-secret", "ref": "rw/aws-prod", "content": "aws-access-key"}
        ),
        "method": {
            "script": {
                "path": f"integration/{spec['script']}",
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "inputs": {"region": "us-east-1"},
        "materialize": doc["credentialTypes"][spec["produces"]]["materialize"],
    }
    assert validate("auth-plan", plan) == []
    assert set(plan["inputs"]) == set(spec["inputs"])


# ---- types and the permission schema ----------------------------------------------------------


def test_the_types_are_account_region_function_with_a_canonical_account_id():
    types = yaml.safe_load((INTEGRATION / "types.yaml").read_text())["types"]
    assert types["account"] == {
        "root": True,
        "rootKey": "accountId",
        "canonicalKey": "accountId",
        "plural": "accounts",
    }
    assert types["region"]["parent"] == "account"
    assert types["lambda-function"]["parent"] == "region"
    assert set(types) == {"account", "region", "lambda-function"}


def permission_schema():
    return json.loads((INTEGRATION / "schemas" / "aws-permission.json").read_text())


def test_the_permission_schema_is_valid_2020_12_and_takes_iam_actions():
    schema = permission_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    arn = "arn:aws:logs:eu-west-1:123456789012:log-group:/aws/lambda/*"
    ok = [
        {"action": "lambda:ListFunctions"},
        {"action": "logs:FilterLogEvents", "resource": arn},
        {"action": "lambda:Get*", "resource": "*"},
    ]
    for permission in ok:
        jsonschema.validate(permission, schema)
    bad = [
        {},
        {"action": "ListFunctions"},
        {"action": "lambda:"},
        {"action": "lambda:List Functions"},
        {"action": "lambda:ListFunctions", "verbs": ["x"]},
        {"action": "lambda:ListFunctions", "resource": "not-an-arn"},
        {"actions": ["lambda:ListFunctions"]},
    ]
    for permission in bad:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(permission, schema)
