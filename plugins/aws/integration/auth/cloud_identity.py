"""Auth script for the `cloud-identity` method of the aws integration (D67, CONTRACTS F7).

The identity is ambient: IRSA (AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE), EKS Pod Identity or
an instance profile is already in the executor's environment and the aws CLI finds it by itself, so
nothing is read from a reference and no key is written. The script only turns the connection's input
into `AWS_REGION` in $RW_AUTH_OUT_ENV (the `aws/sdk` type's region for account-level calls):

    AWS_REGION=<input region, default us-east-1>

On a bad input it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and exits 1.
"""

import json
import os
import re
import sys

DEFAULT_REGION = "us-east-1"
# us-east-1, eu-central-2, ap-southeast-4, us-gov-west-1, cn-north-1, us-iso-east-1
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}")


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def build(env):
    """The aws/sdk environment as an ordered list of (NAME, value): the region only."""
    value = env.get("RW_AUTH_INPUT_REGION")
    if value is None or value == "":
        value = DEFAULT_REGION
    elif not REGION.fullmatch(value):
        raise AuthError("invalid_input", "input region must be a region name such as eu-west-1")
    return [("AWS_REGION", value)]


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
        print(f"auth/cloud_identity.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
