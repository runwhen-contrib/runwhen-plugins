"""kubectl access for this skill's scripts."""

import json
import subprocess

from k8s_istio_health.common import MESSAGE_LIMIT
from k8s_istio_health.errors import RwError

# (code, substrings of lower-cased stderr); first match wins.
_CLASSES = (
    (
        "permission_denied",
        ("forbidden", "cannot get", "cannot list", "cannot watch", "cannot create"),
    ),
    ("dependency", ("the server doesn't have a resource type", "executable file not found")),
    ("not_found", ("notfound", "not found")),
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
    ("timeout", ("deadline exceeded",)),
    ("rate_limited", ("429", "too many requests")),
)


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call."""
    text = (stderr or "").lower()
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            return code
    return "internal"


def run_checked(ctx, argv, timeout=None):
    """`ctx.run(argv)`, raising RwError with a D64 code when the command failed or timed out.

    Under the real runner `ctx.run` raises RwCommandError (an RwError) itself and that is
    passed through unchanged; a `ctx.run` that returns a non-zero CompletedProcess is
    checked here.
    """
    try:
        result = ctx.run(argv, **({"timeout": timeout} if timeout else {}))
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"{' '.join(argv[:4])} timed out") from None
    if result.returncode:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify(result.returncode, stderr),
            f"{argv[0]} exited {result.returncode}: {stderr[:MESSAGE_LIMIT]}",
        )
    return result


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A failed call (forbidden, not found, unreachable) raises RwError and never turns into
    data here; output that is not JSON is an `internal` error.
    """
    result = run_checked(ctx, ["kubectl", *args, "-o", "json"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def kubectl_text(ctx, *args, timeout=None):
    """Run `kubectl <args>` and return stdout as text."""
    return run_checked(ctx, ["kubectl", *args], timeout=timeout).stdout


def kubectl_json_scoped(ctx, scope, *args, hint=None):
    """A read the rest of the result can do without: the document, or None when it is
    forbidden (reported with `ctx.error` as permission_denied for `scope`). Every other
    failure (not found, unreachable, timeout, bad output) still raises."""
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
