"""Running one psql query that returns one JSON value, and classifying its failure.

This file is shared by every postgres skill. Skills do not import each other (a task sees
only its own skill's lib/), so each skill carries a verbatim copy of `errors.py` and `pg.py`
in its own `lib/<skill_snake>/`; they import only `.errors`, so the copy needs no edit. A test
(the plugin unit test test_pg_copies.py) fails when a copy drifts from `pg_base`'s.

    from pg_base.pg import psql_json            # in another skill: from <skill_snake>.pg import ...

    row = psql_json(ctx, "SELECT json_build_object('version', version())")
    dbs = psql_json(ctx, "SELECT coalesce(json_agg(datname), '[]') FROM pg_database",
                    database="orders")

The connection is libpq's own environment (PGHOST, PGPORT, PGUSER, PGDATABASE, PGSSLMODE...),
which the runner materializes from the connection's `postgres/libpq` credential; this module
never reads or prints PGPASSWORD. The statement runs in one `BEGIN READ ONLY` transaction with a
`SET LOCAL statement_timeout`, bounded by the task's settings `connectTimeout` and
`statementTimeout` (durations, default 10s and 30s). Nothing is set on the session itself, so a
pooler that shares server connections (pgbouncer in transaction mode) never carries a setting
over to another client, and the statement cannot write.
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
MAX_MS = 2147483647  # statement_timeout is an int4 of milliseconds; 0 would mean "no limit"

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


def statement_ms(seconds):
    """statement_timeout in milliseconds, clamped to 1..2147483647 (never 0, which disables it,
    and never above what the server's int4 accepts)."""
    return min(max(int(seconds * 1000), 1), MAX_MS)


def connect_seconds(seconds):
    """PGCONNECT_TIMEOUT in whole seconds, at least 1 (0 means wait forever) and at most
    2147483 (2147483647 ms)."""
    return min(max(round(seconds), 1), MAX_MS // 1000)


def session_sql(sql, statement):
    """The one script psql runs: a read-only transaction, the statement timeout local to it, then
    `sql`. `-A -t -q` print only the value the query returns."""
    return (
        f"BEGIN READ ONLY; SET LOCAL statement_timeout = {statement_ms(statement)}; {sql}; COMMIT;"
    )


# stderr patterns, tried in this order. Timeouts first: a connect timeout is worded
# "connection to server ... failed: timeout expired", which the unreachable pattern would also
# match. Then rate limits and every permission_denied wording, all of which precede the
# "connection to server .* failed" catch-all of unreachable ("... failed: FATAL: ..." too).
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
        "rate_limited",
        re.compile(
            r"too many clients already|remaining connection slots are reserved"
            r"|too many connections for role|too many connections for database",
            re.I,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"password authentication failed|permission denied|no pg_hba\.conf entry"
            r"|pg_hba\.conf rejects connection|is not permitted to log in"
            r"|role \"[^\"]*\" does not exist|authentication failed|no password supplied"
            r"|must be (?:superuser|owner|member)|insufficient privilege",
            re.I,
        ),
    ),
    # pgbouncer words a missing database as "no such database"; postgres as "does not exist"
    ("not_found", re.compile(r"database \"[^\"]*\" does not exist|no such database", re.I)),
    # `unsupported startup parameter` is pgbouncer refusing a libpq option it does not pass on
    ("dependency", re.compile(r"unsupported startup parameter", re.I)),
    (
        "unreachable",
        re.compile(
            r"could not connect|connection refused|could not translate host name"
            r"|name or service not known|no route to host|network is unreachable"
            r"|ssl error|ssl syscall|sslmode|server does not support ssl|certificate verify failed"
            r"|server closed the connection unexpectedly|connection to server .* failed"
            r"|the database system is (?:starting up|shutting down|in recovery mode)"
            r"|conflict with recovery|terminating connection due to administrator command"
            r"|terminating connection due to conflict with recovery"
            r"|connection to server was lost|ssl connection has been closed unexpectedly"
            r"|pgbouncer cannot connect to server",
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
    pg_hba.conf entry or rejection, role not permitted to log in, missing role, permission
    denied), rate_limited (too many connections), not_found (database does not exist),
    unreachable (cannot connect, DNS, SSL, a connection cut by recovery or an administrator),
    timeout (connect or statement timeout), dependency (no psql in the runtime, or a pooler
    that refuses a startup parameter) or internal. `sql` runs in one `BEGIN READ ONLY`
    transaction whose `statement_timeout` comes from the task's `statementTimeout` setting;
    it must be the task's own fixed text, never user input: a statement that runs
    `SET TRANSACTION READ WRITE` would lift the read-only mode.
    """
    connect, statement = timeouts(ctx.settings)
    argv = ["psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1"]
    if database is not None:
        argv += ["-d", dbname_arg(database)]
    argv += ["-c", session_sql(sql, statement)]
    try:
        result = ctx.run(
            argv,
            timeout=connect + statement + _SLACK,
            env={"PGCONNECT_TIMEOUT": str(connect_seconds(connect))},
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
