"""The function a task runs on, and the settings and inputs it reads, validated before any call."""

import re
from types import SimpleNamespace
from typing import NamedTuple

from .awscli import check_region, parse_duration
from .errors import RwError

TYPE = "aws/lambda-function"
# Lambda function names: letters, digits, hyphens and underscores, 1 to 64 characters. The name
# goes into argv and into a log group name, so nothing else is let through.
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PATH_REGION = re.compile(r"/regions/([^/]+)/")
_PATH_ACCOUNT = re.compile(r"(?:^|/)accounts/([^/]+)/")
_ACCOUNT = re.compile(r"[0-9]{12}")
# the ARN partition of a region: aws unless the region's prefix says otherwise
_PARTITIONS = (
    ("cn-", "aws-cn"),
    ("us-gov-", "aws-us-gov"),
    ("us-isob-", "aws-iso-b"),
    ("us-iso-", "aws-iso"),
    ("eusc-", "aws-eusc"),
)
MAX_SINCE_SECONDS = 14 * 86400  # CloudWatch keeps one-minute metrics for 15 days


class Target(NamedTuple):
    region: str
    name: str
    account: str
    arn: str  # the function's full ARN, the value every call passes as --function-name


def partition_of(region):
    for prefix, partition in _PARTITIONS:
        if region.startswith(prefix):
            return partition
    return "aws"


def lambda_target(resource):
    """The function a run targets as a Target(region, name, account, arn); anything but an
    aws/lambda-function is refused. Region and account are the target's own: ctx.resource.region
    and .account, else the path's segments. The ARN is built from them, never from the
    credential, so a credential for another account cannot read a same-named function of its own:
    every `--function-name` is the full ARN (Lambda accepts it and refuses a foreign account)."""
    resource = resource or {}
    if resource.get("type") != TYPE or not resource.get("name"):
        raise RwError("invalid_input", f"this task runs on an {TYPE}, got {resource.get('type')!r}")
    name = str(resource["name"])
    if not _NAME.fullmatch(name):
        raise RwError("invalid_input", f"not a Lambda function name: {name[:64]!r}")
    path = str(resource.get("path", "")) + "/"
    region = resource.get("region")
    if not region:
        match = _PATH_REGION.search(path)
        region = match.group(1) if match else None
    account = resource.get("account")
    if not account:
        match = _PATH_ACCOUNT.search(path)
        account = match.group(1) if match else None
    check_region(region)
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise RwError("invalid_input", f"not a 12-digit AWS account id: {str(account)[:32]!r}")
    arn = f"arn:{partition_of(region)}:lambda:{region}:{account}:function:{name}"
    return Target(region, name, account, arn)


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input. Settings a
    task does not declare read as their defaults."""

    def get(name, default):
        return getattr(settings, name, default)

    max_items = get("max_items", 200)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    max_events = get("max_log_events", 50)
    if (
        isinstance(max_events, bool)
        or not isinstance(max_events, int)
        or not 1 <= max_events <= 1000
    ):
        raise RwError("invalid_input", "setting maxLogEvents must be an integer from 1 to 1000")
    pattern = get("error_pattern", "")
    if not isinstance(pattern, str) or len(pattern) > 200 or any(ord(c) < 32 for c in pattern):
        raise RwError(
            "invalid_input", "setting errorPattern must be one line of at most 200 characters"
        )
    if pattern.startswith("-"):
        # the aws CLI would read it as an option
        raise RwError("invalid_input", "setting errorPattern must not start with '-'")
    rate = get("error_rate_threshold", 0.05)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise RwError("invalid_input", "setting errorRateThreshold must be a number from 0 to 1")
    throttles = get("throttle_threshold", 1)
    if isinstance(throttles, bool) or not isinstance(throttles, int) or throttles < 1:
        raise RwError("invalid_input", "setting throttleThreshold must be an integer of 1 or more")
    ratio = get("duration_timeout_ratio", 0.9)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.1 <= ratio <= 1:
        raise RwError(
            "invalid_input", "setting durationTimeoutRatio must be a number from 0.1 to 1"
        )
    return SimpleNamespace(
        max_items=max_items,
        max_log_events=min(max_events, max_items),
        error_pattern=pattern,
        error_rate_threshold=float(rate),
        throttle_threshold=throttles,
        duration_timeout_ratio=float(ratio),
    )


def read_since(value):
    """The look-back in seconds from the `since` input (a duration such as 1h or 90m)."""
    seconds = parse_duration(value, "since")
    if seconds > MAX_SINCE_SECONDS:
        raise RwError(
            "invalid_input",
            "since must be at most 14d: CloudWatch keeps one-minute metrics 15 days",
        )
    return int(seconds)
