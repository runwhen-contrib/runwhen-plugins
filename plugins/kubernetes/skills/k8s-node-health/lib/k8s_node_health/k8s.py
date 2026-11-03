"""kubectl access for this skill's scripts."""

import json

from k8s_node_health.errors import RwError

MESSAGE_LIMIT = 1024

# (code, substrings of lower-cased stderr); first match wins.
_CLASSES = (
    (
        "permission_denied",
        ("forbidden", "cannot get", "cannot list", "cannot watch", "cannot create"),
    ),
    (
        "dependency",
        (
            "the server doesn't have a resource type",
            "executable file not found",
            "unable to handle the request",
            "serviceunavailable",
        ),
    ),
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


def run_checked(ctx, argv):
    """`ctx.run(argv)`, raising RwError with a D64 code when the command failed.

    The installed SDK's `ctx.run` returns the CompletedProcess without
    raising, so the return code is checked here; an SDK that raises instead
    has its exception passed through unchanged.
    """
    result = ctx.run(argv)
    if result.returncode:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify(result.returncode, stderr),
            f"{argv[0]} exited {result.returncode}: {stderr[:MESSAGE_LIMIT]}",
        )
    return result


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A failed call (forbidden, not found, unreachable) raises RwError and never
    turns into data here; output that is not JSON is an `internal` error.
    """
    result = run_checked(ctx, ["kubectl", *args, "-o", "json"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def kubectl_json_scoped(ctx, scope, hint, *args):
    """A read the rest of the result can do without: (document, ok).

    Forbidden is reported with `ctx.error` as permission_denied for `scope`
    and returns (None, False); every other failure (unreachable, timeout,
    missing API, bad output) still raises.
    """
    try:
        return kubectl_json(ctx, *args), True
    except RwError as exc:
        if exc.code != "permission_denied":
            raise
        ctx.error(
            "permission_denied",
            f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
            scope=scope,
            hint=hint,
        )
        return None, False
