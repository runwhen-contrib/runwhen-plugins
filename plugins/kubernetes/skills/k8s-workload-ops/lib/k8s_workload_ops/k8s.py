"""kubectl access for this skill's scripts."""

import json

from k8s_workload_ops.errors import RwError

MESSAGE_LIMIT = 1024

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


def run_checked(ctx, argv, **run_kwargs):
    """`ctx.run(argv)`, raising RwError with a D64 code when the command failed.

    The installed SDK's `ctx.run` returns the CompletedProcess without
    raising, so the return code is checked here; an SDK that raises instead
    has its exception passed through unchanged.
    """
    result = ctx.run(argv, **run_kwargs)
    if result.returncode:
        stderr = (result.stderr or "").strip()
        # kubectl prints "Warning: ..." lines before the error; the error is what the message is for
        lines = [x for x in stderr.splitlines() if not x.startswith("Warning:")]
        shown = "\n".join(lines).strip() or stderr
        raise RwError(
            classify(result.returncode, stderr),
            f"{argv[0]} exited {result.returncode}: {shown[:MESSAGE_LIMIT]}",
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


def error_code(exc):
    """The D64 code of a failure raised by run_checked, else None."""
    return getattr(exc, "code", None)


def kubectl_json_scoped(ctx, scope, *args, missing_ok=False):
    """A read of an object the rest of the result can do without: (document, state).

    state is 'ok', 'not_found' (only with `missing_ok`: the object does not
    exist, a fact for the caller) or 'denied' (forbidden: reported with
    `ctx.error` as permission_denied for `scope`, document None). Every other
    failure (unreachable, timeout, missing API, bad output) still raises.
    """
    try:
        return kubectl_json(ctx, *args), "ok"
    except RwError as exc:
        if exc.code == "not_found" and missing_ok:
            return None, "not_found"
        if exc.code == "permission_denied":
            ctx.error(
                "permission_denied",
                f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
                scope=scope,
                hint=f"grant get/list on {scope} through a Role in the target's namespace",
            )
            return None, "denied"
        raise


def kubectl_text(ctx, *args, **run_kwargs):
    """Run `kubectl <args>` and return stdout; a failed call raises RwError.

    For the mutating calls, whose output is a sentence rather than a document.
    """
    return run_checked(ctx, ["kubectl", *args], **run_kwargs).stdout or ""
