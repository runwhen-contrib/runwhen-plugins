"""Running one gcloud command and classifying its failure.

This file is shared by every gcp skill. Skills do not import each other (a task sees only its own
skill's lib/), so each skill carries a verbatim copy of `errors.py` and `gcloud.py` in its own
`lib/<skill_snake>/`; they import only `.errors`, so a copy needs no edit. A test
(tests/unit/test_gcp_copies.py) fails when a copy drifts from `gcp_base`'s.

    from gcp_base.gcloud import gcloud_json, gcloud_text        # elsewhere: from <skill_snake>.gcloud

    buckets = gcloud_json(ctx, "storage", "buckets", "list", "--project=shop-prod-1234", "--raw")
    total = gcloud_text(ctx, "storage", "du", "--summarize", "gs://shop-assets")

Every command is `gcloud <args> [--format=json] --quiet`: `--quiet` so that gcloud never stops to
ask a question (it would offer to enable a disabled API, then wait for an answer that never
comes). Write flags as `--flag=value`, one token, so an argv is the same on every run (fixtures
replay by exact argv).

The credential is the `gcp/adc` file named by CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE, which
gcloud reads itself; this module reads only its `type`, to tell a service-account key from the
cloud-identity marker (integration/auth/cloud-identity.py), and never prints or returns it. For
the marker the variable is blanked for the child process, and gcloud then uses the metadata
server. A failure raises RwError with the D64 code classified from gcloud's stderr (see
`classify_error`); a secret that slipped into stderr (a key, a bearer token) is scrubbed first.

gcloud storage vs gsutil: this plugin uses `gcloud storage` everywhere. gsutil is the older,
separate tool, deprecated in favour of `gcloud storage`: it keeps its own credential store
(~/.boto), has no `--format=json`, and its output and error texts are not the gcloud ones, so one
classifier could not cover both. `gcloud storage` shares gcloud's credential, flags, JSON output
and error format, and is the faster of the two on large listings.
"""

import json
import os
import re
import subprocess

from .errors import RwError

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability.errors import OutputTooLargeError
except ImportError:  # pragma: no cover

    class OutputTooLargeError(ValueError):
        pass


CREDENTIAL_ENV = "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"
MARKER_TYPE = "runwhen_cloud_identity"  # integration/auth/cloud-identity.py writes it
DEFAULT_COMMAND_TIMEOUT = "60s"
MESSAGE_LIMIT = 1024
MAX_CREDENTIAL_BYTES = 64 * 1024

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value, name="duration"):
    """Seconds from a duration such as `500ms`, `30s`, `1m30s`; a bad value raises invalid_input."""
    text = str(value).strip().lower()
    parts = _DURATION.findall(text)
    if not text or "".join(n + u for n, u in parts) != text:
        raise RwError(
            "invalid_input", f"{name} must be a duration such as 30s or 5m, got {value!r}"
        )
    seconds = sum(float(n) * _UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise RwError("invalid_input", f"{name} must be greater than zero, got {value!r}")
    return seconds


def command_timeout(settings, name="command_timeout", default=DEFAULT_COMMAND_TIMEOUT):
    """Seconds one gcloud call may take: the setting `commandTimeout` (or another named duration
    setting), validated."""
    return parse_duration(getattr(settings, name, default), _camel(name))


def _camel(snake):
    head, *rest = snake.split("_")
    return head + "".join(p.title() for p in rest)


def credential_env(environ=None):
    """The environment overrides for a gcloud child: `{}` normally, and the credential variable
    blanked (gcloud reads a blank property as unset) when the credential file is the
    cloud-identity marker, so gcloud falls back to the metadata server."""
    path = (os.environ if environ is None else environ).get(CREDENTIAL_ENV)
    if not path:
        return {}
    try:
        with open(path, "rb") as handle:
            doc = json.loads(handle.read(MAX_CREDENTIAL_BYTES))
    except (OSError, ValueError):
        return {}  # gcloud reports an unreadable credential in its own words
    if isinstance(doc, dict) and doc.get("type") == MARKER_TYPE:
        return {CREDENTIAL_ENV: ""}
    return {}


# stderr patterns, tried in this order. Rate limits first (a 403 can say rateLimitExceeded), then
# timeouts and a disabled API (also a 403), then the permission wordings, which include
# authentication (D64 has no code of its own for it), then not-found, bad input and the network.
_RULES = (
    (
        "rate_limited",
        re.compile(
            r"HTTPError 429|RESOURCE_EXHAUSTED|Quota exceeded|rateLimitExceeded"
            r"|userRateLimitExceeded|Too Many Requests|rate of change requests per second",
            re.I,
        ),
    ),
    (
        "timeout",
        re.compile(
            r"DEADLINE_EXCEEDED|deadline exceeded|ReadTimeout|read operation timed out", re.I
        ),
    ),
    (
        "dependency",
        re.compile(
            r"SERVICE_DISABLED|has not been used in project|API \[[^\]]*\] not enabled"
            r"|is not enabled on project|API has not been enabled|accessNotConfigured",
            re.I,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"PERMISSION_DENIED|HTTPError 40[13]|UNAUTHENTICATED|Reauthentication required"
            r"|do not currently have an active account|problem refreshing your current auth tokens"
            r"|invalid_grant|does not have [\w.-]+ access|does not have permission"
            r"|caller does not have permission|Permission '[^']*' denied|Forbidden"
            r"|insufficient authentication scopes|Invalid Credentials|Could not load the default"
            r"|Anonymous caller|ACCESS_TOKEN_SCOPE_INSUFFICIENT|credential file",
            re.I,
        ),
    ),
    (
        "not_found",
        re.compile(
            r"HTTPError 404|NOT_FOUND|not found|does not exist|no such bucket"
            r"|matched no objects or files",
            re.I,
        ),
    ),
    (
        "invalid_input",
        re.compile(
            r"HTTPError 400|INVALID_ARGUMENT|invalid choice|unrecognized arguments"
            r"|invalid bucket name|Bucket names? must|argument [^:]*: Invalid",
            re.I,
        ),
    ),
    (
        "unreachable",
        re.compile(
            r"Failed to establish a new connection|Name or service not known"
            r"|Temporary failure in name resolution|Connection refused|Connection reset"
            r"|Connection aborted|Connection timed out|Network is unreachable|No route to host"
            r"|Max retries exceeded|Unable to reach|Unable to connect|ConnectionError|SSLError"
            r"|certificate verify failed|HTTPError 50[0234]|Service Unavailable|Bad Gateway"
            r"|Could not reach",
            re.I,
        ),
    ),
)

_SECRETS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
    ),
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"(?i)\b(bearer|token)[ =:]+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r'(?i)"(private_key|client_secret|refresh_token|access_token)"\s*:\s*"[^"]*"'),
)


def classify_error(stderr, returncode=1):
    """D64 error code for a failed gcloud call, from its stderr and exit code."""
    if returncode == 127:
        return "dependency"
    text = str(stderr)
    for code, pattern in _RULES:
        if pattern.search(text):
            return code
    if returncode == 124:
        return "timeout"
    return "internal"


def scrub(text):
    """`text` with anything shaped like a key, token or secret replaced."""
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    return text


def error_text(stderr):
    """gcloud's error as one short line: from the first `ERROR:` line on (the warnings and
    deprecation notices before it are noise), whitespace collapsed, scrubbed, at most 1024 chars."""
    text = scrub(str(stderr))
    start = text.find("ERROR:")
    text = " ".join((text[start:] if start >= 0 else text).split())
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 3] + "..."


def run_gcloud(ctx, args, *, json_output, timeout=None):
    """stdout of `gcloud <args> [--format=json] --quiet`; a failure raises RwError."""
    argv = ["gcloud", *args]
    if json_output:
        argv.append("--format=json")
    argv.append("--quiet")
    seconds = timeout if timeout is not None else parse_duration(DEFAULT_COMMAND_TIMEOUT)
    try:
        result = ctx.run(argv, timeout=seconds, env=credential_env(), check=False)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"gcloud did not finish within {seconds:.0f}s") from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input", "the gcloud result is too large to return; narrow the command"
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "gcloud is not installed in this runtime") from None
    if result.returncode != 0:
        stderr = result.stderr or ""
        raise RwError(
            classify_error(stderr, result.returncode),
            error_text(stderr) or f"gcloud exited {result.returncode}",
        )
    return result.stdout or ""


def gcloud_json(ctx, *args, timeout=None):
    """The JSON value `gcloud <args> --format=json` prints. Empty output is an error, never an
    empty result: a list verb prints `[]`, so nothing at all means the command did not do its
    job."""
    text = run_gcloud(ctx, args, json_output=True, timeout=timeout).strip()
    if not text:
        raise RwError("internal", "gcloud printed no JSON")
    try:
        return json.loads(text)
    except ValueError:
        raise RwError("internal", "gcloud did not return a JSON value") from None


def gcloud_text(ctx, *args, timeout=None):
    """The text a gcloud command without a JSON form prints (`gcloud storage du`)."""
    return run_gcloud(ctx, args, json_output=False, timeout=timeout)


_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


def valid_project(name):
    return isinstance(name, str) and bool(_PROJECT.fullmatch(name))


def valid_bucket(name):
    """A Cloud Storage bucket name (3 to 63 characters, or up to 222 with dots, of a-z 0-9 . _ -);
    anything else is never put in a `gs://` URL."""
    return isinstance(name, str) and bool(_BUCKET.fullmatch(name)) and ".." not in name


def target_path(resource):
    """(project, bucket or None) from the run's target: a `gcp/project` or a `gcp/bucket`, read
    from its path (`gcp/projects/<id>[/buckets/<name>]`). Anything else is `invalid_input`."""
    resource = resource or {}
    parts = str(resource.get("path", "")).split("/")
    kind = resource.get("type")
    if kind == "gcp/project" and len(parts) == 3 and parts[:2] == ["gcp", "projects"]:
        project, bucket = parts[2], None
    elif (
        kind == "gcp/bucket"
        and len(parts) == 5
        and parts[:2] == ["gcp", "projects"]
        and parts[3] == "buckets"
    ):
        project, bucket = parts[2], parts[4]
    else:
        raise RwError("invalid_input", f"expected a gcp/project or gcp/bucket target, got {kind!r}")
    if not valid_project(project) or (bucket is not None and not valid_bucket(bucket)):
        raise RwError("invalid_input", "the target's project or bucket name is not a valid name")
    return project, bucket
