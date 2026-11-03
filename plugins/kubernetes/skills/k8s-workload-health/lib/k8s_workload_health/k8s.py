"""kubectl access for this skill's scripts."""

import json
import re

try:  # the SDK's error type, once it ships one
    from runwhen_capability import RwError
except ImportError:  # pre-M1 SDKs: same shape, `RwError(code, message)`

    class RwError(Exception):
        """A failure with a D64 error code (permission_denied, not_found, ...)."""

        def __init__(self, code, message=""):
            super().__init__(f"{code}: {message}")
            self.code = code
            self.message = message


# (code, pattern) in priority order; first match wins.
_RULES = (
    ("dependency", r"the server doesn't have a resource type|command not found"),
    ("permission_denied", r"forbidden|cannot (get|list|watch)"),
    ("not_found", r"notfound|not found"),
    ("rate_limited", r"\b429\b|too many requests|toomanyrequests"),
    ("timeout", r"deadline exceeded"),
    ("unreachable", r"unable to connect|i/o timeout|connection refused|no such host|x509|tls"),
)


def classify(returncode, stderr):
    """Map a failed kubectl call to a D64 error code."""
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    text = (stderr or "").lower()
    for code, pattern in _RULES:
        if re.search(pattern, text):
            return code
    return "internal"


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A non-zero exit raises RwError with a D64 code taken from stderr. (When the
    SDK's `ctx.run` raises on failure itself, the returncode check never fires.)
    """
    result = ctx.run(["kubectl", *args, "-o", "json"])
    returncode = getattr(result, "returncode", 0)
    if returncode:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RwError(
            classify(returncode, stderr),
            f"kubectl {' '.join(args)}: {stderr[-500:] or f'exit {returncode}'}",
        )
    try:
        return json.loads(result.stdout)
    except ValueError as e:
        raise RwError("internal", f"kubectl {' '.join(args)}: output is not JSON ({e})") from None
