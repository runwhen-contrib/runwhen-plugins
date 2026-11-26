"""Auth script for the `token` method of the git integration (D67, CONTRACTS F7).

Runs in the executor before the task script. It turns the credential reference (a Secret, Vault
path or file holding the token) and the connection's input into the `git/https-token` environment,
by writing `NAME=value` lines to $RW_AUTH_OUT_ENV:

    GIT_USERNAME GIT_TOKEN

Reference: the token is the key `token`, `GIT_TOKEN` or `value` of $RW_AUTH_REF_DIR; exactly one of
them must exist. Input: $RW_AUTH_INPUT_USERNAME (optional; `x-access-token` when empty).
On a bad reference or input it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and
exits 1. It never prints the token, and no message it writes contains it.
"""

import json
import os
import sys

TOKEN_KEYS = ("token", "GIT_TOKEN", "value")
DEFAULT_USERNAME = "x-access-token"


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _single_line(name, value):
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", f"{name} must be a single line without NUL")
    return value


def read_token(ref_dir):
    """The token from the reference directory; exactly one of the accepted keys must exist."""
    if not ref_dir or not os.path.isdir(ref_dir):
        raise AuthError("invalid_input", "the credential reference was not materialized")
    present = [k for k in TOKEN_KEYS if os.path.exists(os.path.join(ref_dir, k))]
    if len(present) != 1:
        raise AuthError(
            "invalid_input",
            "the credential reference must hold exactly one of the keys "
            + ", ".join(TOKEN_KEYS)
            + (f"; it holds {', '.join(present)}" if present else "; it holds none of them"),
        )
    try:
        with open(os.path.join(ref_dir, present[0]), encoding="utf-8", newline="") as handle:
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
    return _single_line("the token", text)


def build(env):
    """The `git/https-token` environment as an ordered list of (NAME, value)."""
    username = env.get("RW_AUTH_INPUT_USERNAME") or DEFAULT_USERNAME
    username = _single_line("input username", username)
    return [("GIT_USERNAME", username), ("GIT_TOKEN", read_token(env.get("RW_AUTH_REF_DIR")))]


def main(env=None):
    env = os.environ if env is None else env
    try:
        pairs = build(env)
        out = env.get("RW_AUTH_OUT_ENV")
        if not out:
            raise AuthError("internal", "RW_AUTH_OUT_ENV is not set")
        with open(out, "w", encoding="utf-8") as handle:
            handle.writelines(f"{name}={value}\n" for name, value in pairs)
    except AuthError as exc:
        target = env.get("RW_AUTH_OUT_ERROR")
        if target:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"code": exc.code, "message": exc.message}) + "\n")
        print(f"auth/https-token.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
