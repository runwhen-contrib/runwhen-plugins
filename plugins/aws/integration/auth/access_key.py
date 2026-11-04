"""Auth script for the `access-key` method of the aws integration (D67, CONTRACTS F7).

Runs in the executor, in the aws runtime, before the task script. It turns the credential
reference (a Secret, Vault path or file holding an IAM access key) into the environment of the
`aws/sdk` type, by writing `NAME=value` lines to $RW_AUTH_OUT_ENV:

    AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY [AWS_SESSION_TOKEN]

Reference: the keys of $RW_AUTH_REF_DIR are the standard variable names, so a Secret written for
any AWS tool works as it is: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required,
AWS_SESSION_TOKEN is optional (temporary credentials). This is the same convention the kubernetes
plugin's `eks` method reads, so one Secret serves both. The method has no input and writes no
region: the type materializes secrets only, and every aws call names its own `--region`.

On a bad reference it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and exits 1.
It never prints a key, and no message it writes contains one.
"""

import sys

from _common import read_credentials, run


def build(env):
    return read_credentials(env.get("RW_AUTH_REF_DIR"))


def main(env=None):
    return run("auth/access_key.py", build, env)


if __name__ == "__main__":
    sys.exit(main())
