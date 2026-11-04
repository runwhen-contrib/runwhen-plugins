"""Auth script for the `managed-identity` method of the azure integration (D67, CONTRACTS F7).

Runs in the executor, inside the azure@1 runtime, before the task script. The managed identity of
the pod or node is ambient (the reference is a `cloud-identity`: nothing to read), so this signs
the az CLI in with it and leaves the session in a directory the task then uses:

    az login --identity [--client-id <clientId>]
    az account set --subscription <subscriptionId>

and writes `NAME=value` lines to $RW_AUTH_OUT_ENV:

    AZURE_CONFIG_DIR=$RW_CREDENTIALS_DIR/azure     (az's token cache and profile; mode 0700)
    AZURE_SUBSCRIPTION_ID=<subscriptionId>

Inputs: $RW_AUTH_INPUT_<NAME> with the input name in SCREAMING_SNAKE (`SUBSCRIPTION_ID`); the same
name without the underscore (`SUBSCRIPTIONID`) is read too. `clientId` is optional: set it for a
user-assigned identity, leave it empty for the system-assigned one. On a bad input or login it
writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and exits 1. No message it writes
contains az's stderr: az prints the AADSTS code of a refusal, and that code is the only part of it
that is kept. There is no secret anywhere in this method.

The helpers below up to `read_inputs` are the same in service_principal.py (a test keeps them equal).
"""

import json
import os
import re
import subprocess
import sys

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


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def single_line(name, value):
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", f"{name} must be a single line without NUL")
    return value


def read_input(env, name, *, required=True):
    """Input `name` (camelCase, e.g. tenantId) from RW_AUTH_INPUT_*; None when optional and unset."""
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()
    for key in (snake, snake.replace("_", "")):
        value = env.get(f"RW_AUTH_INPUT_{key}")
        if value is not None and value != "":
            return single_line(f"input {name}", value)
    if required:
        raise AuthError("invalid_input", f"input {name} is required")
    return None


def guid(name, value):
    if not GUID.match(value):
        raise AuthError("invalid_input", f"input {name} must be a GUID")
    return value.lower()


def credentials_dir(env):
    base = env.get("RW_CREDENTIALS_DIR")
    if not base or not os.path.isdir(base):
        raise AuthError("internal", "RW_CREDENTIALS_DIR is not set to a directory")
    return base


def config_dir(env):
    """$RW_CREDENTIALS_DIR/azure, created 0700: az's token cache lives here for this task run."""
    path = os.path.join(credentials_dir(env), "azure")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def az_env(env, config):
    """The environment az runs in: the caller's, with az's state in `config`, no telemetry, and
    nothing that could make it stop and ask."""
    merged = dict(env)
    merged.update(
        {
            "AZURE_CONFIG_DIR": config,
            "AZURE_CORE_COLLECT_TELEMETRY": "false",
            "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
            "AZURE_CORE_NO_COLOR": "true",
        }
    )
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


def login_failure(stderr, what):
    """AuthError for a failed `az login`/`az account set`; the message holds no text of stderr
    except the AADSTS code and its meaning."""
    found = AADSTS.search(stderr)
    if found:
        number = found.group(1)
        meaning = AADSTS_MEANING.get(number)
        code = "invalid_input" if number in INPUT_CODES else "permission_denied"
        return AuthError(
            code, f"{what} was refused (AADSTS{number}{': ' + meaning if meaning else ''})"
        )
    if _TIMEOUT.search(stderr):
        return AuthError("timeout", f"{what} timed out")
    if _NETWORK.search(stderr):
        return AuthError("unreachable", f"{what} could not reach Microsoft Entra ID")
    return AuthError("internal", f"{what} failed")


def read_inputs(env):
    """(subscription id, client id or None), validated."""
    subscription = guid("subscriptionId", read_input(env, "subscriptionId"))
    client = read_input(env, "clientId", required=False)
    return subscription, (guid("clientId", client) if client else None)


def build(env):
    """Sign az in with the ambient identity; the ordered (NAME, value) pairs of the credential type."""
    subscription, client = read_inputs(env)
    config = config_dir(env)
    run_env = az_env(env, config)
    argv = ["login", "--identity", "--output", "none"]
    if client:
        argv += ["--client-id", client]
    code, stderr = run_az(argv, run_env)
    if code != 0:
        if re.search(
            r"169\.254\.169\.254|identity endpoint|IDENTITY_ENDPOINT|managed identity", stderr, re.I
        ) and not AADSTS.search(stderr):
            raise AuthError(
                "unreachable", "the managed identity endpoint did not answer: no identity here?"
            )
        raise login_failure(stderr, "the managed identity sign-in")
    code, stderr = run_az(["account", "set", "--subscription", subscription], run_env)
    if code != 0:
        if re.search(r"doesn't exist|does not exist|not found|not recognized", stderr, re.I):
            raise AuthError(
                "permission_denied",
                "the managed identity cannot see that subscription: it has no role assignment"
                " there, or the subscription does not exist",
            )
        raise login_failure(stderr, "selecting the subscription")
    return [("AZURE_CONFIG_DIR", config), ("AZURE_SUBSCRIPTION_ID", subscription)]


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
        print(f"auth/managed_identity.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
