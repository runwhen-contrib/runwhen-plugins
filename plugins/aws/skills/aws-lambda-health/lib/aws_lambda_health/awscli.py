"""Running aws CLI v2 calls that return one JSON document, and classifying their failures.

This file is shared by every aws skill. Skills do not import each other (a task sees only its own
skill's lib/), so each skill carries a verbatim copy of `errors.py` and `awscli.py` in its own
`lib/<skill_snake>/`; they import only `.errors`, so the copy needs no edit. A test
(the plugin unit test test_aws_copies.py) fails when a copy drifts from `aws_base`'s.

    from aws_base.awscli import aws_json, aws_paged     # in another skill: from <skill_snake>.awscli import ...

    who = aws_json(ctx, ["sts", "get-caller-identity"], region="us-east-1")
    items, truncated = aws_paged(
        ctx, ["lambda", "list-functions", "--query", QUERY], region="eu-west-1",
        items_key="Functions", limit=500)

The credential is the aws CLI's own environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_SESSION_TOKEN, or the ambient identity), which the runner materializes from the connection's
`aws/sdk` credential; this module never reads or prints it. Every call names its region with
`--region`: the region of a target is the target's, never the environment's. Calls run with
`--output json --no-cli-pager`. A non-zero exit raises RwError with the D64 code classified from
the error code in stderr (`An error occurred (AccessDeniedException) when calling ...`), not from
the exit code alone: the CLI exits 254 for every service error and 255 for most others.

Pagination. `--query` runs after the CLI has paginated and drops everything it does not select,
including the `NextToken` that `--max-items` adds, so a projection that forgets it makes a
truncated list read as complete. `aws_paged` requires the query to select `NextToken` (it is null
when there is no more) and raises `internal` when the key is missing from the document.
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


DEFAULT_TIMEOUT = 60.0  # seconds a single CLI call may take (the CLI retries throttling inside it)
MESSAGE_LIMIT = 1024
MAX_PAGES = 100  # pages aws_paged follows before it gives up and reports truncated

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
# The one region pattern of the plugin. Accepts us-east-1, eu-central-2, ap-southeast-4,
# us-gov-west-1, cn-north-1, us-iso-east-1 and the sovereign cloud's eusc-de-east-1 (a 2 to 4 letter
# geography prefix).
_REGION = re.compile(r"[a-z]{2,4}(?:-[a-z]+)+-[0-9]{1,2}")


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


def check_region(region):
    """The region name, or invalid_input: it goes into argv and into resource paths."""
    if not isinstance(region, str) or not _REGION.fullmatch(region):
        raise RwError("invalid_input", f"not an AWS region name: {str(region)[:64]!r}")
    return region


# --- failure classification ----------------------------------------------------------------------
# The CLI words a service error `An error occurred (<Code>) when calling the <Op> operation
# [(reached max retries: N)]: <message>`, behind a blank line and `aws: [ERROR]: ` in v2.39 (older
# v2 had no prefix). Codes are matched on the code, anywhere in stderr, never on the prefix.
_CODE = re.compile(r"An error occurred \((?P<code>[A-Za-z0-9_.:-]+)\)")

_PERMISSION = frozenset(
    """AccessDenied AccessDeniedException UnauthorizedOperation UnauthorizedAccess Unauthorized
    UnauthorizedException AuthFailure AuthorizationError NotAuthorized Forbidden ExpiredToken
    ExpiredTokenException RequestExpired InvalidClientTokenId UnrecognizedClientException
    InvalidAccessKeyId SignatureDoesNotMatch IncompleteSignature MissingAuthenticationToken
    InvalidSignatureException InvalidToken TokenRefreshRequired OptInRequired
    SubscriptionRequiredException NoCredentials PartialCredentials InvalidIdentityToken
    IDPCommunicationError RegionDisabledException""".split()
)
_NOT_FOUND = frozenset(
    """ResourceNotFoundException ResourceNotFound NoSuchEntity NoSuchBucket NoSuchKey
    NotFoundException ParameterNotFound FunctionNotFound""".split()
)
_RATE_LIMITED = frozenset(
    """Throttling ThrottlingException ThrottledException TooManyRequestsException TooManyRequests
    RequestLimitExceeded RequestThrottled RequestThrottledException EC2ThrottledException
    ProvisionedThroughputExceededException BandwidthLimitExceeded SlowDown""".split()
)
_INVALID_INPUT = frozenset(
    """ParamValidation ValidationException ValidationError InvalidParameterValue
    InvalidParameterValueException InvalidParameterCombination InvalidRequestException
    InvalidParameter MissingParameter MalformedQueryString""".split()
)
_TIMEOUT = frozenset("RequestTimeout RequestTimeoutException".split())
_UNREACHABLE = frozenset("ServiceUnavailable ServiceUnavailableException".split())

# wording without a code (client side failures), tried in order
_TEXT_RULES = (
    ("timeout", re.compile(r"(?:connect|read) timeout on endpoint url", re.I)),
    (
        "unreachable",
        re.compile(
            r"could not connect to the endpoint url|could not resolve|name or service not known"
            r"|temporary failure in name resolution|ssl validation failed|connection (?:was )?"
            r"(?:reset|closed)|proxy connection failed|network is unreachable|no route to host",
            re.I,
        ),
    ),
    (
        "permission_denied",
        re.compile(
            r"unable to locate credentials|is not authorized to perform"
            # IRSA / web identity: STS refuses the pod's token (the code is also matched above)
            r"|not authorized to perform:? sts:assumerolewithwebidentity"
            r"|error when retrieving credentials from assume-role-with-web-identity",
            re.I,
        ),
    ),
    (
        "invalid_input",
        re.compile(
            r"doesn't match a supported format|invalid endpoint|unknown options?:"
            r"|parameter validation failed|invalid choice|the following arguments are required",
            re.I,
        ),
    ),
)


def error_code(stderr):
    """The service error code named in stderr (`AccessDeniedException`), or None."""
    match = _CODE.search(str(stderr))
    return match["code"] if match else None


def classify_error(stderr, returncode=1):
    """D64 error code for a failed aws call, from the error code in its stderr; the exit code
    only for what has no code (127 no aws binary, 124 a killed run)."""
    if returncode == 127:
        return "dependency"
    text = str(stderr)
    code = error_code(text)
    if code is not None:
        for known, mapped in (
            (_NOT_FOUND, "not_found"),
            (_RATE_LIMITED, "rate_limited"),
            (_PERMISSION, "permission_denied"),
            (_INVALID_INPUT, "invalid_input"),
            (_TIMEOUT, "timeout"),
            (_UNREACHABLE, "unreachable"),
        ):
            if code in known:
                return mapped
        if code.endswith((".NotFound", ".Malformed")):  # EC2: InvalidInstanceID.NotFound
            return "not_found" if code.endswith(".NotFound") else "invalid_input"
        if code.endswith(("AccessDenied", "NotAuthorized", "Unauthorized")):
            return "permission_denied"
        if code.endswith(("Throttling", "ThrottlingException", "LimitExceeded")):
            return "rate_limited"
    for mapped, pattern in _TEXT_RULES:
        if pattern.search(text):
            return mapped
    if returncode == 124:
        return "timeout"
    return "internal"


def _clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 3] + "..."


def failure_message(stderr, returncode):
    """The error line of stderr for a RwError: no `aws: [ERROR]:` prefix, no usage block."""
    text = str(stderr).strip()
    text = text.split("\nusage: aws")[0].strip()
    text = re.sub(r"^aws: \[ERROR\]:\s*", "", text)
    return _clip(text) or f"aws exited {returncode}"


# --- running -------------------------------------------------------------------------------------


def aws_json(ctx, args, *, region, timeout=None):
    """Run `aws <args> --region <region> --output json --no-cli-pager` and return the parsed
    JSON document (None for an empty stdout).

    A non-zero exit raises RwError with the D64 code classified from stderr: permission_denied
    (AccessDenied*, UnauthorizedOperation, expired or invalid credentials, no credentials),
    not_found (ResourceNotFoundException, NoSuchEntity...), rate_limited (Throttling*,
    TooManyRequestsException, RequestLimitExceeded), invalid_input (ParamValidation,
    ValidationException, a malformed region), unreachable (cannot reach the endpoint),
    timeout (connect/read timeout or the run's own timeout), dependency (no aws in the runtime)
    or internal. `args` must be the task's own fixed words and values, never a shell string."""
    check_region(region)
    argv = [
        "aws",
        *[str(a) for a in args],
        "--region",
        region,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    seconds = DEFAULT_TIMEOUT if timeout is None else timeout
    try:
        result = ctx.run(argv, timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        raise RwError(
            "timeout", f"aws {args[0]} {args[1]} did not finish within {seconds:.0f}s"
        ) from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input", "the aws output is too large to return; narrow the call with --query"
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "the aws CLI is not installed in this runtime") from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RwError(
            classify_error(stderr, result.returncode), failure_message(stderr, result.returncode)
        )
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise RwError("internal", "the aws CLI did not return JSON") from None


def aws_paged(ctx, args, *, region, items_key, limit=None, page_size=1000, timeout=None):
    """Follow `--max-items` / `NextToken` and return (items, truncated).

    `args` select the list with `--query`, which must select `NextToken` beside the list:
    `{<items_key>: <path>, NextToken: NextToken}`. Without `limit` every page is read, up to
    MAX_PAGES pages; with `limit` the read stops once `limit` items are in hand. `truncated` is
    True when the service said there was more (a non-null NextToken) that was not read, so a
    list cut at `limit` or at MAX_PAGES is never presented as complete. A document without the
    `NextToken` key (a query that dropped it) or without `items_key` raises internal. A page call
    that fails raises its RwError with `.partial`, the list of items the earlier pages returned."""
    items = []
    token = None
    for _page in range(MAX_PAGES):
        want = page_size if limit is None else max(min(page_size, limit - len(items)), 1)
        call = [*args, "--max-items", want]
        if token is not None:
            call += ["--starting-token", token]
        try:
            doc = aws_json(ctx, call, region=region, timeout=timeout)
        except RwError as exc:
            exc.partial = items  # the items earlier pages returned, for a caller that can use them
            raise
        if not isinstance(doc, dict) or items_key not in doc or "NextToken" not in doc:
            raise RwError(
                "internal",
                f"the aws {args[0]} {args[1]} result has no {items_key!r} and NextToken: "
                "the --query must select both, or a truncated list reads as complete",
            )
        page = doc[items_key] or []
        if not isinstance(page, list):
            raise RwError(
                "internal", f"the aws {args[0]} {args[1]} result {items_key!r} is not a list"
            )
        items.extend(page)
        token = doc["NextToken"] or None
        if token is None:
            return items, False
        if limit is not None and len(items) >= limit:
            return items[:limit], True
    return items, True
