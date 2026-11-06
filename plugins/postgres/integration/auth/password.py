"""Auth script for the `password` method of the postgres integration (D67, CONTRACTS F7).

Runs in the executor before the task script. It turns the credential reference (a Secret, Vault
path or file holding the role's password) and the connection's inputs into the libpq environment
of the `postgres/libpq` type, by writing `NAME=value` lines to $RW_AUTH_OUT_ENV:

    PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE

Reference: the password is the key `password`, `PGPASSWORD` or `value` of $RW_AUTH_REF_DIR;
exactly one of them must exist. Inputs: $RW_AUTH_INPUT_<NAME>, the upper-cased input name.
On a bad reference or input it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and
exits 1. It never prints the password, and no message it writes contains it.

A custom CA bundle (PGSSLROOTCERT, a file target) comes with a later version of the integration.
"""

import json
import os
import sys

PASSWORD_KEYS = ("password", "PGPASSWORD", "value")
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_PORT = "5432"
DEFAULT_DATABASE = "postgres"
DEFAULT_SSLMODE = "require"


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _single_line(name, value):
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", f"{name} must be a single line without NUL")
    return value


def read_password(ref_dir):
    """The password from the reference directory; exactly one of the accepted keys must exist."""
    if not ref_dir or not os.path.isdir(ref_dir):
        raise AuthError("invalid_input", "the credential reference was not materialized")
    present = [k for k in PASSWORD_KEYS if os.path.exists(os.path.join(ref_dir, k))]
    if len(present) != 1:
        raise AuthError(
            "invalid_input",
            "the credential reference must hold exactly one of the keys "
            + ", ".join(PASSWORD_KEYS)
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
    return _single_line("the password", text)


def read_inputs(env):
    """(host, port, user, database, sslmode) from RW_AUTH_INPUT_*, validated."""

    def get(name, default=None):
        value = env.get(f"RW_AUTH_INPUT_{name}")
        if value is None or value == "":
            if default is None:
                raise AuthError("invalid_input", f"input {name.lower()} is required")
            return default
        return _single_line(f"input {name.lower()}", value)

    host, user = get("HOST"), get("USER")
    port = get("PORT", DEFAULT_PORT)
    if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise AuthError("invalid_input", "input port must be an integer from 1 to 65535")
    sslmode = get("SSLMODE", DEFAULT_SSLMODE)
    if sslmode not in SSLMODES:
        raise AuthError("invalid_input", f"input sslmode must be one of {', '.join(SSLMODES)}")
    return host, str(int(port)), user, get("DATABASE", DEFAULT_DATABASE), sslmode


def build(env):
    """The libpq environment as an ordered list of (NAME, value)."""
    host, port, user, database, sslmode = read_inputs(env)
    password = read_password(env.get("RW_AUTH_REF_DIR"))
    return [
        ("PGHOST", host),
        ("PGPORT", port),
        ("PGUSER", user),
        ("PGPASSWORD", password),
        ("PGDATABASE", database),
        ("PGSSLMODE", sslmode),
    ]


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
        print(f"auth/password.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
