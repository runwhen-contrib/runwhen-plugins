"""The instance a task runs on, and the settings and inputs it reads, validated before any call."""

import re
from types import SimpleNamespace
from typing import NamedTuple

from .awscli import check_region, parse_duration
from .errors import RwError

TYPE = "aws/ec2-instance"
# Instance ids: `i-` and 8 hex digits (before 2016) or 17. The id goes into argv, so nothing else
# is let through.
_ID = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})")
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
    instance_id: str
    account: str
    arn: str  # arn:<partition>:ec2:<region>:<account>:instance/<id>


def partition_of(region):
    for prefix, partition in _PARTITIONS:
        if region.startswith(prefix):
            return partition
    return "aws"


def ec2_target(resource):
    """The instance a run targets as a Target(region, instance_id, account, arn); anything but an
    aws/ec2-instance is refused. Region and account are the target's own: ctx.resource.region and
    .account, else the path's segments, never the credential's or the environment's."""
    resource = resource or {}
    if resource.get("type") != TYPE or not resource.get("name"):
        raise RwError("invalid_input", f"this task runs on an {TYPE}, got {resource.get('type')!r}")
    instance_id = str(resource["name"])
    if not _ID.fullmatch(instance_id):
        raise RwError("invalid_input", f"not an EC2 instance id: {instance_id[:64]!r}")
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
    arn = f"arn:{partition_of(region)}:ec2:{region}:{account}:instance/{instance_id}"
    return Target(region, instance_id, account, arn)


def read_settings(settings):
    """The task's settings as a namespace; a bad value raises RwError invalid_input. Settings a
    task does not declare read as their defaults."""

    def get(name, default):
        return getattr(settings, name, default)

    max_items = get("max_items", 200)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
        raise RwError("invalid_input", "setting maxItems must be an integer from 1 to 10000")
    cpu = get("cpu_utilization_threshold", 80)
    if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not 0 <= cpu <= 100:
        raise RwError(
            "invalid_input", "setting cpuUtilizationThreshold must be a percentage from 0 to 100"
        )
    credits = get("cpu_credit_balance_threshold", 20)
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
        raise RwError(
            "invalid_input", "setting cpuCreditBalanceThreshold must be a number of 0 or more"
        )
    return SimpleNamespace(
        max_items=max_items,
        cpu_utilization_threshold=float(cpu),
        cpu_credit_balance_threshold=float(credits),
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
