"""kubectl access for this skill's scripts."""

import json
import re

from k8s_litellm_health.common import MESSAGE_LIMIT
from k8s_litellm_health.errors import RwError

# kubectl prints an API error as `Error from server (<Reason>): <message>`; the Reason is the
# API server's own classification and is read first. The message text is only a fallback for the
# reasons that do not decide (InternalError, BadRequest) and for errors with no such line
# (`Unable to connect to the server`).
_API_ERROR = re.compile(r"^Error from server \((\w+)\):?(.*)$", re.MULTILINE)
_REASONS = {
    "Forbidden": "permission_denied",
    "Unauthorized": "permission_denied",
    "NotFound": "not_found",
    "Timeout": "timeout",
    "ServerTimeout": "timeout",
    "GatewayTimeout": "timeout",
    "TooManyRequests": "rate_limited",
    "ServiceUnavailable": "unreachable",
}
# (code, substrings of lower-cased stderr); first match wins.
_CLASSES = (
    (
        "permission_denied",
        ("forbidden", "cannot get", "cannot list", "cannot watch", "cannot create"),
    ),
    ("dependency", ("the server doesn't have a resource type", "executable file not found")),
    ("not_found", ("notfound", "not found")),
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


def api_error(stderr):
    """(Reason, lower-cased message) of the first `Error from server (<Reason>): <message>` line,
    or (None, None)."""
    match = _API_ERROR.search(stderr or "")
    return (match.group(1), match.group(2).strip().lower()) if match else (None, None)


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call: the exit code, then the `Error from server`
    Reason, then (only when those do not decide) the message text."""
    text = (stderr or "").lower()
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    reason, _ = api_error(stderr)
    if reason == "NotFound" and "doesn't have a resource type" in text:
        return "dependency"
    if reason in _REASONS:
        return _REASONS[reason]
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            return code
    return "internal"


def failure(argv, result):
    """The RwError for a failed command result, with a D64 code from its stderr."""
    stderr = (result.stderr or "").strip()
    # kubectl prints "Warning: ..." lines before the error; the error is what the message is for
    lines = [x for x in stderr.splitlines() if not x.startswith("Warning:")]
    shown = "\n".join(lines).strip() or stderr
    return RwError(
        classify(result.returncode, shown),
        f"{argv[0]} exited {result.returncode}: {shown}",
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


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A failed call (forbidden, not found, unreachable) raises RwError and never turns into data
    here; output that is not JSON is an `internal` error.
    """
    result = run_checked(ctx, ["kubectl", *args, "-o", "json"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def kubectl_json_scoped(ctx, scope, *args, hint=None):
    """A read the rest of the result can do without: the document, or None when it is forbidden
    (reported with `ctx.error` as permission_denied for `scope`). Every other failure (not
    found, unreachable, timeout, bad output) still raises."""
    try:
        return kubectl_json(ctx, *args)
    except RwError as exc:
        if exc.code != "permission_denied":
            raise
        ctx.error(
            "permission_denied",
            f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
            scope=scope,
            **({"hint": hint} if hint else {}),
        )
        return None
