"""kubectl access for this skill's scripts."""

import json

from k8s_argocd_health.errors import RwError

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


def run_checked(ctx, argv):
    """Run argv, raising RwError with a D64 code when the command failed.

    Runs with check=False and classifies here, so the code is the same whether
    or not the runner would have raised on a non-zero exit.
    """
    result = ctx.run(argv, check=False)
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
