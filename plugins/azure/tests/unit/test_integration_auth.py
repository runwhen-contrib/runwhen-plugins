"""The azure integration's two script auth methods: the scripts (run as the executor runs them,
with temp dirs and a fake `az` on PATH), their fit with the auth-plan contract, and the types.

The fake `az` records its argv and the environment it saw, and reads `-p @<file>` the way the real
CLI does (verified against az 2.92.0 in the azure@1 image: the file's content, minus its trailing
newline, is the value). Nothing here reaches Microsoft Entra ID."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
INTEGRATION = PLUGIN / "integration"
SP_SCRIPT = INTEGRATION / "auth" / "service_principal.py"
MI_SCRIPT = INTEGRATION / "auth" / "managed_identity.py"
SECRET = "s3cr3t~value/with'odd\\chars"
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
SUB = "33333333-3333-3333-3333-333333333333"
ENV_NAMES = ["AZURE_CONFIG_DIR", "AZURE_SUBSCRIPTION_ID"]

# What the fake az does, chosen by FAKE_AZ_MODE. Stderr texts are what az 2.92.0 printed in the
# azure@1 image (capture of 2026-11-04) or what Microsoft documents for the code.
FAKE_AZ = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_AZ_MODE", "ok")
    seen = {{"argv": argv, "config": os.environ.get("AZURE_CONFIG_DIR"), "secret_file": None}}
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
            sys.stderr.write("ERROR: Subscription '%s' not found. Check the spelling and casing and try again.\\n" % argv[-1])
            sys.exit(1)
        sys.exit(0)
    if mode == "ok-warning":
        sys.stderr.write("WARNING: Authentication with username and password in the command line is strongly discouraged.\\n")
        sys.exit(0)
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
    creds = tmp_path / "creds"
    ref_dir.mkdir(exist_ok=True)
    creds.mkdir(exist_ok=True)
    for key, value in (ref if ref is not None else {"clientSecret": SECRET}).items():
        (ref_dir / key).write_text(value, encoding="utf-8")
    env = {
        "PATH": f"{fake_az}{os.pathsep}/usr/bin:/bin",
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(tmp_path / "out.env"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "out.error"),
        "RW_CREDENTIALS_DIR": str(creds),
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
        code, shown = load(script).main(env), ""
    out_env, out_error, log = tmp_path / "out.env", tmp_path / "out.error", tmp_path / "az.log"
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
        "--tenant", TENANT, "--output", "none",
    ]  # fmt: skip
    assert select["argv"] == ["account", "set", "--subscription", SUB]
    assert login["secret_file"] == SECRET


def test_service_principal_writes_the_credential_types_two_variables(tmp_path, fake_az):
    r = run_script(tmp_path, fake_az)
    config = tmp_path / "creds" / "azure"
    assert list(r.env) == ENV_NAMES
    assert r.env == {"AZURE_CONFIG_DIR": str(config), "AZURE_SUBSCRIPTION_ID": SUB}
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    # az ran with its state under the credentials dir, not under a home directory
    assert {c["config"] for c in r.calls} == {str(config)}


def test_the_secret_is_never_on_argv_in_the_output_env_or_printed(tmp_path, fake_az):
    r = run_script(tmp_path, fake_az, as_process=True)
    assert r.code == 0 and r.shown == ""
    for call in r.calls:
        assert not any(SECRET in a or SECRET.split("/")[0] in a for a in call["argv"])
    assert SECRET not in (tmp_path / "out.env").read_text()


def test_a_success_with_warnings_on_stderr_is_a_success(tmp_path, fake_az):
    # az login warns about -p on stderr and exits 0: the exit code decides
    r = run_script(tmp_path, fake_az, mode="ok-warning")
    assert (r.code, r.error) == (0, None)
    assert r.env["AZURE_SUBSCRIPTION_ID"] == SUB


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
    assert r.code == 0 and r.env["AZURE_SUBSCRIPTION_ID"] == SUB
    assert r.calls[0]["argv"][3] == CLIENT and r.calls[0]["argv"][7] == "contoso.onmicrosoft.com"  # fmt: skip


def test_inputs_are_read_with_or_without_the_underscore(tmp_path, fake_az):
    flat = {"TENANTID": TENANT, "CLIENTID": CLIENT, "SUBSCRIPTIONID": SUB}
    assert run_script(tmp_path, fake_az, inputs=flat).code == 0


@pytest.mark.parametrize(
    "mode,code,needle",
    [
        ("bad-secret", "permission_denied", "AADSTS7000215: the client secret is invalid"),
        ("app-not-found", "permission_denied", "AADSTS700016"),
        ("tenant-not-found", "invalid_input", "AADSTS90002"),
        ("network", "unreachable", "Microsoft Entra ID"),
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
    r = run_script(tmp_path, fake_az, mode="no-subscription")
    assert r.code == 1 and r.error["code"] == "permission_denied"
    assert "cannot see that subscription" in r.error["message"]


def test_a_missing_az_is_a_dependency_error(tmp_path):
    r = run_script(tmp_path, tmp_path / "nowhere")
    assert r.code == 1 and r.error["code"] == "dependency"


def test_a_login_that_hangs_is_a_timeout(tmp_path, fake_az, monkeypatch):
    mod = load(SP_SCRIPT)
    monkeypatch.setattr(mod, "LOGIN_TIMEOUT", 0.3)
    env = run_env_for(tmp_path, fake_az, "sleep")
    assert mod.main(env) == 1
    assert json.loads((tmp_path / "out.error").read_text())["code"] == "timeout"


def run_env_for(tmp_path, fake_az, mode):
    """The environment run_script builds, for a test that calls main() itself."""
    ref_dir, creds = tmp_path / "ref", tmp_path / "creds"
    ref_dir.mkdir()
    creds.mkdir()
    (ref_dir / "clientSecret").write_text(SECRET)
    return {
        "PATH": f"{fake_az}{os.pathsep}/usr/bin:/bin",
        "RW_AUTH_REF_DIR": str(ref_dir),
        "RW_AUTH_OUT_ENV": str(tmp_path / "out.env"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "out.error"),
        "RW_CREDENTIALS_DIR": str(creds),
        "FAKE_AZ_MODE": mode,
        "FAKE_AZ_LOG": str(tmp_path / "az.log"),
        "RW_AUTH_INPUT_TENANT_ID": TENANT,
        "RW_AUTH_INPUT_CLIENT_ID": CLIENT,
        "RW_AUTH_INPUT_SUBSCRIPTION_ID": SUB,
    }


@pytest.mark.parametrize("script", [SP_SCRIPT, MI_SCRIPT])
def test_a_missing_credentials_dir_is_internal(tmp_path, fake_az, script):
    r = run_script(tmp_path, fake_az, script, extra_env={"RW_CREDENTIALS_DIR": str(tmp_path / "x")})
    assert r.code == 1 and r.error["code"] == "internal" and r.calls == []


@pytest.mark.parametrize("script", [SP_SCRIPT, MI_SCRIPT])
def test_a_failure_exits_non_zero_so_the_task_run_fails(tmp_path, fake_az, script):
    r = run_script(tmp_path, fake_az, script, inputs={}, as_process=True)
    assert r.code == 1 and r.error["code"] == "invalid_input"


def test_a_missing_reference_directory_is_invalid_input(tmp_path, fake_az):
    env = run_env_for(tmp_path, fake_az, "ok")
    del env["RW_AUTH_REF_DIR"]
    assert load(SP_SCRIPT).main(env) == 1
    assert json.loads((tmp_path / "out.error").read_text())["code"] == "invalid_input"


# ---- managed-identity --------------------------------------------------------------------------


def test_managed_identity_logs_in_with_the_system_identity_and_needs_no_reference(
    tmp_path, fake_az
):
    r = run_script(tmp_path, fake_az, MI_SCRIPT, ref={})
    assert (r.code, r.error) == (0, None)
    login, select = r.calls
    assert login["argv"] == ["login", "--identity", "--output", "none"]
    assert select["argv"] == ["account", "set", "--subscription", SUB]
    assert r.env == {
        "AZURE_CONFIG_DIR": str(tmp_path / "creds" / "azure"),
        "AZURE_SUBSCRIPTION_ID": SUB,
    }
    assert list(r.env) == ENV_NAMES


def test_managed_identity_passes_a_user_assigned_client_id(tmp_path, fake_az):
    inputs = {"SUBSCRIPTION_ID": SUB, "CLIENT_ID": CLIENT}
    r = run_script(tmp_path, fake_az, MI_SCRIPT, inputs=inputs, mode="ok-warning")
    assert r.code == 0
    assert r.calls[0]["argv"] == ["login", "--identity", "--output", "none", "--client-id", CLIENT]


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
        "credentials_dir",
        "config_dir",
        "az_env",
        "run_az",
        "login_failure",
        "GUID",
        "LOGIN_TIMEOUT",
        "AADSTS",
        "INPUT_CODES",
        "AADSTS_MEANING",
        "_NETWORK",
        "_TIMEOUT",
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


def test_the_types_are_a_subscription_root_and_the_key_vault_below_it():
    types = yaml.safe_load((INTEGRATION / "types.yaml").read_text())["types"]
    assert set(types) == {"subscription", "keyvault"}
    assert types["subscription"]["root"] is True
    # rootKey names a connection input, and the canonical key is the same id (C3)
    assert types["subscription"]["rootKey"] == "subscriptionId"
    assert types["subscription"]["canonicalKey"] == "subscriptionId"
    for method in integration()["authMethods"].values():
        assert types["subscription"]["rootKey"] in method["inputs"]
    assert types["keyvault"]["parent"] == "subscription"


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
