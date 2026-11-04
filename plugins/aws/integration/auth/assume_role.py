"""Auth script for the `assume-role` method of the aws integration (D67, CONTRACTS F7).

Turns base credentials into the temporary credentials of an IAM role, by running
`aws sts assume-role` in the executor and writing the result to $RW_AUTH_OUT_ENV:

    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

The base credentials are either an access key reference (the reference holds AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY, the same convention as the `access-key` method and the kubernetes `eks`
method) or the executor's ambient identity (IRSA, pod identity, or an instance profile with the
input allowInstanceProfile = true, exactly the checks of the `cloud-identity` method). Inputs:

    roleArn           required, arn:<partition>:iam::<account>:role/<name>
    externalId        optional, the role's ExternalId condition
    sessionName       default `runwhen`, the role session name shown in CloudTrail
    allowInstanceProfile  default false, as for `cloud-identity`

The three values go from the CLI's output pipe straight into the env file: never on a command
line, never on this script's stdout or stderr. The base keys reach the CLI through its
environment. Instance metadata and container credential hosts are exempted from any HTTP proxy
(NO_PROXY), because the proxy cannot reach them. A failure writes one `{code, message}` line to
$RW_AUTH_OUT_ERROR (the message names the AWS error code, nothing else of the CLI's output) and
exits 1.
"""

import json
import os
import re
import subprocess
import sys

from _common import (
    AuthError,
    SECRET_VALUE,
    KEY_ID,
    ACCESS_KEY_ID,
    SECRET_ACCESS_KEY,
    SESSION_TOKEN,
    check_ambient,
    has_access_key,
    read_credentials,
    run,
    truthy,
)

ROLE = re.compile(
    r"arn:(aws|aws-cn|aws-us-gov|aws-iso|aws-iso-b|aws-eusc):iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]{1,512}"
)
EXTERNAL_ID = re.compile(r"[A-Za-z0-9_+=,.@:/-]{2,1224}")
SESSION_NAME = re.compile(r"[A-Za-z0-9_+=,.@-]{2,64}")
DEFAULT_SESSION = "runwhen"
CALL_TIMEOUT = 45  # seconds for the sts call
# STS is a global service with a regional endpoint per partition; the call needs a region
PARTITION_REGION = {
    "aws": "us-east-1",
    "aws-cn": "cn-north-1",
    "aws-us-gov": "us-gov-west-1",
    "aws-iso": "us-iso-east-1",
    "aws-iso-b": "us-isob-east-1",
    "aws-eusc": "eusc-de-east-1",
}
# hosts of the instance metadata and container credential endpoints: never through a proxy
METADATA_HOSTS = ("169.254.169.254", "169.254.170.2", "[fd00:ec2::254]", "localhost", "127.0.0.1")
# a stderr phrase or error code -> the D64 code of the failure
FAILURES = (
    (
        re.compile(
            r"AccessDenied|not authorized|ExpiredToken|InvalidClientTokenId|SignatureDoesNotMatch|Unable to locate credentials|RegionDisabled|InvalidIdentityToken|IDPCommunicationError"
        ),
        "permission_denied",
    ),
    (re.compile(r"Throttling|TooManyRequests|RequestLimitExceeded"), "rate_limited"),
    (
        re.compile(
            r"Could not connect|Could not resolve|Connect timeout|Read timeout|SSL validation|Proxy connection"
        ),
        "unreachable",
    ),
    (re.compile(r"ValidationError|ParamValidation|MalformedPolicy"), "invalid_input"),
)
ERROR_CODE = re.compile(r"An error occurred \(([A-Za-z0-9_.:-]+)\)")


def read_inputs(env):
    """(role arn, external id or None, session name): validated, nothing is echoed back."""
    role = env.get("RW_AUTH_INPUT_ROLE_ARN") or ""
    if not ROLE.fullmatch(role):
        raise AuthError(
            "invalid_input",
            "input roleArn must be an IAM role ARN such as arn:aws:iam::123456789012:role/rw-read",
        )
    external = env.get("RW_AUTH_INPUT_EXTERNAL_ID") or None
    if external is not None and not EXTERNAL_ID.fullmatch(external):
        raise AuthError(
            "invalid_input",
            "input externalId must be 2 to 1224 characters of letters, digits and _+=,.@:/-",
        )
    session = env.get("RW_AUTH_INPUT_SESSION_NAME") or DEFAULT_SESSION
    if not SESSION_NAME.fullmatch(session):
        raise AuthError(
            "invalid_input",
            "input sessionName must be 2 to 64 characters of letters, digits and _+=,.@-",
        )
    return role, external, session


def child_env(env, base_pairs):
    """The environment of the aws CLI: the executor's, the base keys when there are any, no
    profile or config file to take over, and the metadata hosts out of any proxy."""
    child = {k: v for k, v in env.items() if not k.startswith("RW_")}
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        child.pop(name, None)
    if base_pairs:
        # an access key reference: only its keys, whatever else the executor has
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            child.pop(name, None)
        child.update(dict(base_pairs))
    child["AWS_CONFIG_FILE"] = os.devnull
    child["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    for name in ("NO_PROXY", "no_proxy"):
        have = [h for h in child.get(name, "").split(",") if h]
        child[name] = ",".join(have + [h for h in METADATA_HOSTS if h not in have])
    return child


def failure(returncode, stderr):
    """The AuthError for a failed sts call: the AWS error code, never the CLI's text."""
    text = stderr or ""
    match = ERROR_CODE.search(text)
    code = next((c for rx, c in FAILURES if rx.search(text)), "internal")
    what = match.group(1) if match else f"exit {returncode}"
    return AuthError(
        code,
        f"aws sts assume-role failed ({what}); check the role ARN, its trust policy and the base credentials",
    )


def assume(env, role, external, session, base_pairs):
    partition = role.split(":")[1]
    argv = [
        "aws", "sts", "assume-role",
        "--role-arn", role,
        "--role-session-name", session,
        *(["--external-id", external] if external else []),
        "--region", PARTITION_REGION[partition],
        "--output", "json",
        "--no-cli-pager",
    ]  # fmt: skip
    try:
        done = subprocess.run(
            argv,
            env=child_env(env, base_pairs),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CALL_TIMEOUT,
        )
    except FileNotFoundError:
        raise AuthError("dependency", "the aws CLI is not installed in this runtime") from None
    except subprocess.TimeoutExpired:
        raise AuthError(
            "timeout", f"aws sts assume-role did not finish in {CALL_TIMEOUT}s"
        ) from None
    if done.returncode != 0:
        raise failure(done.returncode, done.stderr)
    try:
        credentials = json.loads(done.stdout)["Credentials"]
        values = [
            credentials["AccessKeyId"],
            credentials["SecretAccessKey"],
            credentials["SessionToken"],
        ]
    except (ValueError, KeyError, TypeError):
        raise AuthError("internal", "aws sts assume-role returned no credentials") from None
    ok = (
        isinstance(values[0], str) and KEY_ID.fullmatch(values[0])
        and all(isinstance(v, str) and SECRET_VALUE.fullmatch(v) for v in values[1:])
    )  # fmt: skip
    if not ok:
        raise AuthError(
            "internal", "aws sts assume-role returned credentials of an unexpected shape"
        )
    return list(zip((ACCESS_KEY_ID, SECRET_ACCESS_KEY, SESSION_TOKEN), values))


def build(env):
    role, external, session = read_inputs(env)
    ref_dir = env.get("RW_AUTH_REF_DIR")
    if has_access_key(ref_dir):
        base = read_credentials(ref_dir)
    else:
        try:
            check_ambient(env, truthy(env.get("RW_AUTH_INPUT_ALLOW_INSTANCE_PROFILE")))
        except AuthError as exc:
            # the reference may be an access key reference that lacks its key
            raise AuthError(
                exc.code, f"{exc.message}; or the reference has to hold {ACCESS_KEY_ID}"
            ) from None
        base = []
    return assume(env, role, external, session, base)


def main(env=None):
    return run("auth/assume_role.py", build, env)


if __name__ == "__main__":
    sys.exit(main())
