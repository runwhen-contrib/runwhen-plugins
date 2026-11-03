"""Running the validated kubectl command and classifying its failure."""

import re
import subprocess

from k8s_base.errors import RwError

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability.errors import OutputTooLargeError
except ImportError:  # pragma: no cover

    class OutputTooLargeError(ValueError):
        pass


RUN_TIMEOUT = 50  # seconds; the task's own timeout is 60s

# `Error from server (Reason): message`: the reason is the API server's verdict; the message
# repeats object names ("forbidden-app", "unable-to-connect") that must not be read as one.
_SERVER_ERROR = re.compile(r"^Error from server \((\w+)\)", re.MULTILINE)
_BY_REASON = {
    "Forbidden": "permission_denied",
    "Unauthorized": "permission_denied",
    "NotFound": "not_found",
    "TooManyRequests": "rate_limited",
    "Timeout": "timeout",
    "ServerTimeout": "timeout",
    "ServiceUnavailable": "unreachable",
    "BadRequest": "invalid_input",
    "Invalid": "invalid_input",
}
_UNREACHABLE = (
    "unable to connect",
    "i/o timeout",
    "connection refused",
    "no such host",
    "x509",
    "tls",
)


def classify_error(stderr, returncode=1):
    """D64 error code for a failed kubectl call, from its stderr and exit code.

    Order matters: the server's own `Error from server (Reason)` line first, then kubectl's
    client-side messages, and only then loose text; matching words anywhere in stderr would
    read an object named `forbidden-app` as a permission error."""
    text = str(stderr)
    t = text.lower()
    if returncode == 127:
        return "dependency"
    if returncode == 124:
        return "timeout"
    if "doesn't have a resource type" in t or "metrics api not available" in t:
        return "dependency"
    match = _SERVER_ERROR.search(text)
    if match:
        return _BY_REASON.get(match.group(1), "internal")
    if "you must be logged in" in t:
        return "permission_denied"
    if any(marker in t for marker in _UNREACHABLE):
        return "unreachable"
    if "deadline exceeded" in t:
        return "timeout"
    if "too many requests" in t:
        return "rate_limited"
    if re.search(r"^error: ", text, re.MULTILINE):
        return "invalid_input"
    return "internal"


def run_kubectl(ctx, argv):
    """Run `kubectl <argv>` (already validated) and return (stdout, stderr). A non-zero exit
    raises RwError with the D64 code. The real runner raises RwCommandError itself unless
    check=False; this asks for the process back so one classifier serves both."""
    try:
        result = ctx.run(["kubectl", *argv], timeout=RUN_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"kubectl {argv[0]} timed out after {RUN_TIMEOUT}s") from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input",
            "the output is too large to return; narrow the command "
            "(-n, -l, --field-selector, -o name, --tail)",
        ) from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify_error(stderr, result.returncode),
            stderr[:1024] or f"kubectl exited {result.returncode}",
        )
    return result.stdout or "", (result.stderr or "").strip()
