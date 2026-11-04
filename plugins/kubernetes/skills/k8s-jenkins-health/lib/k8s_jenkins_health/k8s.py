"""kubectl access for this skill's scripts."""

import re

from k8s_jenkins_health.common import BODY_LIMIT
from k8s_jenkins_health.errors import RwError

# kubectl prints an API error as `Error from server (<Reason>): <message>`. The reason is the
# apiserver's own and is what a failure is classified by; for a Forbidden or an Unauthorized the
# message is the backend's body, which is never used for the code (only shown, as a hint).
_REASON_LINE = re.compile(r"^Error from server \((\w+)\): ?(.*)$")
_REASONS = {
    "Forbidden": "permission_denied",
    "Unauthorized": "permission_denied",
    "NotFound": "not_found",
    "Timeout": "timeout",
    "ServerTimeout": "timeout",
    "TooManyRequests": "rate_limited",
    "ServiceUnavailable": "unreachable",
    "InternalError": "internal",
}
# kubectl's own lines (no reason): (code, substrings of the lower-cased line); first match wins.
_CLIENT_CLASSES = (
    ("dependency", ("the server doesn't have a resource type", "executable file not found")),
    ("permission_denied", ("you must be logged in to the server",)),
    ("timeout", ("deadline exceeded", "client.timeout")),
    (
        "unreachable",
        (
            "unable to connect",
            "i/o timeout",
            "connection refused",
            "no such host",
            "x509",
            "tls:",
            "tls handshake",
        ),
    ),
    ("timeout", ("timed out",)),
    ("rate_limited", ("429", "too many requests")),
)


def error_line(stderr):
    """The line of kubectl's stderr that says what failed: the first `Error from server`, `error:`
    or `Unable to connect` line, else the first line that is not a `Warning:`. Lines after it
    (a backend's multi-line body) are not read."""
    lines = [x.strip() for x in (stderr or "").splitlines()]
    lines = [x for x in lines if x and not x.startswith("Warning:")]
    for line in lines:
        if line.startswith(("Error from server", "error:", "Error:", "Unable to connect")):
            return line
    return lines[0] if lines else ""


def split_reason(stderr):
    """(Reason, message) of an `Error from server (<Reason>): <message>` line, else (None, line)."""
    line = error_line(stderr)
    found = _REASON_LINE.match(line)
    return (found.group(1), found.group(2)) if found else (None, line)


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call: from the apiserver's `(<Reason>)` of the error
    line; for kubectl's own lines (no reason) from fixed wording of that line. A backend's body
    text decides nothing."""
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    reason, message = split_reason(stderr)
    if reason is not None:
        return _REASONS.get(reason, "internal")
    text = message.lower()
    for code, needles in _CLIENT_CLASSES:
        if any(n in text for n in needles):
            return code
    return "internal"


def failure(argv, result):
    """The RwError for a failed command result, with a D64 code from its stderr. The message holds
    the error line only: a backend's body is cut to its first line and BODY_LIMIT characters."""
    stderr = (result.stderr or "").strip()
    shown = error_line(stderr) or (stderr.splitlines() or [""])[0]
    return RwError(
        classify(result.returncode, stderr),
        f"{argv[0]} exited {result.returncode}: {shown[:BODY_LIMIT]}",
    )


def run_unchecked(ctx, argv, **run_kwargs):
    """`ctx.run(argv)` that returns the finished process whatever its exit code.

    The v13 runner raises on a non-zero exit by default; `check=False` keeps the return-code
    path, which this skill classifies itself.
    """
    return ctx.run(argv, check=False, **run_kwargs)


def run_checked(ctx, argv, **run_kwargs):
    """`run_unchecked`, raising RwError with a D64 code when the command failed."""
    result = run_unchecked(ctx, argv, **run_kwargs)
    if result.returncode:
        raise failure(argv, result)
    return result
