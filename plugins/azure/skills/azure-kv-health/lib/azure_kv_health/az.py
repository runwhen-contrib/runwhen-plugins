"""Running one `az` command that returns JSON, and classifying its failure.

This file is shared by every azure skill. Skills do not import each other (a task sees only its
own skill's lib/), so each skill carries a verbatim copy of `errors.py` and `az.py` in its own
`lib/<skill_snake>/`; they import only `.errors`, so the copy needs no edit. A test
(tests/unit/test_azure_copies.py) fails when a copy drifts from `azure_base`'s.

    from azure_base.az import az_json           # in another skill: from <skill_snake>.az import ...

    account = az_json(ctx, ["account", "show", "--subscription", subscription_id])
    vaults = az_json(ctx, ["keyvault", "list", "--subscription", subscription_id])

The session is az's own state: AZURE_CONFIG_DIR, which the connection's `azure/cli` credential
materializes (an auth script logged in there). This module never reads or prints it, and a secret
never goes on argv: a command that needs one reads it from a file (`-p @file`).

The exit code decides, then stderr. az prints warnings to stderr on commands that succeed (an
upgrade notice, a deprecation, `az login -p`), so an exit code of 0 is a success whatever stderr
holds, and stderr is only read to classify a non-zero exit. On a failure az prints `ERROR: ...`
lines; an unexpected error also prints a traceback after them, so the classifier reads the
`ERROR:` lines only (the traceback names exception classes that would match anything).
"""

import json
import re
import subprocess

from .errors import RwError

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability.errors import OutputTooLargeError
except ImportError:  # pragma: no cover

    class OutputTooLargeError(ValueError):
        pass


DEFAULT_COMMAND_TIMEOUT = "60s"
MESSAGE_LIMIT = 1024

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value, name="duration"):
    """Seconds from a duration such as `500ms`, `30s`, `1m30s`; a bad value raises invalid_input."""
    text = str(value).strip().lower()
    parts = _DURATION.findall(text)
    if not text or "".join(n + u for n, u in parts) != text:
        raise RwError(
            "invalid_input", f"{name} must be a duration such as 30s or 1h, got {value!r}"
        )
    seconds = sum(float(n) * _UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise RwError("invalid_input", f"{name} must be greater than zero, got {value!r}")
    return seconds


def command_timeout(settings):
    """Seconds one az command may take, from the task's `commandTimeout` setting."""
    return parse_duration(
        getattr(settings, "command_timeout", DEFAULT_COMMAND_TIMEOUT), "commandTimeout"
    )


# stderr patterns (of the ERROR: lines), tried in this order.
#   dependency first: a provider that is not registered, or a command az does not know, is a
#     property of the subscription or the runtime, whatever else the text says.
#   rate_limited before permission_denied: a throttled call says nothing about permissions.
#   a firewall or private-link refusal is worded "(Forbidden) Client address is not authorized":
#     it is the network path, not the role assignment, so it precedes permission_denied.
#   the connection failures precede timeout: "Failed to establish a new connection: [Errno 110]
#     Connection timed out" is unreachable, a bare "Read timed out" is a timeout.
_RULES = (
    (
        "dependency",
        re.compile(
            r"MissingSubscriptionRegistration|not registered to use namespace"
            r"|is misspelled or not recognized|extension .* (?:not|isn't) installed|No module named",
            re.I,
        ),
    ),
    (
        "rate_limited",
        re.compile(
            r"TooManyRequests|too many requests|\b429\b|throttl|RetryAfter"
            r"|(?:Read|Write)RequestsLimitExceeded|rate limit",
            re.I,
        ),
    ),
    (
        "unreachable",
        re.compile(
            r"ForbiddenByFirewall|ForbiddenByConnection|Client address is not authorized"
            r"|not a trusted service|private link|public network access",
            re.I,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"AuthorizationFailed|AuthorizationPermissionMismatch|Forbidden|not authorized"
            r"|does not have authorization|AccessDenied|Unauthorized|AADSTS|InsufficientPermissions"
            r"|PermissionDenied|Please run 'az login'|Interactive authentication is needed"
            r"|\b40[13]\b|SubscriptionNotRegistered|was not found in the directory",
            re.I,
        ),
    ),
    (
        "not_found",
        re.compile(
            r"not found|NotFound|could not be found|does not exist|doesn't exist|\b404\b",
            re.I,
        ),
    ),
    (
        "unreachable",
        re.compile(
            r"Max retries exceeded|Failed to establish a new connection|Name or service not known"
            r"|Temporary failure in name resolution|getaddrinfo|Connection (?:refused|reset|aborted)"
            r"|ConnectionError|Remote end closed|ProxyError|SSLError|CERTIFICATE_VERIFY_FAILED"
            r"|network is unreachable|no route to host",
            re.I,
        ),
    ),
    ("timeout", re.compile(r"timed out|timeout|deadline exceeded", re.I)),
    (
        "invalid_input",
        re.compile(
            r"InvalidSubscriptionId|InvalidResourceName|InvalidResourceType|InvalidParameter"
            r"|InvalidQueryParameterValue|InvalidRequestContent",
            re.I,
        ),
    ),
)


def headline(stderr):
    """The `ERROR:` lines of az's stderr (az prints a traceback after them on an unexpected
    error); the whole text when there are none."""
    text = str(stderr or "")
    lines = [ln for ln in text.splitlines() if ln.startswith("ERROR:")]
    return "\n".join(lines) if lines else text


def classify_error(stderr, returncode=1):
    """D64 error code for a failed az command, from its stderr and exit code."""
    if returncode == 127:
        return "dependency"
    text = headline(stderr)
    for code, pattern in _RULES:
        if pattern.search(text):
            return code
    if returncode == 124:
        return "timeout"
    return "internal"


def _clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 3] + "..."


def az_json(ctx, argv):
    """Run `az <argv> --output json` and return the JSON it prints, parsed.

    No output at all (a command that prints nothing, such as an empty `az rest` body) is returned
    as None. A non-zero exit raises RwError with the D64 code classified from the ERROR lines of
    stderr: permission_denied (AuthorizationFailed, Forbidden, AADSTS, no `az login`),
    rate_limited (TooManyRequests, 429, throttled), not_found (not found, ResourceNotFound,
    SubscriptionNotFound), unreachable (cannot connect, DNS, TLS, or a Key Vault firewall or
    private link refusal), timeout (the command timeout, or a read timeout), dependency (no az in
    the runtime, a resource provider that is not registered, a command az does not know),
    invalid_input (an id az rejects) or internal. An exit code of 0 is a success whatever az wrote
    to stderr. The command's timeout is the task's `commandTimeout` setting.
    """
    seconds = command_timeout(ctx.settings)
    try:
        result = ctx.run(["az", *argv, "--output", "json"], timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        raise RwError(
            "timeout", f"az {' '.join(argv[:2])} did not finish within {seconds:.0f}s"
        ) from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input", "the az output is too large to return; narrow the command"
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "az is not installed in this runtime") from None
    if result.returncode != 0:
        stderr = result.stderr or ""
        raise RwError(
            classify_error(stderr, result.returncode),
            _clip(headline(stderr).strip()) or f"az exited {result.returncode}",
        )
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise RwError("internal", "az did not return JSON") from None
