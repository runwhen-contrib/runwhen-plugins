"""kubectl access for this skill's scripts, with failures classified to the D64 error codes."""

import json
import re
import subprocess

try:  # the SDK's own class once it is importable
    from runwhen_capability import RwError
except ImportError:

    class RwError(Exception):
        """A failure with a D64 error code (permission_denied, not_found, ...)."""

        def __init__(self, code, message, scope=None, hint=None, details=None):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message
            self.scope = scope
            self.hint = hint
            self.details = details


MESSAGE_LIMIT = 1024

# (code, pattern) in priority order, matched against kubectl's stderr.
_PATTERNS = [
    (
        "permission_denied",
        re.compile(r"Forbidden|\bcannot (get|list|watch|create)\b|Unauthorized", re.I),
    ),
    (
        "dependency",
        re.compile(
            r"the server doesn't have a resource type|no matches for kind|"
            r"executable file not found",
            re.I,
        ),
    ),
    ("not_found", re.compile(r"NotFound|\bnot found\b", re.I)),
    ("timeout", re.compile(r"context deadline exceeded|timed out|Client\.Timeout", re.I)),
    ("rate_limited", re.compile(r"\b429\b|Too Many Requests", re.I)),
    (
        "unreachable",
        re.compile(
            r"Unable to connect|i/o timeout|connection refused|no such host|x509|\btls\b|"
            r"connection reset|dial tcp|ServiceUnavailable|unable to handle the request",
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


def kubectl_json(ctx, *args, **run_kwargs):
    """Run `kubectl <args> -o json` and return the parsed document.

    Under the runner `ctx.run` raises `RwCommandError` (an `RwError`) on a
    non-zero exit and that propagates; with a `ctx.run` that returns the
    finished process instead, the exit status is checked here. Either way a
    failed call (forbidden, not found, unreachable) raises `RwError` with its
    code and never turns into data.
    """
    try:
        result = ctx.run(["kubectl", *args, "-o", "json"], **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RwError("timeout", f"kubectl {' '.join(args)} timed out"[:MESSAGE_LIMIT]) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify(result.returncode, stderr),
            (stderr or f"kubectl exited {result.returncode}")[:MESSAGE_LIMIT],
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def stderr_of(exc):
    """What kubectl printed, for an error raised by `kubectl_json` or by the runner's ctx.run."""
    return (getattr(exc, "stderr", None) or getattr(exc, "message", None) or str(exc)).lower()
