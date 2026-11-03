"""Running the validated kubectl command and classifying its failure."""

import subprocess

from k8s_base.errors import RwError


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


def run_kubectl(ctx, argv):
    """Run `kubectl <argv>` (already validated) and return (stdout, stderr). A non-zero exit
    raises RwError with the D64 code. The real runner raises RwCommandError itself unless
    check=False; this asks for the process back so one classifier serves both."""
    try:
        result = ctx.run(["kubectl", *argv], check=False)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"kubectl {argv[0]} timed out") from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify_error(stderr, result.returncode),
            stderr[:1024] or f"kubectl exited {result.returncode}",
        )
    return result.stdout or "", (result.stderr or "").strip()
