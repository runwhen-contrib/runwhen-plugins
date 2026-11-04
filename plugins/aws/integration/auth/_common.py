"""What the aws auth scripts share (D67, CONTRACTS F7). Not a method: `integration.yaml` names no
script called `_common`; the three scripts import it from their own directory.

The scripts run in the executor under the SDK's child discipline: every `RW_*` variable is
stripped except the ones the contract hands over (RW_AUTH_REF_DIR, RW_AUTH_OUT_ENV,
RW_AUTH_OUT_FILE, RW_AUTH_OUT_ERROR and RW_AUTH_INPUT_<NAME>), so a script reads no other `RW_*`
name (not the credentials directory the SDK keeps for itself). An input called `roleArn` arrives as RW_AUTH_INPUT_ROLE_ARN
(camelCase to SCREAMING_SNAKE). The `aws/sdk` type materializes secrets and paths only: never an
id or a region, because every materialized value is redacted from all task output.

A script never prints a credential and no message it writes contains one."""

import json
import os
import re
import sys

ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN = "AWS_SESSION_TOKEN"
KEYS = (ACCESS_KEY_ID, SECRET_ACCESS_KEY, SESSION_TOKEN)
# IAM: an access key id is 16 to 128 upper-case letters and digits
KEY_ID = re.compile(r"[A-Z0-9]{16,128}")
# a secret key or session token is base64-like; whitespace, control characters and quotes are
# never part of one, and one would corrupt the env file
SECRET_VALUE = re.compile(r"[A-Za-z0-9/+=_.~:-]{16,4096}")

TOKEN_FILE = "AWS_WEB_IDENTITY_TOKEN_FILE"
ROLE_ARN = "AWS_ROLE_ARN"
CONTAINER_URIS = ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def read_key(ref_dir, name):
    """The text of key `name`, or None when absent; one trailing newline is dropped."""
    path = os.path.join(ref_dir, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        raise AuthError(
            "invalid_input", f"the credential key {name} cannot be read as text"
        ) from None
    # a value written with `echo` or an editor ends in one newline that is not part of it
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return text


def read_credentials(ref_dir):
    """[(NAME, value)] for the keys the reference holds, validated; the secrets are never quoted."""
    if not ref_dir or not os.path.isdir(ref_dir):
        raise AuthError("invalid_input", "the credential reference was not materialized")
    found = {name: read_key(ref_dir, name) for name in KEYS}
    for name in (ACCESS_KEY_ID, SECRET_ACCESS_KEY):
        if found[name] is None:
            raise AuthError("invalid_input", f"the credential reference has no key {name}")
    pairs = []
    for name in KEYS:
        value = found[name]
        if value is None:
            continue
        if not value:
            raise AuthError("invalid_input", f"the credential key {name} is empty")
        pattern = KEY_ID if name == ACCESS_KEY_ID else SECRET_VALUE
        if not pattern.fullmatch(value):
            # shape only: the message never repeats the value
            raise AuthError(
                "invalid_input",
                f"the credential key {name} is not a valid {name.removeprefix('AWS_').lower()}: "
                "it must be a single line of the characters AWS issues (no spaces or quotes)",
            )
        pairs.append((name, value))
    return pairs


def has_access_key(ref_dir):
    """True when the reference holds an access key id (an access-key reference, not an ambient one)."""
    return bool(ref_dir) and os.path.exists(os.path.join(ref_dir, ACCESS_KEY_ID))


def truthy(value):
    """An input of type boolean arrives as the text `true` or `false`."""
    return str(value or "").strip().lower() == "true"


def check_ambient(env, allow_instance_profile):
    """Raises invalid_input unless the executor runs with an ambient AWS identity: IRSA
    (AWS_WEB_IDENTITY_TOKEN_FILE and AWS_ROLE_ARN) or pod identity / ECS
    (AWS_CONTAINER_CREDENTIALS_FULL_URI or _RELATIVE_URI), or the input allowInstanceProfile says
    the instance profile is meant. Without one the CLI would walk its provider chain down to the
    instance metadata service and fail there, slowly and with an unrelated message. The message
    names the variables that are missing, never a value."""
    token_file, role_arn = env.get(TOKEN_FILE), env.get(ROLE_ARN)
    if token_file and role_arn:
        if not os.path.isfile(token_file):
            raise AuthError(
                "invalid_input", f"the file named by {TOKEN_FILE} does not exist in this executor"
            )
        return "web-identity"
    if any(env.get(name) for name in CONTAINER_URIS):
        return "container"
    if allow_instance_profile:
        return "instance-profile"
    if token_file and not role_arn:
        missing = f"{ROLE_ARN} is not set (IRSA needs it beside {TOKEN_FILE})"
    elif role_arn and not token_file:
        missing = f"{TOKEN_FILE} is not set (IRSA needs it beside {ROLE_ARN})"
    else:
        missing = (
            f"{TOKEN_FILE} with {ROLE_ARN} (IRSA), or {CONTAINER_URIS[0]} / {CONTAINER_URIS[1]} "
            "(pod identity) is not set"
        )
    raise AuthError(
        "invalid_input",
        f"no ambient AWS identity in this executor: {missing}. Set the input "
        "allowInstanceProfile to true to use the instance profile",
    )


def write_env(env, pairs):
    out = env.get("RW_AUTH_OUT_ENV")
    if not out:
        raise AuthError("internal", "RW_AUTH_OUT_ENV is not set")
    with open(out, "w", encoding="utf-8") as handle:
        for name, value in pairs:
            handle.write(f"{name}={value}\n")


def run(script, build, env=None):
    """The shared main: write build(env) to $RW_AUTH_OUT_ENV; on an AuthError write one
    `{code, message}` line to $RW_AUTH_OUT_ERROR, say the message on stderr and return 1."""
    env = os.environ if env is None else env
    try:
        write_env(env, build(env))
    except AuthError as exc:
        target = env.get("RW_AUTH_OUT_ERROR")
        if target:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"code": exc.code, "message": exc.message}) + "\n")
        print(f"{script}: {exc.message}", file=sys.stderr)
        return 1
    return 0
