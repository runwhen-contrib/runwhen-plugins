"""The integration's three auth methods: their scripts (run as the executor runs them, with temp
dirs and a stubbed `aws`), their fit with the auth-plan contract, the types, and the permission
schema. tests/unit/test_integration_auth_runner.py runs the same scripts through the SDK's own
auth-script runner."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from runwhen_capability.contracts import validate
from runwhen_capability.custom.manifest import input_env_name

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
AUTH = INTEGRATION / "auth"

KEY_ID = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TOKEN = "IQoJb3JpZ2luX2VjEXAMPLE+token/with=padding=="
ENV_NAMES = [
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
]
GOOD_REF = {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
ROLE = "arn:aws:iam::210987654321:role/rw-read"
# what the scripts may read of the RW_ namespace: the SDK strips every other RW_* variable
ALLOWED_RW = {"RW_AUTH_REF_DIR", "RW_AUTH_OUT_ENV", "RW_AUTH_OUT_FILE", "RW_AUTH_OUT_ERROR"}
IRSA = {
    "AWS_WEB_IDENTITY_TOKEN_FILE": "{token}",
    "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/pod",
}


def load(name):
    if str(AUTH) not in sys.path:  # a script finds its sibling _common.py as it does when run
        sys.path.insert(0, str(AUTH))
    spec = importlib.util.spec_from_file_location(f"aws_auth_{name}", AUTH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def input_var(name):
    """RW_AUTH_INPUT_<NAME> exactly as the SDK spells it for an input called `name`."""
    return f"RW_AUTH_INPUT_{input_env_name(name)}"


def run_script(tmp_path, name, ref=None, inputs=None, *, as_process=False, extra_env=None):
    """(exit code, parsed $RW_AUTH_OUT_ENV or None, error JSON or None, stdout+stderr). `inputs`
    are keyed by the input's name in integration.yaml (roleArn), not by its variable."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir(exist_ok=True)
    for key, value in (GOOD_REF if ref is None else ref).items():
        (ref_dir / key).write_text(value, encoding="utf-8")
    out_env, out_error = tmp_path / "out.env", tmp_path / "out.error"
    env = {
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(out_env),
        "RW_AUTH_OUT_ERROR": str(out_error),
        "TMPDIR": str(tmp_path),
    }
    env.update({input_var(k): v for k, v in (inputs or {}).items()})
    env.update(extra_env or {})
    if as_process:
        env.setdefault("PATH", os.environ["PATH"])
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


def ambient(tmp_path, **extra):
    """The environment of an executor with IRSA: a token file that exists and a role ARN."""
    token = tmp_path / "token"
    token.write_text("projected-service-account-token")
    return {**{k: v.format(token=token) for k, v in IRSA.items()}, **extra}


# ---- the scripts read no RW_ name but the contract's --------------------------------------------


def script_sources():
    return sorted(AUTH.glob("*.py"))


def test_the_scripts_read_only_the_contracts_rw_names_and_never_the_credentials_dir():
    for path in script_sources():
        text = path.read_text()
        assert "RW_CREDENTIALS_DIR" not in text, path.name
        names = set(re.findall(r"\bRW_[A-Z0-9_]+", text))
        inputs = {n for n in names if n.startswith("RW_AUTH_INPUT_")}
        assert names - inputs <= ALLOWED_RW, (
            f"{path.name} reads {sorted(names - inputs - ALLOWED_RW)}"
        )


def test_every_input_variable_a_script_reads_is_the_sdks_spelling_of_a_declared_input():
    methods = integration()["authMethods"]
    declared = {input_var(name) for method in methods.values() for name in method.get("inputs", {})}
    read = {
        n
        for path in script_sources()
        for n in re.findall(r"\bRW_AUTH_INPUT_[A-Z0-9_]+", path.read_text())
    }
    assert read == declared
    assert "RW_AUTH_INPUT_ROLE_ARN" in read and "RW_AUTH_INPUT_ALLOW_INSTANCE_PROFILE" in read


def test_no_script_names_a_region_or_materializes_one():
    for path in script_sources():
        assert "REGION" not in path.read_text().replace("PARTITION_REGION", ""), path.name


# ---- access-key ------------------------------------------------------------------------------


def test_access_key_writes_the_keys_only_in_the_types_names(tmp_path):
    code, env, error, _ = run_script(tmp_path, "access_key")
    assert (code, error) == (0, None)
    assert env == {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    assert list(env) == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]  # no AWS_REGION


def test_access_key_passes_a_session_token_through_when_the_reference_has_one(tmp_path):
    ref = {**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}
    code, env, _, _ = run_script(tmp_path, "access_key", ref=ref)
    assert code == 0
    assert list(env) == ENV_NAMES[:3]  # the order of the credential type's materialize.env
    assert env["AWS_SESSION_TOKEN"] == TOKEN


def test_access_key_writes_no_region_even_when_the_environment_offers_one(tmp_path):
    _, env, _, _ = run_script(
        tmp_path, "access_key", extra_env={"AWS_REGION": "eu-west-1", "RW_AUTH_INPUT_REGION": "x"}
    )
    assert "AWS_REGION" not in env and "eu-west-1" not in json.dumps(env)


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
        ({**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + "\nAWS_X=x"}, "single line"),
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
        None,  # success
        {**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN},
        {**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + "\nmore"},
        {**GOOD_REF, "AWS_SECRET_ACCESS_KEY": SECRET + " "},
        {**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN + " "},
        {**GOOD_REF, "AWS_ACCESS_KEY_ID": KEY_ID.lower()},
    ]
    for i, ref in enumerate(cases):
        sub = tmp_path / str(i)
        sub.mkdir()
        _, _, error, shown = run_script(sub, "access_key", ref, as_process=True)
        for secret in (SECRET, TOKEN, KEY_ID, KEY_ID.lower()):
            assert secret not in shown and secret not in json.dumps(error)


def test_access_key_runs_as_a_plain_script_with_python3(tmp_path):
    code, env, _, shown = run_script(tmp_path, "access_key", as_process=True)
    assert code == 0 and env["AWS_ACCESS_KEY_ID"] == KEY_ID and shown == ""


def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path):
    code, _, error, _ = run_script(tmp_path, "access_key", ref={}, as_process=True)
    assert code == 1 and error["code"] == "invalid_input"


# ---- cloud-identity --------------------------------------------------------------------------


def test_cloud_identity_with_irsa_writes_only_paths_of_empty_files_in_a_private_directory(
    tmp_path,
):
    code, env, error, shown = run_script(
        tmp_path,
        "cloud_identity",
        ref={"AWS_SECRET_ACCESS_KEY": SECRET},  # a reference is ignored
        as_process=True,
        extra_env=ambient(tmp_path),
    )
    assert (code, error, shown) == (0, None, "")
    assert list(env) == ["AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]
    for path in env.values():
        assert Path(path).is_file() and Path(path).read_text() == ""
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(env["AWS_CONFIG_FILE"]).parent.stat().st_mode) == 0o700
    assert SECRET not in json.dumps(env)  # no key, no region: nothing for the CLI but the paths


@pytest.mark.parametrize(
    "extra",
    [
        {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials"},
        {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"},
    ],
)
def test_cloud_identity_accepts_a_container_credential_endpoint(tmp_path, extra):
    code, env, error, _ = run_script(tmp_path, "cloud_identity", extra_env=extra)
    assert (code, error) == (0, None) and "AWS_CONFIG_FILE" in env


def test_cloud_identity_accepts_the_instance_profile_only_when_asked(tmp_path):
    for sub, inputs, ok in (
        ("none", {}, False),
        ("true", {"allowInstanceProfile": "true"}, True),
        ("false", {"allowInstanceProfile": "false"}, False),
    ):
        (tmp_path / sub).mkdir()
        code, env, error, _ = run_script(tmp_path / sub, "cloud_identity", inputs=inputs)
        if ok:
            assert (code, error) == (0, None) and "AWS_CONFIG_FILE" in env
        else:
            assert code == 1 and error["code"] == "invalid_input"


def test_cloud_identity_without_an_ambient_identity_names_what_is_missing(tmp_path):
    code, env, error, _ = run_script(tmp_path, "cloud_identity", as_process=True)
    assert code == 1 and env is None and error["code"] == "invalid_input"
    for name in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "allowInstanceProfile",
    ):
        assert name in error["message"]
    assert set(error) == {"code", "message"}


def test_cloud_identity_names_the_half_of_irsa_that_is_missing(tmp_path):
    token = ambient(tmp_path)["AWS_WEB_IDENTITY_TOKEN_FILE"]
    _, _, error, _ = run_script(
        tmp_path, "cloud_identity", extra_env={"AWS_WEB_IDENTITY_TOKEN_FILE": token}
    )
    assert "AWS_ROLE_ARN is not set" in error["message"]
    _, _, error, _ = run_script(
        tmp_path, "cloud_identity", extra_env={"AWS_ROLE_ARN": IRSA["AWS_ROLE_ARN"]}
    )
    assert "AWS_WEB_IDENTITY_TOKEN_FILE is not set" in error["message"]


def test_cloud_identity_refuses_a_token_file_that_does_not_exist_without_printing_it(tmp_path):
    secret_path = "/var/run/secrets/eks.amazonaws.com/serviceaccount/NOSUCHTOKEN"
    code, _, error, shown = run_script(
        tmp_path,
        "cloud_identity",
        extra_env={**IRSA, "AWS_WEB_IDENTITY_TOKEN_FILE": secret_path},
        as_process=True,
    )
    assert code == 1 and error["code"] == "invalid_input"
    assert secret_path not in error["message"] + shown and IRSA["AWS_ROLE_ARN"] not in shown


def test_cloud_identity_never_prints_a_value_on_success_or_failure(tmp_path):
    env = ambient(tmp_path, AWS_CONTAINER_CREDENTIALS_FULL_URI="http://169.254.170.23/SECRETPATH")
    for sub, extra in (("ok", env), ("bad", {"AWS_ROLE_ARN": "arn:aws:iam::1:role/SECRETROLE"})):
        (tmp_path / sub).mkdir()
        _, _, error, shown = run_script(
            tmp_path / sub, "cloud_identity", extra_env=extra, as_process=True
        )
        assert "SECRETPATH" not in shown + json.dumps(error)
        assert "SECRETROLE" not in shown + json.dumps(error)


def test_cloud_identity_works_with_no_reference_directory(tmp_path):
    env = {
        "RW_AUTH_OUT_ENV": str(tmp_path / "o"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "e"),
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/x",
        "TMPDIR": str(tmp_path),
    }
    assert load("cloud_identity").main(env) == 0
    assert (tmp_path / "o").read_text().startswith("AWS_CONFIG_FILE=")


# ---- assume-role: a stub `aws` first on PATH --------------------------------------------------------

STS_SECRET = "stsSecretAccessKey/ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
STS_TOKEN = "FwoGZXIvYXdzEXAMPLEsessiontokenwithlongbody/+=="
STS_KEY = "ASIAZZZZZZZZZZZZZZZZ"
STUB_AWS = """#!{python}
import json, os, sys
record = {{
    "argv": sys.argv[1:],
    "base_key": os.environ.get("AWS_ACCESS_KEY_ID"),
    "base_secret": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    "base_token": os.environ.get("AWS_SESSION_TOKEN"),
    "no_proxy": os.environ.get("NO_PROXY"),
    "rw_vars": sorted(k for k in os.environ if k.startswith("RW_")),
    "config_file": os.environ.get("AWS_CONFIG_FILE"),
    "profile": os.environ.get("AWS_PROFILE"),
}}
with open({log!r}, "a") as handle:
    handle.write(json.dumps(record) + "\\n")
mode = open({mode!r}).read().strip() if os.path.exists({mode!r}) else "ok"
if mode == "denied":
    sys.stderr.write(
        "\\naws: [ERROR]: An error occurred (AccessDenied) when calling the AssumeRole operation: "
        "User: arn:aws:iam::1:user/x is not authorized; secret=LEAKEDINSTDERR\\n"
    )
    sys.exit(254)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "shape":
    print(json.dumps({{"Credentials": {{"AccessKeyId": "bad key", "SecretAccessKey": "x", "SessionToken": "y"}}}}))
    sys.exit(0)
print(json.dumps({{"Credentials": {{
    "AccessKeyId": {key!r}, "SecretAccessKey": {secret!r}, "SessionToken": {token!r},
    "Expiration": "2026-11-04T13:00:00+00:00"}},
    "AssumedRoleUser": {{"AssumedRoleId": "AROAEXAMPLE:runwhen", "Arn": "arn:aws:sts::1:assumed-role/rw-read/runwhen"}}}}))
"""


@pytest.fixture
def stub_aws(tmp_path):
    """A stub `aws` on a PATH of its own; `.log()` is the records of its calls, `.mode(x)` makes
    the next calls fail (denied), answer with garbage or with credentials of a bad shape."""
    bin_dir, log, mode = tmp_path / "bin", tmp_path / "aws.log", tmp_path / "aws.mode"
    bin_dir.mkdir()
    exe = bin_dir / "aws"
    exe.write_text(
        STUB_AWS.format(
            python=sys.executable,
            log=str(log),
            mode=str(mode),
            key=STS_KEY,
            secret=STS_SECRET,
            token=STS_TOKEN,
        )  # fmt: skip
    )
    exe.chmod(0o755)

    class Stub:
        path = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"

        def log(self):
            return (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )

        def mode(self, value):
            mode.write_text(value)

    return Stub()


def assume(tmp_path, stub, ref=None, inputs=None, **kw):
    return run_script(
        tmp_path, "assume_role", ref=ref, inputs={"roleArn": ROLE, **(inputs or {})},
        extra_env={"PATH": stub.path, **kw.pop("extra_env", {})}, **kw,
    )  # fmt: skip


def test_assume_role_with_an_access_key_writes_the_three_values_and_nothing_else(
    tmp_path, stub_aws
):
    code, env, error, shown = assume(
        tmp_path, stub_aws, ref={**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}, as_process=True
    )
    assert (code, error, shown) == (0, None, "")  # nothing on stdout or stderr
    assert env == {
        "AWS_ACCESS_KEY_ID": STS_KEY,
        "AWS_SECRET_ACCESS_KEY": STS_SECRET,
        "AWS_SESSION_TOKEN": STS_TOKEN,
    }
    assert list(env) == ENV_NAMES[:3]
    (call,) = stub_aws.log()
    # the base keys reach the CLI through its environment, never its command line
    assert (call["base_key"], call["base_secret"], call["base_token"]) == (KEY_ID, SECRET, TOKEN)
    assert call["argv"][:2] == ["sts", "assume-role"]
    for value in (KEY_ID, SECRET, TOKEN, STS_KEY, STS_SECRET, STS_TOKEN):
        assert value not in " ".join(call["argv"])


def test_assume_role_argv_and_defaults(tmp_path, stub_aws):
    assume(tmp_path, stub_aws, inputs={"externalId": "ext-id_1"})
    argv = stub_aws.log()[0]["argv"]
    assert argv == [
        "sts", "assume-role", "--role-arn", ROLE, "--role-session-name", "runwhen",
        "--external-id", "ext-id_1", "--region", "us-east-1", "--output", "json", "--no-cli-pager",
    ]  # fmt: skip
    stub_aws_log = tmp_path / "aws.log"
    stub_aws_log.unlink()
    assume(tmp_path / "again", stub_aws, inputs={"sessionName": "triage-7"}) if (
        tmp_path / "again"
    ).mkdir() is None else None
    argv = stub_aws.log()[0]["argv"]
    assert "--external-id" not in argv and argv[argv.index("--role-session-name") + 1] == "triage-7"


@pytest.mark.parametrize(
    "role,region",
    [
        ("arn:aws-cn:iam::123456789012:role/r", "cn-north-1"),
        ("arn:aws-us-gov:iam::123456789012:role/r", "us-gov-west-1"),
        ("arn:aws-eusc:iam::123456789012:role/path/r", "eusc-de-east-1"),
    ],
)
def test_assume_role_calls_the_partitions_own_sts_region(tmp_path, stub_aws, role, region):
    code, _, _, _ = assume(tmp_path, stub_aws, inputs={"roleArn": role})
    assert code == 0
    argv = stub_aws.log()[0]["argv"]
    assert argv[argv.index("--region") + 1] == region


def test_assume_role_with_an_ambient_base_needs_an_ambient_identity(tmp_path, stub_aws):
    code, env, error, _ = assume(tmp_path, stub_aws, ref={})
    assert code == 1 and env is None and error["code"] == "invalid_input"
    assert "AWS_WEB_IDENTITY_TOKEN_FILE" in error["message"] and stub_aws.log() == []
    (tmp_path / "irsa").mkdir()
    code, env, error, _ = assume(tmp_path / "irsa", stub_aws, ref={}, extra_env=ambient(tmp_path))
    assert (code, error) == (0, None) and env["AWS_ACCESS_KEY_ID"] == STS_KEY
    (call,) = stub_aws.log()
    assert call["base_key"] is None  # the CLI found the IRSA identity itself


def test_assume_role_with_an_instance_profile_base_only_when_allowed(tmp_path, stub_aws):
    code, _, error, _ = assume(tmp_path, stub_aws, ref={}, inputs={"allowInstanceProfile": "true"})
    assert (code, error) == (0, None)


def test_assume_role_runs_the_cli_with_the_metadata_hosts_out_of_any_proxy(tmp_path, stub_aws):
    assume(
        tmp_path, stub_aws, extra_env={"NO_PROXY": "corp.internal", "HTTPS_PROXY": "http://p:3128"}
    )
    hosts = stub_aws.log()[0]["no_proxy"].split(",")
    assert hosts[0] == "corp.internal"
    for host in ("169.254.169.254", "169.254.170.2", "[fd00:ec2::254]"):
        assert host in hosts


def test_assume_role_runs_the_cli_without_rw_variables_profiles_or_config_files(tmp_path, stub_aws):
    assume(
        tmp_path, stub_aws,
        extra_env={"AWS_PROFILE": "prod", "RW_SECRET_THING": "x", "AWS_CONFIG_FILE": "/home/u/.aws/c"},
    )  # fmt: skip
    call = stub_aws.log()[0]
    assert call["rw_vars"] == [] and call["profile"] is None and call["config_file"] == os.devnull


@pytest.mark.parametrize(
    "inputs",
    [
        {"roleArn": ""},
        {"roleArn": "rw-read"},
        {"roleArn": "arn:aws:iam::123:role/x"},
        {"roleArn": "arn:aws:iam::123456789012:user/x"},
        {"roleArn": ROLE + "\\nAWS_X=1"},
        {"sessionName": "x"},
        {"sessionName": "a b"},
        {"sessionName": "x" * 65},
        {"externalId": "a"},
        {"externalId": "ok\\nAWS_X=1"},
    ],
)
def test_assume_role_bad_inputs_are_invalid_input_before_any_call(tmp_path, stub_aws, inputs):
    code, env, error, _ = assume(tmp_path, stub_aws, inputs=inputs)
    assert code == 1 and env is None and error["code"] == "invalid_input"
    assert stub_aws.log() == []


def test_assume_role_without_a_role_arn_input_is_invalid_input(tmp_path, stub_aws):
    code, _, error, _ = run_script(tmp_path, "assume_role", extra_env={"PATH": stub_aws.path})
    assert code == 1 and error["code"] == "invalid_input" and "roleArn" in error["message"]


def test_assume_role_a_denied_call_is_permission_denied_and_echoes_none_of_the_clis_text(
    tmp_path, stub_aws
):
    stub_aws.mode("denied")
    code, env, error, shown = assume(tmp_path, stub_aws, as_process=True)
    assert code == 1 and env is None and error["code"] == "permission_denied"
    assert "AccessDenied" in error["message"] and "trust policy" in error["message"]
    assert "LEAKEDINSTDERR" not in error["message"] + shown
    assert KEY_ID not in error["message"] + shown and SECRET not in error["message"] + shown


@pytest.mark.parametrize("mode", ["garbage", "shape"])
def test_assume_role_credentials_of_an_unexpected_shape_are_internal_and_written_nowhere(
    tmp_path, stub_aws, mode
):
    stub_aws.mode(mode)
    code, env, error, _ = assume(tmp_path, stub_aws)
    assert code == 1 and env is None and error["code"] == "internal"


def test_assume_role_without_the_cli_is_a_dependency_error(tmp_path):
    code, _, error, _ = run_script(
        tmp_path, "assume_role", inputs={"roleArn": ROLE}, extra_env={"PATH": str(tmp_path)}
    )
    assert code == 1 and error["code"] == "dependency"


def test_assume_role_never_prints_or_errors_with_any_key(tmp_path, stub_aws):
    for sub, mode in (("ok", "ok"), ("denied", "denied"), ("shape", "shape")):
        (tmp_path / sub).mkdir()
        stub_aws.mode(mode)
        _, _, error, shown = assume(
            tmp_path / sub, stub_aws, ref={**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN}, as_process=True
        )
        everything = shown + json.dumps(error)
        for secret in (KEY_ID, SECRET, TOKEN, STS_KEY, STS_SECRET, STS_TOKEN):
            assert secret not in everything


# ---- the methods and the credential type, against the contracts ------------------------------


def integration():
    return yaml.safe_load((INTEGRATION / "integration.yaml").read_text())


def test_the_three_methods_are_script_methods_that_produce_aws_sdk():
    methods = integration()["authMethods"]
    assert set(methods) == {"access-key", "cloud-identity", "assume-role"}
    for name, script, accepts, inputs in (
        ("access-key", "auth/access_key.py", ["aws-access-key"], set()),
        ("cloud-identity", "auth/cloud_identity.py", ["cloud-identity"], {"allowInstanceProfile"}),
        (
            "assume-role",
            "auth/assume_role.py",
            ["aws-access-key", "cloud-identity"],
            {"roleArn", "externalId", "sessionName", "allowInstanceProfile"},
        ),
    ):
        method = methods[name]
        assert method["script"] == script and "native" not in method
        assert method["produces"] == "aws/sdk" and method["accepts"] == accepts
        assert (INTEGRATION / script).is_file()
        assert set(method.get("inputs", {})) == inputs
    assume_role = methods["assume-role"]["inputs"]
    assert assume_role["roleArn"]["required"] is True
    assert assume_role["sessionName"]["default"] == "runwhen"
    assert "region" not in {n for m in methods.values() for n in m.get("inputs", {})}


def test_the_access_key_convention_shared_with_the_kubernetes_eks_method_is_noted():
    text = (INTEGRATION / "integration.yaml").read_text()
    assert "kubernetes" in text and "eks" in text and "AWS_SESSION_TOKEN" in text


def test_the_credential_type_is_env_only_secrets_and_paths_never_an_id_or_a_region(tmp_path):
    ctype = integration()["credentialTypes"]["aws/sdk"]
    assert ctype["materialize"] == {"env": ENV_NAMES}  # one target per plan: env, no file
    assert "AWS_REGION" not in str(
        ctype
    )  # a region is neither secret nor path: it would be redacted
    assert "content" not in ctype  # a script method matches references by kind or content token
    _, env, _, _ = run_script(tmp_path, "access_key", ref={**GOOD_REF, "AWS_SESSION_TOKEN": TOKEN})
    assert list(env) == ctype["materialize"]["env"][:3]
    # every name any script writes is one the type lists
    (tmp_path / "ci").mkdir()
    _, ci_env, _, _ = run_script(tmp_path / "ci", "cloud_identity", extra_env=ambient(tmp_path))
    assert set(ci_env) <= set(ctype["materialize"]["env"])


@pytest.mark.parametrize("method", ["access-key", "cloud-identity", "assume-role"])
def test_a_compiled_auth_plan_for_each_method_fits_the_auth_plan_schema(method):
    doc = integration()
    spec = doc["authMethods"][method]
    inputs = {
        "access-key": {},
        "cloud-identity": {"allowInstanceProfile": False},
        "assume-role": {"roleArn": ROLE, "sessionName": "runwhen", "allowInstanceProfile": False},
    }[method]
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
        "inputs": inputs,
        "materialize": doc["credentialTypes"][spec["produces"]]["materialize"],
    }
    assert validate("auth-plan", plan) == []
    assert set(plan["inputs"]) <= set(spec.get("inputs", {}))


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
