"""kubectl access for this skill's scripts."""

import json
import subprocess

from k8s_workload_logs.errors import RwError

# kubectl logs stops after this many bytes per stream (callers flag the stream as cut).
LIMIT_BYTES = 4 * 1024 * 1024


def classify_error(stderr, returncode=1):
    """D64 error code for a failed kubectl call, from its stderr and exit code."""
    t = str(stderr).lower()
    if returncode == 127 or "doesn't have a resource type" in t:
        return "dependency"
    if "forbidden" in t or any(
        f"cannot {verb}" in t for verb in ("get", "list", "watch", "create")
    ):
        return "permission_denied"
    if "notfound" in t or "not found" in t:
        return "not_found"
    if returncode == 124 or "deadline exceeded" in t:
        return "timeout"
    if "429" in t or "too many requests" in t:
        return "rate_limited"
    for marker in (
        "unable to connect",
        "i/o timeout",
        "connection refused",
        "no such host",
        "x509",
        "tls",
    ):
        if marker in t:
            return "unreachable"
    return "internal"


def run_kubectl(ctx, *args):
    """Run `kubectl <args>`; return stdout. A non-zero exit or a timeout raises
    RwError with the D64 code (the SDK's ctx.run returns the failed process)."""
    try:
        result = ctx.run(["kubectl", *args])
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"kubectl {args[0] if args else ''} timed out") from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify_error(stderr, result.returncode),
            stderr[:1024] or f"kubectl exited {result.returncode}",
        )
    return result.stdout


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document."""
    return json.loads(run_kubectl(ctx, *args, "-o", "json"))


def kubectl_text(ctx, *args):
    """Run `kubectl <args>` and return stdout as text."""
    return run_kubectl(ctx, *args)
