"""Running one psql query that returns one JSON value, and classifying its failure.

This file is shared by every postgres skill. Skills do not import each other (a task sees
only its own skill's lib/), so each skill carries a verbatim copy of `errors.py` and `pg.py`
in its own `lib/<skill_snake>/`; they import only `.errors`, so the copy needs no edit. A test
(tests/unit/test_pg_copies.py) fails when a copy drifts from `pg_base`'s.

    from pg_base.pg import psql_json            # in another skill: from <skill_snake>.pg import ...

    row = psql_json(ctx, "SELECT json_build_object('version', version())")
    dbs = psql_json(ctx, "SELECT coalesce(json_agg(datname), '[]') FROM pg_database",
                    database="orders")

The connection is libpq's own environment (PGHOST, PGPORT, PGUSER, PGDATABASE, PGSSLMODE...),
which the runner materializes from the connection's `postgres/libpq` credential; this module
never reads or prints PGPASSWORD. The session is read-only and bounded by the task's settings
`connectTimeout` and `statementTimeout` (durations, default 10s and 30s).
"""

import json
import re
import subprocess

from .errors import RwError

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability.errors import OutputTooLargeError
except ImportError:  # pragma: no cover

    class OutputTooLargeError(ValueError):
        pass


DEFAULT_CONNECT_TIMEOUT = "10s"
DEFAULT_STATEMENT_TIMEOUT = "30s"
MESSAGE_LIMIT = 1024
_SLACK = 5.0  # seconds a run may take beyond connect + statement timeouts

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value, name="duration"):
    """Seconds from a duration such as `500ms`, `30s`, `1m30s`; a bad value raises invalid_input."""
    text = str(value).strip().lower()
    parts = _DURATION.findall(text)
    if not text or "".join(n + u for n, u in parts) != text:
        raise RwError(
            "invalid_input", f"{name} must be a duration such as 10s or 1m, got {value!r}"
        )
    seconds = sum(float(n) * _UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise RwError("invalid_input", f"{name} must be greater than zero, got {value!r}")
    return seconds


def timeouts(settings):
    """(connect seconds, statement seconds) from the task's settings, validated."""
    connect = getattr(settings, "connect_timeout", DEFAULT_CONNECT_TIMEOUT)
    statement = getattr(settings, "statement_timeout", DEFAULT_STATEMENT_TIMEOUT)
    return parse_duration(connect, "connectTimeout"), parse_duration(statement, "statementTimeout")


# stderr patterns, tried in this order. Timeouts first: a connect timeout is worded
# "connection to server ... failed: timeout expired", which the unreachable pattern would also
# match. Authentication and authorization next, for the same reason ("... failed: FATAL: ...").
_RULES = (
    (
        "timeout",
        re.compile(
            r"timeout expired|canceling statement due to statement timeout"
            r"|canceling statement due to user request",
            re.I,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"password authentication failed|permission denied|no pg_hba\.conf entry"
            r"|role \"[^\"]*\" does not exist|authentication failed|no password supplied"
            r"|must be (?:superuser|owner|member)|insufficient privilege",
            re.I,
        ),
    ),
    ("not_found", re.compile(r"database \"[^\"]*\" does not exist", re.I)),
    (
        "unreachable",
        re.compile(
            r"could not connect|connection refused|could not translate host name"
            r"|name or service not known|no route to host|network is unreachable"
            r"|ssl error|ssl syscall|sslmode|server does not support ssl|certificate verify failed"
            r"|server closed the connection unexpectedly|connection to server .* failed"
            r"|the database system is (?:starting up|shutting down)",
            re.I,
        ),
    ),
)


def classify_error(stderr, returncode=1):
    """D64 error code for a failed psql call, from its stderr and exit code."""
    if returncode == 127:
        return "dependency"
    text = str(stderr)
    for code, pattern in _RULES:
        if pattern.search(text):
            return code
    if returncode == 124:
        return "timeout"
    return "internal"


def _clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 3] + "..."


_PLAIN_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$-]*$")


def dbname_arg(database):
    """The `-d` value for a database name. psql reads a value with `=` or a URL scheme as a
    connection string, so a name that is not plain is passed as `dbname='...'`."""
    if "\x00" in database or not database:
        raise RwError("invalid_input", "database must be a non-empty name without NUL")
    if _PLAIN_NAME.match(database):
        return database
    quoted = database.replace("\\", "\\\\").replace("'", "\\'")
    return f"dbname='{quoted}'"


def dollar_quote(text):
    """`text` as a dollar-quoted SQL string literal, whatever it contains (no escaping rules,
    independent of standard_conforming_strings)."""
    text = str(text)
    if "\x00" in text:
        raise RwError("invalid_input", "a value sent to the database cannot contain NUL")
    n = 0
    while True:
        tag = f"$q{n}$"
        if (text + tag).find(tag) == len(text):  # the first occurrence is the closing tag
            return f"{tag}{text}{tag}"
        n += 1


def psql_json(ctx, sql, *, database=None):
    """Run `sql` (one statement that returns one JSON value) and return it parsed.

    The statement must produce a single value, for example `SELECT json_agg(...)` or
    `SELECT row_to_json(t) FROM ...`; wrap a possible NULL in `coalesce(..., '[]')`. An empty
    result (the statement returned NULL or no row) is returned as None.

    `database` connects to that database instead of PGDATABASE. A non-zero psql exit raises
    RwError with the D64 code classified from stderr: permission_denied (authentication, no
    pg_hba.conf entry, missing role, permission denied), not_found (database does not exist),
    unreachable (cannot connect, DNS, SSL), timeout (connect or statement timeout), dependency
    (no psql in the runtime) or internal. The session is read-only and statement_timeout is set
    from the task's `statementTimeout` setting."""
    connect, statement = timeouts(ctx.settings)
    session = (
        f"SET statement_timeout = {int(statement * 1000)}; SET default_transaction_read_only = on"
    )
    argv = ["psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1"]
    if database is not None:
        argv += ["-d", dbname_arg(database)]
    argv += ["-c", session, "-c", sql]
    try:
        result = ctx.run(
            argv,
            timeout=connect + statement + _SLACK,
            env={"PGCONNECT_TIMEOUT": str(max(1, round(connect)))},
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"psql did not finish within {connect + statement:.0f}s") from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input", "the query result is too large to return; narrow the query"
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "psql is not installed in this runtime") from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify_error(stderr, result.returncode),
            _clip(stderr) or f"psql exited {result.returncode}",
        )
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise RwError("internal", "psql did not return a JSON value") from None
