"""kubectl access for this skill's scripts, with failures classified to the D64 error codes."""

import json
import re
import subprocess

try:  # the SDK's own class once it is importable
    from runwhen_capability import RwError
except ImportError:

    class RwError(Exception):
        """A failure with a D64 error code (permission_denied, not_found, ...)."""

        def __init__(self, code, message):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message


# (code, pattern) in priority order, matched against kubectl's stderr.
_PATTERNS = [
    ("not_found", re.compile(r"NotFound|\bnot found\b", re.I)),
    (
        "permission_denied",
        re.compile(r"Forbidden|\bcannot (get|list|watch|create)\b|Unauthorized", re.I),
    ),
    (
        "dependency",
        re.compile(r"the server doesn't have a resource type|no matches for kind", re.I),
    ),
    ("timeout", re.compile(r"context deadline exceeded|timed out|Client\.Timeout", re.I)),
    ("rate_limited", re.compile(r"\b429\b|Too Many Requests", re.I)),
    (
        "unreachable",
        re.compile(
            r"Unable to connect|i/o timeout|connection refused|no such host|x509|\btls\b|"
            r"connection reset|dial tcp|\\bEOF\\b",
            re.I,
        ),
    ),
]


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call."""
    text = stderr or ""
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    for code, pattern in _PATTERNS:
        if pattern.search(text):
            return code
    return "internal"


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    `ctx.run` hands back the finished process without raising on a non-zero
    exit, so the exit status is checked here: a failed call (forbidden, not
    found, unreachable) raises `RwError` with its error code and never turns
    into data.
    """
    try:
        result = ctx.run(["kubectl", *args, "-o", "json"])
    except subprocess.TimeoutExpired as exc:
        raise RwError("timeout", f"kubectl {' '.join(args)} timed out") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        raise RwError(
            classify(exc.returncode, stderr),
            stderr.strip()[:1024] or f"kubectl exited {exc.returncode}",
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify(result.returncode, stderr),
            stderr[:1024] or f"kubectl exited {result.returncode}",
        )
    return json.loads(result.stdout)


def secondary(ctx, scope, *args, code_raises=()):
    """A fetch whose failure leaves the primary facts useful: on failure, record
    the error on the result (`ctx.error`, making it partial) and return None.
    Failures with a code in `code_raises` are not secondary and still raise."""
    try:
        return kubectl_json(ctx, *args)
    except RwError as exc:
        if exc.code in code_raises:
            raise
        ctx.error(exc.code, exc.message, scope=scope)
        return None
