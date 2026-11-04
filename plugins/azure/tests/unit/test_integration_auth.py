"""The azure integration's two script auth methods: the scripts, run two ways with a fake `az` on
PATH (nothing here reaches Microsoft Entra ID), their fit with the auth-plan contract, and the types.

  - through the SDK's `run_script_plan`, exactly as the executor runs them: the host environment
    minus every RW_* variable, a private HOME and TMPDIR, the working directory of the plugin root,
    RW_AUTH_REF_DIR / RW_AUTH_OUT_ENV / RW_AUTH_OUT_ERROR / RW_AUTH_INPUT_<NAME> set, the output
    parsed against the plan's `materialize`. These tests need an SDK that has
    `runwhen_capability.plugin.auth_script` and skip without one.
  - directly (`main(env)` with a hand-made env, or as a process), for the many error paths.

The scripts may read no variable but those of the auth-script contract; the second way hands them a
strict environment that fails the test on any other RW_* name.

The fake `az` records its argv and the environment it saw, and reads `-p @<file>` the way the real
CLI does (verified against az 2.92.0 in the azure@1 image: the file's content, minus its trailing
newline, is the value). Its stderr texts are what az 2.92.0 printed in that image or read from its
source (the `account set` refusal and "No subscriptions found" are from `_profile.py`), or what
Microsoft documents for the AADSTS code."""

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from runwhen_capability.contracts import validate
from runwhen_capability.custom.manifest import input_env_name

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
SP_SCRIPT = INTEGRATION / "auth" / "service_principal.py"
MI_SCRIPT = INTEGRATION / "auth" / "managed_identity.py"
SECRET = "s3cr3t~value/with'odd\\chars"
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
SUB = "33333333-3333-3333-3333-333333333333"
ENV_NAMES = ["AZURE_CONFIG_DIR"]
# the only RW_ variables an auth script may read (CONTRACTS.md, "Auth plan"): everything else, the
# executor strips from its environment
ALLOWED_RW = {"RW_AUTH_REF_DIR", "RW_AUTH_OUT_ENV", "RW_AUTH_OUT_FILE", "RW_AUTH_OUT_ERROR"}

# What the fake az does, chosen by FAKE_AZ_MODE.
FAKE_AZ = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_AZ_MODE", "ok")
    seen = {{
        "argv": argv,
        "config": os.environ.get("AZURE_CONFIG_DIR"),
        "home": os.environ.get("HOME"),
        "rw_names": sorted(k for k in os.environ if k.startswith("RW_")),
        "no_proxy": os.environ.get("NO_PROXY"),
        "secret_file": None,
    }}
    if "-p" in argv:
        value = argv[argv.index("-p") + 1]
        if value.startswith("@"):
            with open(value[1:], encoding="utf-8") as handle:
                seen["secret_file"] = handle.read().rstrip("\\r\\n")
    with open(os.environ["FAKE_AZ_LOG"], "a") as log:
        log.write(json.dumps(seen) + "\\n")
    secret = os.environ.get("FAKE_AZ_SECRET", "")
    cmd = argv[:2]
    if mode == "sleep":
        time.sleep(30)
    if cmd == ["account", "set"]:
        if mode == "no-subscription":
            sys.stderr.write("ERROR: The subscription of '%s' doesn't exist in cloud 'AzureCloud'.\\n" % argv[-1])
            sys.exit(1)
        sys.exit(0)
    if mode == "ok-warning":
        sys.stderr.write("WARNING: Authentication with username and password in the command line is strongly discouraged.\\n")
        sys.exit(0)
    if mode == "no-subscriptions-at-login":
        sys.stderr.write("ERROR: No subscriptions found for %s.\\n" % argv[argv.index("-u") + 1])
        sys.exit(1)
    if mode == "bad-secret":
        sys.stderr.write("ERROR: AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app. %s Trace ID: 0000 Correlation ID: 1111\\n" % secret)
        sys.exit(1)
    if mode == "tenant-not-found":
        sys.stderr.write("ERROR: The command failed with an unexpected error. Here is the traceback:\\nERROR: Unable to get authority configuration. AADSTS90002: Tenant 'x' not found. %s\\nTraceback (most recent call last):\\n" % secret)
        sys.exit(1)
    if mode == "app-not-found":
        sys.stderr.write("ERROR: AADSTS700016: Application with identifier 'x' was not found in the directory 'y'.\\n")
        sys.exit(1)
    if mode == "network":
        sys.stderr.write("ERROR: The command failed with an unexpected error. Here is the traceback:\\nERROR: HTTPSConnectionPool(host='login.microsoftonline.com', port=443): Max retries exceeded with url: /x (Caused by NewConnectionError('Failed to establish a new connection: [Errno -2] Name or service not known')) %s\\n" % secret)
        sys.exit(1)
    if mode == "network-timeout":
        sys.stderr.write("ERROR: HTTPSConnectionPool(host='login.microsoftonline.com', port=443): Max retries exceeded with url: /x (Caused by NewConnectionError('Failed to establish a new connection: [Errno 110] Connection timed out'))\\n")
        sys.exit(1)
    if mode == "read-timeout":
        sys.stderr.write("ERROR: HTTPSConnectionPool(host='login.microsoftonline.com', port=443): Read timed out. (read timeout=300)\\n")
        sys.exit(1)
    if mode == "no-imds":
        sys.stderr.write("ERROR: The command failed with an unexpected error. Here is the traceback:\\nERROR: HTTPConnectionPool(host='169.254.169.254', port=80): Max retries exceeded with url: /metadata/identity/oauth2/token (Caused by NewConnectionError('Failed to establish a new connection: [Errno 111] Connection refused'))\\n")
        sys.exit(1)
    if mode == "weird":
        sys.stderr.write("ERROR: something nobody has seen before %s\\n" % secret)
        sys.exit(3)
    sys.exit(0)
    """
)


@pytest.fixture
def fake_az(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(FAKE_AZ.format(python=sys.executable))
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    return bin_dir


class StrictEnv(dict):
    """An environment that fails the test when a script reads an RW_ variable the auth-script
    contract does not give it (RW_CREDENTIALS_DIR, say): the executor strips those."""

    @staticmethod
    def check(key):
        if key.startswith("RW_") and key not in ALLOWED_RW and not key.startswith("RW_AUTH_INPUT_"):
            raise AssertionError(f"the auth script read {key}, which the executor does not give it")

    def get(self, key, default=None):
        self.check(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.check(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.check(key)
        return super().__contains__(key)


def load(script):
    spec = importlib.util.spec_from_file_location(f"azure_auth_{script.stem}", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Run:
    def __init__(self, code, env, error, shown, calls, tmp_path):
        self.code, self.env, self.error, self.shown, self.calls = code, env, error, shown, calls
        self.tmp_path = tmp_path


def run_script(
    tmp_path,
    fake_az,
    script=SP_SCRIPT,
    *,
    ref=None,
    inputs=None,
    mode="ok",
    as_process=False,
    extra_env=None,
):
    """Run the auth script as the executor does: reference dir, inputs and output files in env."""
    ref_dir = tmp_path / "ref"
    out_dir = tmp_path / "out"
    ref_dir.mkdir(exist_ok=True)
    out_dir.mkdir(mode=0o700, exist_ok=True)
    for key, value in (ref if ref is not None else {"clientSecret": SECRET}).items():
        (ref_dir / key).write_text(value, encoding="utf-8")
    env = {
        "PATH": f"{fake_az}{os.pathsep}/usr/bin:/bin",
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(out_dir / "env"),
        "RW_AUTH_OUT_ERROR": str(out_dir / "error"),
        "FAKE_AZ_MODE": mode,
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
        "FAKE_AZ_SECRET": SECRET,
    }
    if inputs is None:
        inputs = (
            {"TENANT_ID": TENANT, "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB}
            if script == SP_SCRIPT
            else {"SUBSCRIPTION_ID": SUB}
        )
    env.update({f"RW_AUTH_INPUT_{k}": v for k, v in inputs.items()})
    env.update(extra_env or {})
    if as_process:
        done = subprocess.run(
            [sys.executable, str(script)], env=env, capture_output=True, text=True, cwd=INTEGRATION
        )
        code, shown = done.returncode, done.stdout + done.stderr
    else:
        code, shown = load(script).main(StrictEnv(env)), ""
    out_env, out_error, log = out_dir / "env", out_dir / "error", tmp_path / "az.log"
    parsed = None
    if out_env.exists():
        parsed = dict(line.split("=", 1) for line in out_env.read_text().splitlines())
    error = json.loads(out_error.read_text()) if out_error.exists() else None
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return Run(code, parsed, error, shown, calls, tmp_path)


# ---- service-principal -------------------------------------------------------------------------


def test_service_principal_logs_in_with_the_secret_as_a_file_and_selects_the_subscription(
    tmp_path, fake_az
):
    r = run_script(tmp_path, fake_az)
    assert (r.code, r.error) == (0, None)
    login, select = r.calls
    ref_file = str(tmp_path / "ref" / "clientSecret")
    assert login["argv"] == [
        "login", "--service-principal", "-u", CLIENT, "-p", f"@{ref_file}",
        "--tenant", TENANT, "--allow-no-subscriptions", "--output", "none",
    ]  # fmt: skip
    assert select["argv"] == ["account", "set", "--subscription", SUB]
    assert login["secret_file"] == SECRET


def test_service_principal_writes_the_credential_types_one_variable_beside_its_output_file(
    tmp_path, fake_az
):
    r = run_script(tmp_path, fake_az)
    config = tmp_path / "out" / "azure"  # dirname($RW_AUTH_OUT_ENV)/azure
    assert list(r.env) == ENV_NAMES
    assert r.env == {
        "AZURE_CONFIG_DIR": str(config)
    }  # no subscription: it is not a secret or a path
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    # az ran with its state there, and was given none of the auth-script variables
    assert {c["config"] for c in r.calls} == {str(config)}
    assert all(c["rw_names"] == [] for c in r.calls)
    assert all("169.254.169.254" in c["no_proxy"] for c in r.calls)


def test_the_secret_is_never_on_argv_in_the_output_env_or_printed(tmp_path, fake_az):
    r = run_script(tmp_path, fake_az, as_process=True)
    assert r.code == 0 and r.shown == ""
    for call in r.calls:
        assert not any(SECRET in a or SECRET.split("/")[0] in a for a in call["argv"])
    assert SECRET not in (tmp_path / "out" / "env").read_text()


def test_a_success_with_warnings_on_stderr_is_a_success(tmp_path, fake_az):
    # az login warns about -p on stderr and exits 0: the exit code decides
    r = run_script(tmp_path, fake_az, mode="ok-warning")
    assert (r.code, r.error) == (0, None)
    assert list(r.env) == ENV_NAMES


@pytest.mark.parametrize("key", ["clientSecret", "AZURE_CLIENT_SECRET", "value"])
def test_the_secret_is_read_from_each_accepted_key(tmp_path, fake_az, key):
    r = run_script(tmp_path, fake_az, ref={key: SECRET})
    assert r.code == 0 and r.calls[0]["secret_file"] == SECRET
    assert r.calls[0]["argv"][5] == f"@{tmp_path / 'ref' / key}"


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_one_trailing_newline_is_not_part_of_the_secret(tmp_path, fake_az, ending):
    r = run_script(tmp_path, fake_az, ref={"clientSecret": "abc" + ending})
    assert r.code == 0 and r.calls[0]["secret_file"] == "abc"


@pytest.mark.parametrize(
    "ref,why",
    [
        ({}, "none of them"),
        ({"clientSecret": "a", "value": "b"}, "clientSecret, value"),
        ({"token": "a"}, "none of them"),
        ({"clientSecret": ""}, "empty"),
        ({"clientSecret": "\n"}, "empty"),
        ({"clientSecret": "line1\nline2"}, "single line"),
        ({"clientSecret": "a\x00b"}, "single line"),
        ({"clientSecret": "a\n\n"}, "single line"),
    ],
)
def test_a_reference_without_exactly_one_usable_secret_is_invalid_input(
    tmp_path, fake_az, ref, why
):
    r = run_script(tmp_path, fake_az, ref=ref)
    assert r.code == 1 and r.env is None and r.calls == []  # az never ran
    assert r.error["code"] == "invalid_input" and why in r.error["message"]
    assert set(r.error) == {"code", "message"}


@pytest.mark.parametrize(
    "inputs",
    [
        {"CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB},  # no tenant
        {"TENANT_ID": TENANT, "SUBSCRIPTION_ID": SUB},  # no client
        {"TENANT_ID": TENANT, "CLIENT_ID": CLIENT},  # no subscription
        {"TENANT_ID": TENANT + "\nAZURE_SUBSCRIPTION_ID=x", "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB},
        {"TENANT_ID": TENANT, "CLIENT_ID": "not-a-guid", "SUBSCRIPTION_ID": SUB},
        {"TENANT_ID": TENANT, "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB + "\n"},
        {"TENANT_ID": TENANT, "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": "my subscription"},
        {"TENANT_ID": "-bad", "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB},
        {"TENANT_ID": "a b", "CLIENT_ID": CLIENT, "SUBSCRIPTION_ID": SUB},
    ],
)  # fmt: skip
def test_a_bad_input_is_invalid_input_and_runs_nothing(tmp_path, fake_az, inputs):
    r = run_script(tmp_path, fake_az, inputs=inputs)
    assert r.code == 1 and r.env is None and r.calls == []
    assert r.error["code"] == "invalid_input"


def test_a_tenant_domain_is_accepted_and_ids_are_lower_cased(tmp_path, fake_az):
    inputs = {
        "TENANT_ID": "contoso.onmicrosoft.com",
        "CLIENT_ID": CLIENT.upper(),
        "SUBSCRIPTION_ID": SUB.upper(),
    }
    r = run_script(tmp_path, fake_az, inputs=inputs)
    assert r.code == 0 and list(r.env) == ENV_NAMES
    assert r.calls[1]["argv"][-1] == SUB  # lower-cased for az account set
    assert r.calls[0]["argv"][3] == CLIENT and r.calls[0]["argv"][7] == "contoso.onmicrosoft.com"  # fmt: skip


def test_inputs_are_read_by_the_sdks_names_and_the_squashed_spelling_is_not_read(tmp_path, fake_az):
    flat = {"TENANTID": TENANT, "CLIENTID": CLIENT, "SUBSCRIPTIONID": SUB}
    r = run_script(tmp_path, fake_az, inputs=flat)
    assert r.code == 1 and r.error["code"] == "invalid_input" and r.calls == []


@pytest.mark.parametrize(
    "mode,code,needle",
    [
        ("bad-secret", "permission_denied", "AADSTS7000215: the client secret is invalid"),
        ("app-not-found", "permission_denied", "AADSTS700016"),
        ("tenant-not-found", "invalid_input", "AADSTS90002"),
        ("network", "unreachable", "Microsoft Entra ID"),
        # a refused connection that mentions "timed out" is unreachable; a bare read timeout is a timeout
        ("network-timeout", "unreachable", "Microsoft Entra ID"),
        ("read-timeout", "timeout", "timed out"),
        ("no-subscriptions-at-login", "permission_denied", "can see no subscription"),
        ("weird", "internal", "failed"),
    ],
)
def test_a_failed_login_is_classified_and_the_message_holds_none_of_az_stderr(
    tmp_path, fake_az, mode, code, needle
):
    r = run_script(tmp_path, fake_az, mode=mode, as_process=True)
    assert r.code == 1 and r.env is None
    assert r.error["code"] == code and needle in r.error["message"]
    # az's stderr echoed the secret; neither the message nor anything printed carries it
    assert SECRET not in json.dumps(r.error) and SECRET not in r.shown
    assert "Traceback" not in r.error["message"] and "Trace ID" not in r.error["message"]
    assert len(r.calls) == 1  # no account set after a failed login


def test_a_subscription_the_identity_cannot_see_is_permission_denied(tmp_path, fake_az):
    # --allow-no-subscriptions lets a service principal with no role assignment log in; the refusal
    # comes from `az account set`, with az's own wording
    r = run_script(tmp_path, fake_az, mode="no-subscription")
    assert r.code == 1 and r.error["code"] == "permission_denied"
    assert "cannot see that subscription" in r.error["message"]
    assert [c["argv"][:2] for c in r.calls] == [
        ["login", "--service-principal"],
        ["account", "set"],
    ]
    assert r.env is None


def test_a_missing_az_is_a_dependency_error(tmp_path):
    r = run_script(tmp_path, tmp_path / "nowhere")
    assert r.code == 1 and r.error["code"] == "dependency"


def test_a_login_that_hangs_is_a_timeout(tmp_path, fake_az, monkeypatch):
    mod = load(SP_SCRIPT)
    monkeypatch.setattr(mod, "LOGIN_TIMEOUT", 0.3)
    env = run_env_for(tmp_path, fake_az, "sleep")
    assert mod.main(env) == 1
    assert json.loads((tmp_path / "out" / "error").read_text())["code"] == "timeout"


def run_env_for(tmp_path, fake_az, mode):
    """The environment run_script builds, for a test that calls main() itself."""
    ref_dir, out_dir = tmp_path / "ref", tmp_path / "out"
    ref_dir.mkdir()
    out_dir.mkdir(mode=0o700)
    (ref_dir / "clientSecret").write_text(SECRET)
    return StrictEnv(
        {
            "PATH": f"{fake_az}{os.pathsep}/usr/bin:/bin",
            "RW_AUTH_REF_DIR": str(ref_dir),
            "RW_AUTH_OUT_ENV": str(out_dir / "env"),
            "RW_AUTH_OUT_ERROR": str(out_dir / "error"),
            "FAKE_AZ_MODE": mode,
            "FAKE_AZ_LOG": str(tmp_path / "az.log"),
            "RW_AUTH_INPUT_TENANT_ID": TENANT,
            "RW_AUTH_INPUT_CLIENT_ID": CLIENT,
            "RW_AUTH_INPUT_SUBSCRIPTION_ID": SUB,
        }
    )


@pytest.mark.parametrize("script", [SP_SCRIPT, MI_SCRIPT])
@pytest.mark.parametrize("where", ["unset", "missing-directory"])
def test_an_output_file_that_has_no_directory_is_internal_and_az_never_runs(
    tmp_path, fake_az, script, where
):
    extra = {"RW_AUTH_OUT_ENV": str(tmp_path / "nowhere" / "env")} if where != "unset" else {}
    if where == "unset":
        env = run_env_for(tmp_path, fake_az, "ok")
        del env["RW_AUTH_OUT_ENV"]
        if script == MI_SCRIPT:
            del env["RW_AUTH_REF_DIR"]
        assert load(script).main(env) == 1
        error = json.loads((tmp_path / "out" / "error").read_text())
    else:
        r = run_script(tmp_path, fake_az, script, extra_env=extra)
        assert r.code == 1 and r.calls == []
        error = r.error
    assert error["code"] == "internal" and "RW_AUTH_OUT_ENV" in error["message"]
    assert not (tmp_path / "az.log").exists()


@pytest.mark.parametrize("script", [SP_SCRIPT, MI_SCRIPT])
def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path, fake_az, script):
    r = run_script(tmp_path, fake_az, script, inputs={}, as_process=True)
    assert r.code == 1 and r.error["code"] == "invalid_input"


def test_a_missing_reference_directory_is_invalid_input(tmp_path, fake_az):
    env = run_env_for(tmp_path, fake_az, "ok")
    del env["RW_AUTH_REF_DIR"]
    assert load(SP_SCRIPT).main(env) == 1
    assert json.loads((tmp_path / "out" / "error").read_text())["code"] == "invalid_input"


# ---- managed-identity --------------------------------------------------------------------------


def test_managed_identity_logs_in_with_the_system_identity_and_needs_no_reference(
    tmp_path, fake_az
):
    r = run_script(tmp_path, fake_az, MI_SCRIPT, ref={})
    assert (r.code, r.error) == (0, None)
    login, select = r.calls
    assert login["argv"] == ["login", "--identity", "--allow-no-subscriptions", "--output", "none"]
    assert select["argv"] == ["account", "set", "--subscription", SUB]
    assert r.env == {"AZURE_CONFIG_DIR": str(tmp_path / "out" / "azure")}
    assert list(r.env) == ENV_NAMES


def test_managed_identity_passes_a_user_assigned_client_id(tmp_path, fake_az):
    inputs = {"SUBSCRIPTION_ID": SUB, "CLIENT_ID": CLIENT}
    r = run_script(tmp_path, fake_az, MI_SCRIPT, inputs=inputs, mode="ok-warning")
    assert r.code == 0
    assert r.calls[0]["argv"] == [
        "login", "--identity", "--allow-no-subscriptions", "--output", "none", "--client-id", CLIENT
    ]  # fmt: skip


@pytest.mark.parametrize(
    "inputs", [{}, {"SUBSCRIPTION_ID": "x"}, {"SUBSCRIPTION_ID": SUB, "CLIENT_ID": "nope"}]
)
def test_managed_identity_rejects_bad_inputs(tmp_path, fake_az, inputs):
    r = run_script(tmp_path, fake_az, MI_SCRIPT, inputs=inputs)
    assert r.code == 1 and r.error["code"] == "invalid_input" and r.calls == []


def test_managed_identity_without_an_identity_endpoint_is_unreachable(tmp_path, fake_az):
    r = run_script(tmp_path, fake_az, MI_SCRIPT, mode="no-imds")
    assert r.code == 1 and r.error["code"] == "unreachable"
    assert "169.254" not in r.error["message"]


def test_managed_identity_runs_as_a_plain_script_and_prints_nothing(tmp_path, fake_az):
    r = run_script(tmp_path, fake_az, MI_SCRIPT, as_process=True)
    assert r.code == 0 and r.shown == ""


# ---- the two scripts share their helpers ---------------------------------------------------------


def test_the_shared_helpers_are_identical_in_both_scripts():
    import ast

    def definitions(script):
        tree = ast.parse(script.read_text())
        out = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                out[node.name] = ast.dump(node)
            elif isinstance(node, ast.Assign):
                out[node.targets[0].id] = ast.dump(node)
        return out

    sp, mi = definitions(SP_SCRIPT), definitions(MI_SCRIPT)
    shared = (
        "AuthError",
        "single_line",
        "read_input",
        "guid",
        "config_dir",
        "az_env",
        "run_az",
        "login_failure",
        "select_subscription",
        "GUID",
        "LOGIN_TIMEOUT",
        "AADSTS",
        "INPUT_CODES",
        "AADSTS_MEANING",
        "NO_PROXY",
        "_NETWORK",
        "_TIMEOUT",
        "_NO_SUBSCRIPTION",
    )
    for name in shared:
        assert name in sp and sp[name] == mi[name], name


# ---- the methods and the credential type, against the contracts ------------------------------------


def integration():
    return yaml.safe_load((INTEGRATION / "integration.yaml").read_text())


def test_both_methods_are_script_methods_that_produce_azure_cli():
    methods = integration()["authMethods"]
    assert set(methods) == {"service-principal", "managed-identity"}
    for method in methods.values():
        assert "native" not in method and method["produces"] == "azure/cli"
        assert (INTEGRATION / method["script"]).is_file()
    assert methods["service-principal"]["accepts"] == ["k8s-secret", "vault", "file"]
    assert methods["managed-identity"]["accepts"] == ["cloud-identity"]


def test_the_inputs_are_what_the_scripts_read():
    methods = integration()["authMethods"]
    sp, mi = methods["service-principal"]["inputs"], methods["managed-identity"]["inputs"]
    assert set(sp) == {"tenantId", "clientId", "subscriptionId"}
    assert all(v["required"] for v in sp.values())
    assert set(mi) == {"subscriptionId", "clientId"}
    assert mi["subscriptionId"]["required"] is True and mi["clientId"]["required"] is False


def test_the_scripts_read_no_rw_variable_but_the_auth_script_contracts():
    """CONTRACTS.md "Auth plan": RW_AUTH_REF_DIR, RW_AUTH_OUT_ENV / RW_AUTH_OUT_FILE, RW_AUTH_OUT_ERROR
    and RW_AUTH_INPUT_<NAME>. The executor strips every other RW_* (RW_CREDENTIALS_DIR is a task-time
    variable, not an auth-script one), so a script that read it would see nothing."""
    for script in (SP_SCRIPT, MI_SCRIPT):
        names = set(re.findall(r"\bRW_[A-Z_]+", script.read_text()))
        assert names - {"RW_AUTH_INPUT_"} <= ALLOWED_RW, sorted(names - ALLOWED_RW)
        assert "RW_CREDENTIALS_DIR" not in script.read_text()


@pytest.mark.parametrize(
    "method,script", [("service-principal", SP_SCRIPT), ("managed-identity", MI_SCRIPT)]
)
def test_the_input_variable_names_are_the_sdks_input_env_names(method, script):
    declared = integration()["authMethods"][method]["inputs"]
    names = load(script).INPUT_VARS
    assert set(names) == set(declared)
    for name, var in names.items():
        assert var == input_env_name(name), name  # tenantId -> TENANT_ID
    assert "TENANT_ID" == input_env_name("tenantId")


def test_the_credential_type_holds_no_subscription_or_other_identifier():
    ctype = integration()["credentialTypes"]["azure/cli"]
    assert ctype["materialize"] == {"env": ["AZURE_CONFIG_DIR"]}
    assert "AZURE_SUBSCRIPTION_ID" not in json.dumps(integration())
    for script in (SP_SCRIPT, MI_SCRIPT):
        assert "AZURE_SUBSCRIPTION_ID" not in script.read_text()


def test_the_credential_type_is_env_only_and_the_scripts_write_exactly_those_names(
    tmp_path, fake_az
):
    ctype = integration()["credentialTypes"]["azure/cli"]
    assert ctype["materialize"] == {"env": ENV_NAMES}
    assert "content" not in ctype  # script methods match references by kind
    for script in (SP_SCRIPT, MI_SCRIPT):
        sub = tmp_path / script.stem
        sub.mkdir()
        assert list(run_script(sub, fake_az, script).env) == ctype["materialize"]["env"]


@pytest.mark.parametrize("method", ["service-principal", "managed-identity"])
def test_a_compiled_auth_plan_for_each_method_fits_the_auth_plan_schema(method):
    doc = integration()
    m = doc["authMethods"][method]
    reference = (
        {"kind": "k8s-secret", "ref": "runwhen/azure-sp"}
        if method == "service-principal"
        else {"kind": "cloud-identity"}
    )
    plan = {
        "connection": "prod-sub",
        "integration": "azure@0.1.0",
        "authMethod": method,
        "credentialType": m["produces"],
        "reference": reference,
        "method": {
            "script": {
                "path": f"integration/{m['script']}",
                "archive": {"sha256": "sha256:" + "0" * 64, "size": 4096},
            }
        },
        "inputs": {name: "x" for name in m["inputs"] if m["inputs"][name]["required"]},
        "materialize": doc["credentialTypes"][m["produces"]]["materialize"],
    }
    assert validate("auth-plan", plan) == []


def test_the_types_are_a_subscription_root_a_resource_group_and_the_key_vault_below_it():
    types = yaml.safe_load((INTEGRATION / "types.yaml").read_text())["types"]
    assert set(types) == {"subscription", "resourcegroup", "keyvault"}
    assert types["subscription"]["root"] is True
    # rootKey names a connection input, and the canonical key is the same id (C3)
    assert types["subscription"]["rootKey"] == "subscriptionId"
    assert types["subscription"]["canonicalKey"] == "subscriptionId"
    for method in integration()["authMethods"].values():
        assert types["subscription"]["rootKey"] in method["inputs"]
    assert types["resourcegroup"]["parent"] == "subscription"
    assert types["keyvault"]["parent"] == "resourcegroup"
    assert [types[t]["plural"] for t in types] == ["subscriptions", "resourcegroups", "keyvaults"]


def test_the_permission_schema_takes_actions_and_data_actions():
    import jsonschema

    schema = json.loads((INTEGRATION / "schemas" / "azure-permission.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    ok = [
        {"action": "Microsoft.KeyVault/vaults/read"},
        {"action": "Microsoft.Insights/metrics/read"},
        {"action": "Microsoft.OperationalInsights/workspaces/query/read"},
        {"action": "Microsoft.ResourceHealth/availabilityStatuses/read"},
        {"dataAction": "Microsoft.KeyVault/vaults/secrets/readMetadata/action"},
        {"dataAction": "Microsoft.KeyVault/vaults/keys/read"},
    ]
    bad = [
        {},
        {"action": "vaults/read"},
        {"action": "Microsoft.KeyVault"},
        {
            "action": "Microsoft.KeyVault/vaults/read",
            "dataAction": "Microsoft.KeyVault/vaults/keys/read",
        },
        {"action": "Microsoft.KeyVault/vaults/read", "privilege": "x"},
        {"verb": "get", "resources": ["pods"]},
    ]
    validator = jsonschema.Draft202012Validator(schema)
    assert all(validator.is_valid(p) for p in ok)
    assert not any(validator.is_valid(p) for p in bad)


# ---- through the SDK's run_script_plan, as the executor runs them ---------------------------------------


try:
    from runwhen_capability.plugin import auth_script
except ImportError:  # an SDK older than the auth-script runner: these tests skip, the rest run
    auth_script = None
from runwhen_capability._redaction import Redactor  # noqa: E402


def plan_for(method):
    doc = integration()
    m = doc["authMethods"][method]
    inputs = {"subscriptionId": SUB}
    if method == "service-principal":
        inputs.update(tenantId=TENANT, clientId=CLIENT)
    return {
        "connection": "prod-sub",
        "integration": "azure@0.1.0",
        "authMethod": method,
        "credentialType": m["produces"],
        "reference": {"kind": "k8s-secret", "ref": "runwhen/azure-sp"},
        "method": {
            "script": {"path": m["script"], "archive": {"sha256": "sha256:" + "0" * 64, "size": 1}}
        },
        "inputs": inputs,
        "materialize": doc["credentialTypes"][m["produces"]]["materialize"],
    }


class Executed:
    def __init__(self, result=None, error=None, work=None, calls=()):
        self.result, self.error, self.work, self.calls = result, error, work, list(calls)


@pytest.fixture
def executor(tmp_path, fake_az, monkeypatch):
    """execute(method, mode=..., reference=..., inputs=...): run_script_plan on the integration's own
    script with a fake az first on the PATH. The host environment is what the executor strips."""
    if auth_script is None:
        pytest.skip("needs an SDK with the auth-script runner (run_script_plan)")
    monkeypatch.setenv("PATH", f"{fake_az}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AZ_LOG", str(tmp_path / "az.log"))
    monkeypatch.setenv("FAKE_AZ_SECRET", SECRET)
    # variables a task host has: the executor must not let the script see them
    monkeypatch.setenv("RW_CREDENTIALS_DIR", str(tmp_path / "must-not-be-read"))
    monkeypatch.setenv("RW_EXECUTOR_TOKEN", "host-token-value")
    monkeypatch.setenv("FAKE_AZ_MODE", "ok")
    counter = iter(range(100))

    def execute(method="service-principal", mode="ok", reference=None, inputs=None):
        monkeypatch.setenv("FAKE_AZ_MODE", mode)
        plan = plan_for(method)
        plan["inputs"].update(inputs or {})
        script = plan["method"]["script"]["path"]
        files = {f"integration/{script}": (INTEGRATION / script).read_bytes()}
        work = tmp_path / f"auth-{next(counter)}"
        ref = [("clientSecret", SECRET.encode())] if method == "service-principal" else []
        try:
            result = auth_script.run_script_plan(
                plan,
                files=files,
                executable=frozenset(),
                reference=reference if reference is not None else ref,
                work_dir=work,
                timeout_s=60,
                redactor=Redactor([SECRET]),
            )
            error = None
        except auth_script.AuthScriptError as exc:
            result, error = None, exc
        log = tmp_path / "az.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if log.exists():
            log.unlink()
        return Executed(result, error, work, calls)

    return execute


@pytest.mark.parametrize("method", ["service-principal", "managed-identity"])
def test_the_executor_runs_the_script_and_gets_one_variable_under_the_tasks_secrets_dir(
    executor, method
):
    done = executor(method)
    assert done.error is None, done.error and done.error.message
    config = done.work / "out" / "azure"
    assert done.result.env == {"AZURE_CONFIG_DIR": str(config)}
    assert done.result.secrets == [str(config)]  # a materialized value: redacted from every output
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    login, select = done.calls
    # az saw the session directory, the executor's private HOME, and none of the host's RW_ variables
    assert login["config"] == select["config"] == str(config)
    assert login["home"] == str(done.work / "home")
    assert login["rw_names"] == [] and select["rw_names"] == []
    assert "--allow-no-subscriptions" in login["argv"]
    assert select["argv"] == ["account", "set", "--subscription", SUB]


def test_the_executor_hands_the_service_principal_secret_as_a_file_never_on_argv(executor):
    done = executor("service-principal")
    login = done.calls[0]
    assert login["secret_file"] == SECRET
    assert not any(SECRET in a for a in login["argv"])
    assert login["argv"][login["argv"].index("--tenant") + 1] == TENANT
    assert login["argv"][login["argv"].index("-u") + 1] == CLIENT
    assert done.result.env.keys() == {"AZURE_CONFIG_DIR"}


def test_a_user_assigned_identity_client_id_reaches_az_through_the_executors_input_names(executor):
    done = executor("managed-identity", inputs={"clientId": CLIENT})
    assert done.error is None
    assert done.calls[0]["argv"][-2:] == ["--client-id", CLIENT]


def test_every_declared_input_reaches_the_script_under_the_sdks_name(executor):
    # the executor names RW_AUTH_INPUT_<input_env_name(input)>: a script that read another spelling
    # would find nothing and fail with invalid_input
    for method in ("service-principal", "managed-identity"):
        done = executor(method)
        assert done.error is None, (method, done.error and done.error.message)


@pytest.mark.parametrize(
    "mode,code,needle",
    [
        ("bad-secret", "permission_denied", "AADSTS7000215"),
        ("tenant-not-found", "invalid_input", "AADSTS90002"),
        ("network", "unreachable", "Microsoft Entra ID"),
        ("no-subscription", "permission_denied", "cannot see that subscription"),
        ("no-subscriptions-at-login", "permission_denied", "no subscription"),
        ("weird", "internal", "failed"),
    ],
)
def test_a_failure_under_the_executor_carries_the_scripts_code_and_never_the_secret(
    executor, mode, code, needle
):
    done = executor("service-principal", mode=mode)
    assert done.result is None
    assert (done.error.code, done.error.scope) == (code, "auth")
    assert needle in done.error.message
    assert SECRET not in done.error.message


def test_a_bad_input_under_the_executor_is_invalid_input_and_az_never_runs(executor):
    done = executor("service-principal", inputs={"clientId": "nope"})
    assert done.error.code == "invalid_input" and done.calls == []


def test_a_reference_without_the_secret_under_the_executor_is_invalid_input(executor):
    done = executor("service-principal", reference=[("unrelated", b"x")])
    assert done.error.code == "invalid_input" and done.calls == []
