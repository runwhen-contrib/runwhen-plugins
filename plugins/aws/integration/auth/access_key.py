"""Auth script for the `access-key` method of the aws integration (D67, CONTRACTS F7).

Runs in the executor, in the aws runtime, before the task script. It turns the credential
reference (a Secret, Vault path or file holding an IAM access key) and the connection's input into
the environment of the `aws/sdk` type, by writing `NAME=value` lines to $RW_AUTH_OUT_ENV:

    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY [AWS_SESSION_TOKEN] AWS_REGION

Reference: the keys of $RW_AUTH_REF_DIR are the standard variable names, so a Secret written for
any AWS tool works as it is: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required,
AWS_SESSION_TOKEN is optional (temporary credentials). Input: $RW_AUTH_INPUT_REGION, default
us-east-1. On a bad reference or input it writes one `{code, message}` JSON line to
$RW_AUTH_OUT_ERROR and exits 1. It never prints a key, and no message it writes contains one.
"""

import json
import os
import re
import sys

ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN = "AWS_SESSION_TOKEN"
KEYS = (ACCESS_KEY_ID, SECRET_ACCESS_KEY, SESSION_TOKEN)
DEFAULT_REGION = "us-east-1"
# us-east-1, eu-central-2, ap-southeast-4, us-gov-west-1, cn-north-1, us-iso-east-1
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}")
# IAM: an access key id is 16 to 128 upper-case letters and digits
KEY_ID = re.compile(r"[A-Z0-9]{16,128}")
# a secret key or session token is base64-like; whitespace, control characters and quotes are
# never part of one, and one would corrupt the env file
SECRET_VALUE = re.compile(r"[A-Za-z0-9/+=_.~:-]{16,4096}")


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


def read_region(env):
    value = env.get("RW_AUTH_INPUT_REGION")
    if value is None or value == "":
        return DEFAULT_REGION
    if not REGION.fullmatch(value):
        raise AuthError("invalid_input", "input region must be a region name such as eu-west-1")
    return value


def build(env):
    """The aws/sdk environment as an ordered list of (NAME, value)."""
    region = read_region(env)
    return [*read_credentials(env.get("RW_AUTH_REF_DIR")), ("AWS_REGION", region)]


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
        print(f"auth/access_key.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
