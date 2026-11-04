"""Auth script for the `service-principal` method of the azure integration (D67, CONTRACTS F7).

Runs in the executor, inside the azure@1 runtime, before the task script. It signs the az CLI in
as the service principal and leaves the session in a directory the task then uses:

    az login --service-principal -u <clientId> -p @<reference file> --tenant <tenantId> --allow-no-subscriptions
    az account set --subscription <subscriptionId>

and writes one `NAME=value` line to $RW_AUTH_OUT_ENV:

    AZURE_CONFIG_DIR=<dirname of $RW_AUTH_OUT_ENV>/azure   (az's token cache and profile; mode 0700)

The only environment variables it reads are the auth-script contract's: RW_AUTH_REF_DIR,
RW_AUTH_OUT_ENV, RW_AUTH_OUT_ERROR and RW_AUTH_INPUT_<NAME> (the executor strips every other RW_*
variable). az's session goes into `azure/` beside the file
RW_AUTH_OUT_ENV names: that directory is the task's private secrets directory, deleted when the
task ends. The subscription is not a variable of the credential type (it would be a materialized
value, redacted from every output); the task passes `--subscription` from the resource it targets,
and this script only proves the identity can see it.

`--allow-no-subscriptions` lets az log in a service principal that has no role assignment
anywhere; `az account set` then refuses the subscription, which is reported as permission_denied
(without it az fails with "No subscriptions found", which is mapped to the same code).

The client secret never goes on argv: az expands `-p @<file>` by reading the file, so the
reference's own file is passed. Reference: the secret is the key `clientSecret`,
`AZURE_CLIENT_SECRET` or `value` of $RW_AUTH_REF_DIR; exactly one of them must exist. Inputs:
$RW_AUTH_INPUT_<NAME>, the input name in SCREAMING_SNAKE as the SDK's input_env_name() makes it
(`tenantId` is `TENANT_ID`; INPUT_VARS lists them and a test checks them against the SDK). On a bad
reference, input or login it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and
exits 1. It never prints the secret, and no message it writes contains az's stderr: az prints the
AADSTS code of a refusal, and that code is the only part of it that is kept.

The helpers from GUID to `select_subscription` are the same in managed_identity.py (a test keeps
them equal).
"""

import json
import os
import re
import subprocess
import sys

SECRET_KEYS = ("clientSecret", "AZURE_CLIENT_SECRET", "value")
INPUT_VARS = {"tenantId": "TENANT_ID", "clientId": "CLIENT_ID", "subscriptionId": "SUBSCRIPTION_ID"}
TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")
GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
LOGIN_TIMEOUT = 120  # seconds for az login; az account set gets the same
AADSTS = re.compile(r"AADSTS(\d{3,7})")
# AADSTS codes whose cause is an input, not the credential: the tenant does not exist
INPUT_CODES = {"90002", "900023"}
AADSTS_MEANING = {
    "7000215": "the client secret is invalid",
    "7000222": "the client secret has expired",
    "700016": "the application is not in that tenant",
    "90002": "the tenant was not found",
    "900023": "the tenant identifier is not valid",
    "7000218": "the application needs a client secret or assertion",
    "500011": "the resource principal was not found in the tenant",
    "53003": "a conditional access policy blocked the sign-in",
}
# Metadata servers (the managed identity endpoint of a node or a container) are link-local: an HTTP
# proxy in the environment must not be asked for them.
NO_PROXY = ("169.254.169.254", "169.254.170.2", "localhost", "127.0.0.1")


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def single_line(name, value):
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", f"{name} must be a single line without NUL")
    return value


def read_input(env, name, var, *, required=True):
    """Input `name` (camelCase, e.g. tenantId) from RW_AUTH_INPUT_<var>, where <var> is the name the
    SDK gives the input (input_env_name: TENANT_ID); None when optional and unset."""
    value = env.get(f"RW_AUTH_INPUT_{var}")
    if value is not None and value != "":
        return single_line(f"input {name}", value)
    if required:
        raise AuthError("invalid_input", f"input {name} is required")
    return None


def guid(name, value):
    if not GUID.match(value):
        raise AuthError("invalid_input", f"input {name} must be a GUID")
    return value.lower()


def config_dir(env):
    """`azure/` beside the file RW_AUTH_OUT_ENV names, created 0700. That directory is the task's
    private secrets directory (0700, deleted when the task ends), so az's token cache lives exactly
    as long as the task and nowhere else."""
    out = env.get("RW_AUTH_OUT_ENV")
    parent = os.path.dirname(os.path.abspath(out)) if out else ""
    if not parent or not os.path.isdir(parent):
        raise AuthError("internal", "RW_AUTH_OUT_ENV is not set to a file in an existing directory")
    path = os.path.join(parent, "azure")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def az_env(env, config):
    """The environment az runs in: the caller's, with az's state in `config` (az has no credential
    variables of its own: it reads the session from AZURE_CONFIG_DIR, so pinning it is what keeps
    another session out), no telemetry, nothing that could make it stop and ask, and no proxy for
    the metadata servers. The auth-script variables (RW_*) are left out: az has no use for them."""
    merged = {k: v for k, v in env.items() if not k.startswith("RW_")}
    merged.update(
        {
            "AZURE_CONFIG_DIR": config,
            "AZURE_CORE_COLLECT_TELEMETRY": "false",
            "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
            "AZURE_CORE_NO_COLOR": "true",
        }
    )
    for name in ("NO_PROXY", "no_proxy"):
        have = [h for h in merged.get(name, "").split(",") if h.strip()]
        merged[name] = ",".join(have + [h for h in NO_PROXY if h not in have])
    return merged


def run_az(argv, env):
    """(returncode, stderr) of `az ...`. Output is discarded: az login prints the account list."""
    try:
        done = subprocess.run(
            ["az", *argv],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=LOGIN_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise AuthError("dependency", "az is not installed in this runtime") from None
    except subprocess.TimeoutExpired:
        raise AuthError("timeout", f"az did not finish within {LOGIN_TIMEOUT}s") from None
    return done.returncode, done.stderr or ""


_NETWORK = re.compile(
    r"max retries exceeded|failed to establish a new connection|name or service not known"
    r"|temporary failure in name resolution|getaddrinfo|connection (?:refused|reset|aborted)"
    r"|connectionerror|connection error|\bssl\b|sslerror|proxyerror|network is unreachable|no route to host",
    re.I,
)
_TIMEOUT = re.compile(r"timed out|timeout", re.I)
_NO_SUBSCRIPTION = re.compile(r"No subscriptions found|doesn't exist in cloud", re.I)


def login_failure(stderr, what):
    """AuthError for a failed `az login`/`az account set`; the message holds no text of stderr
    except the AADSTS code and its meaning. A connection failure is checked before a timeout, as
    az.py does: "Failed to establish a new connection ... Connection timed out" is unreachable."""
    found = AADSTS.search(stderr)
    if found:
        number = found.group(1)
        meaning = AADSTS_MEANING.get(number)
        code = "invalid_input" if number in INPUT_CODES else "permission_denied"
        return AuthError(
            code, f"{what} was refused (AADSTS{number}{': ' + meaning if meaning else ''})"
        )
    if _NO_SUBSCRIPTION.search(stderr):
        return AuthError(
            "permission_denied",
            f"{what}: the identity can see no subscription (no role assignment on any)",
        )
    if _NETWORK.search(stderr):
        return AuthError("unreachable", f"{what} could not reach Microsoft Entra ID")
    if _TIMEOUT.search(stderr):
        return AuthError("timeout", f"{what} timed out")
    return AuthError("internal", f"{what} failed")


def select_subscription(subscription, run_env, who):
    """`az account set`: with `az login --allow-no-subscriptions` a login succeeds for an identity
    that has no role assignment anywhere, and this is where that shows."""
    code, stderr = run_az(["account", "set", "--subscription", subscription], run_env)
    if code != 0:
        if _NO_SUBSCRIPTION.search(stderr) or re.search(
            r"doesn't exist|does not exist|not found", stderr, re.I
        ):
            raise AuthError(
                "permission_denied",
                f"{who} cannot see that subscription: it has no role assignment"
                " there, or the subscription does not exist",
            )
        raise login_failure(stderr, "selecting the subscription")


def read_secret_path(ref_dir):
    """The path of the reference file that holds the client secret, after checking it: exactly
    one of the accepted keys exists, is text, is not empty and has no newline inside it."""
    if not ref_dir or not os.path.isdir(ref_dir):
        raise AuthError("invalid_input", "the credential reference was not materialized")
    present = [k for k in SECRET_KEYS if os.path.exists(os.path.join(ref_dir, k))]
    if len(present) != 1:
        raise AuthError(
            "invalid_input",
            "the credential reference must hold exactly one of the keys "
            + ", ".join(SECRET_KEYS)
            + (f"; it holds {', '.join(present)}" if present else "; it holds none of them"),
        )
    path = os.path.join(ref_dir, present[0])
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        raise AuthError(
            "invalid_input", f"the credential key {present[0]} cannot be read as text"
        ) from None
    # a value written with `echo` or an editor ends in one newline that is not part of it
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise AuthError("invalid_input", f"the credential key {present[0]} is empty")
    single_line("the client secret", text)
    return path


def read_inputs(env):
    """(tenant, client id, subscription id), validated."""
    tenant = read_input(env, "tenantId", INPUT_VARS["tenantId"])
    if not (GUID.match(tenant) or TENANT.match(tenant)):
        raise AuthError("invalid_input", "input tenantId must be a tenant GUID or domain name")
    client = guid("clientId", read_input(env, "clientId", INPUT_VARS["clientId"]))
    subscription = guid(
        "subscriptionId", read_input(env, "subscriptionId", INPUT_VARS["subscriptionId"])
    )
    return tenant, client, subscription


def build(env):
    """Sign az in; the ordered (NAME, value) pairs of the credential type."""
    tenant, client, subscription = read_inputs(env)
    secret_path = read_secret_path(env.get("RW_AUTH_REF_DIR"))
    config = config_dir(env)
    run_env = az_env(env, config)
    # rc 0 with warnings on stderr is a success: az login warns about -p even when it works
    code, stderr = run_az(
        [
            "login",
            "--service-principal",
            "-u",
            client,
            "-p",
            f"@{secret_path}",
            "--tenant",
            tenant,
            "--allow-no-subscriptions",
            "--output",
            "none",
        ],
        run_env,
    )
    if code != 0:
        raise login_failure(stderr, "the service principal sign-in")
    select_subscription(subscription, run_env, "the service principal")
    return [("AZURE_CONFIG_DIR", config)]


def main(env=None):
    env = os.environ if env is None else env
    try:
        pairs = build(env)
        out = env.get("RW_AUTH_OUT_ENV")
        if not out:
            raise AuthError("internal", "RW_AUTH_OUT_ENV is not set")
        with open(out, "w", encoding="utf-8") as handle:
            for name, value in pairs:
                handle.write(f"{name}={value}\n")
    except AuthError as exc:
        target = env.get("RW_AUTH_OUT_ERROR")
        if target:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"code": exc.code, "message": exc.message}) + "\n")
        print(f"auth/service_principal.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
