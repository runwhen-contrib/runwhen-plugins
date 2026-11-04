"""Auth script for the `cloud-identity` method of the aws integration (D67, CONTRACTS F7).

The identity is ambient: IRSA (AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE), EKS Pod Identity
(AWS_CONTAINER_CREDENTIALS_FULL_URI) or an instance profile is already in the executor's
environment and the aws CLI finds it by itself, so nothing is read from a reference and no key is
written. The script's job is to check that such an identity is there, so a missing one fails here
with a message naming what is missing instead of as a slow instance-metadata timeout in the task:

    AWS_WEB_IDENTITY_TOKEN_FILE and AWS_ROLE_ARN, or
    AWS_CONTAINER_CREDENTIALS_FULL_URI / AWS_CONTAINER_CREDENTIALS_RELATIVE_URI,
    or the input allowInstanceProfile = true (the instance profile is meant)

otherwise it exits 1 with `invalid_input` on $RW_AUTH_OUT_ERROR. It never prints a value.

A plan has to produce something, and the `aws/sdk` type materializes only secrets and paths, so
the script writes the paths of an empty config file and an empty shared credentials file in its
private temp directory, as AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE. That also keeps the
CLI to the environment: no profile, SSO session or credential_process of a config file the
executor may hold can take over the ambient identity.
"""

import os
import sys
import tempfile

from _common import check_ambient, run, truthy


def empty_files(env):
    """(config path, credentials path): two empty 0600 files in a new 0700 directory."""
    directory = tempfile.mkdtemp(prefix="aws-", dir=env.get("TMPDIR") or None)
    paths = []
    for name in ("config", "credentials"):
        path = os.path.join(directory, name)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        paths.append(path)
    return paths


def build(env):
    check_ambient(env, truthy(env.get("RW_AUTH_INPUT_ALLOW_INSTANCE_PROFILE")))
    config, credentials = empty_files(env)
    return [("AWS_CONFIG_FILE", config), ("AWS_SHARED_CREDENTIALS_FILE", credentials)]


def main(env=None):
    return run("auth/cloud_identity.py", build, env)


if __name__ == "__main__":
    sys.exit(main())
