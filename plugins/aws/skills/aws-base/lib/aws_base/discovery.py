"""discover: the account, its regions and what lives in them (Lambda functions, EC2 instances), as
facts and as an rw.discovery.v1 sync.

The aws calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync").

Region is a type (integrations/aws types.yaml). What lives under a region is a row of the KINDS
table below, one per type a skill of this plugin targets: add the type to types.yaml and a Kind
here, nothing else. Every region that was tried is a region item with, for every kind, its own
partition: `complete` (also with count 0, so items an earlier run found there are swept when they
are gone), or `forbidden` / `failed` when that list could not be read, in which case nothing under
it is swept. One kind being forbidden does not stop the others in the region."""

import subprocess
import time
from typing import Callable, NamedTuple

from .awscli import DEFAULT_TIMEOUT, aws_json, aws_paged, check_region
from .errors import RwError
from .text import tag_facts

PLATFORM = "aws"
SOURCE = "aws-base/discover"
BATCH = 500  # items per batch (rw.discovery.v1 itemsBatch)
CANONICAL_KEY = "accountId"
READ_BUDGET = 420  # seconds the region loop may take; the rest of the task's 600s is for the push

# One projection per call, so no response carries more than the fields below: list-functions
# also returns each function's Environment variables, which this task never needs or keeps.
# NextToken must stay in the projection (awscli.aws_paged requires it).
FUNCTIONS_QUERY = (
    "{Functions: Functions[].{FunctionName: FunctionName, FunctionArn: FunctionArn, "
    "Runtime: Runtime, PackageType: PackageType, MemorySize: MemorySize, Timeout: Timeout, "
    "State: State, LastUpdateStatus: LastUpdateStatus, LastModified: LastModified}, "
    "NextToken: NextToken}"
)
# describe-instances returns far more (user data is not among it, but tags, network interfaces and
# block devices are): only the fields below are kept. A terminated instance is described for about
# an hour after it is gone; it is not listed, so it is swept from the inventory.
INSTANCES_FILTER = "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped"
INSTANCES_QUERY = (
    "{Instances: Reservations[].Instances[].{InstanceId: InstanceId, InstanceType: InstanceType, "
    "State: State.Name, LaunchTime: LaunchTime, AvailabilityZone: Placement.AvailabilityZone, "
    "Name: Tags[?Key=='Name'] | [0].Value}, NextToken: NextToken}"
)
# the ARN partition of a region: aws unless the region's prefix says otherwise
_PARTITIONS = (
    ("cn-", "aws-cn"),
    ("us-gov-", "aws-us-gov"),
    ("us-isob-", "aws-iso-b"),
    ("us-iso-", "aws-iso"),
    ("eusc-", "aws-eusc"),
)


def partition_of(region):
    for prefix, partition in _PARTITIONS:
        if region.startswith(prefix):
            return partition
    return "aws"


def target_name(resource):
    """The account name from the run's target; anything but an aws/account is refused."""
    resource = resource or {}
    if resource.get("type") != f"{PLATFORM}/account" or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"discover runs on an aws/account, got {resource.get('type')!r}",
        )
    return str(resource["name"])


def read_identity(ctx, region):
    """{accountId, callerArn} from sts get-caller-identity (it needs no IAM permission, so a
    failure here is the credential: expired, invalid, missing)."""
    doc = aws_json(ctx, ["sts", "get-caller-identity"], region=region)
    account = doc.get("Account") if isinstance(doc, dict) else None
    if not (isinstance(account, str) and len(account) == 12 and account.isdigit()):
        raise RwError("internal", "sts get-caller-identity returned no 12-digit account id")
    arn = doc.get("Arn")
    return {"accountId": account, "callerArn": arn if isinstance(arn, str) else None}


def check_target_account(name, identity):
    """The credential must be the target's: an aws/account is named by its 12-digit id, and a
    credential for another account would otherwise sync that account's resources under this
    root. A name that is not an account id (an alias) cannot be compared and is let through."""
    if name.isdigit() and len(name) == 12 and name != identity["accountId"]:
        raise RwError(
            "permission_denied",
            f"credential is for account {identity['accountId']}, target is {name}",
        )


def enabled_regions(ctx, region):
    """[{name, optInStatus}] of the regions the account can call (describe-regions without
    --all-regions lists enabled regions only)."""
    doc = aws_json(ctx, ["ec2", "describe-regions"], region=region)
    rows = doc.get("Regions") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise RwError("internal", "ec2 describe-regions returned no Regions list")
    out = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("RegionName"), str):
            out.append({"name": row["RegionName"], "optInStatus": row.get("OptInStatus")})
    return sorted(out, key=lambda r: r["name"])


# --- the kinds: one row per type under a region -------------------------------------------------


def function_node(row, account, region):
    """One list-functions row as a sync node, kept to the fields discovery reports."""
    if not row.get("FunctionName"):
        return None
    facts = {
        "arn": row.get("FunctionArn"),
        "runtime": row.get("Runtime"),
        "packageType": row.get("PackageType"),
        "memorySizeMb": row.get("MemorySize"),
        "timeoutSeconds": row.get("Timeout"),
        "state": row.get("State"),
        "lastUpdateStatus": row.get("LastUpdateStatus"),
        "lastModified": row.get("LastModified"),
    }
    status = {"state": facts["state"], "lastUpdateStatus": facts["lastUpdateStatus"]}
    return {
        "name": row["FunctionName"],
        "uid": facts["arn"],
        "document": {k: v for k, v in facts.items() if v is not None},
        "status": {k: v for k, v in status.items() if v is not None},
    }


def instance_node(row, account, region):
    """One describe-instances row as a sync node. The display name is the Name tag unless its value
    looks like a secret (tags are free text), else the instance id."""
    instance_id = row.get("InstanceId")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    facts = {
        "instanceType": row.get("InstanceType"),
        "state": row.get("State"),
        "availabilityZone": row.get("AvailabilityZone"),
        "launchTime": row.get("LaunchTime"),
    }
    name = tag_facts("Name", row.get("Name"))["value"]  # None when the value looks like a secret
    document = {k: v for k, v in facts.items() if v is not None}
    if name:
        document["name"] = name
    return {
        "name": instance_id,
        "display": name or instance_id,
        "uid": f"arn:{partition_of(region)}:ec2:{region}:{account}:instance/{instance_id}",
        "document": document,
        "status": {"state": facts["state"]} if facts["state"] else {},
    }


class Kind(NamedTuple):
    type: str  # the type name in types.yaml
    key: str  # the count field of the regions output
    label: str  # what the list holds, for messages
    action: str  # the IAM action the list needs, for the denied-regions message
    scope: str  # the scope of the "denied in N regions" error
    args: tuple  # the aws words of the list call, --query included
    items_key: str
    node: Callable  # (row, accountId, region) -> sync node or None


KINDS = (
    Kind(
        "lambda-function", "functions", "functions", "lambda:ListFunctions", "regions",
        ("lambda", "list-functions", "--query", FUNCTIONS_QUERY), "Functions", function_node,
    ),
    Kind(
        "ec2-instance", "instances", "instances", "ec2:DescribeInstances", "regions:ec2-instance",
        ("ec2", "describe-instances", "--filters", INSTANCES_FILTER, "--query", INSTANCES_QUERY),
        "Instances", instance_node,
    ),
)  # fmt: skip


class Bounded:
    """ctx.run with a deadline, for the calls of the region loop: a call is given at most what is
    left, and one started with nothing left (including the next page of a paged list) times out
    instead of running. aws_json turns the TimeoutExpired into a `timeout` RwError."""

    def __init__(self, ctx, deadline):
        self._ctx = ctx
        self._deadline = deadline

    def run(self, argv, **kwargs):
        left = self._deadline - time.monotonic()
        if left < 1:
            raise subprocess.TimeoutExpired(argv, 0)
        kwargs["timeout"] = min(kwargs.get("timeout") or DEFAULT_TIMEOUT, left)
        return self._ctx.run(argv, **kwargs)


def read_kind(ctx, kind, region, account):
    """One kind in one region: {status, items[], error?}. A list the caller cannot read is
    `forbidden` (permission_denied) or `failed` (anything else), with the error; it does not stop
    the other kinds or regions."""
    try:
        rows, truncated = aws_paged(ctx, list(kind.args), region=region, items_key=kind.items_key)
    except RwError as exc:
        status = "forbidden" if exc.code == "permission_denied" else "failed"
        return {
            "status": status,
            "items": [],
            "error": {"code": exc.code, "message": f"{region}: {exc.message}", "scope": region},
        }
    nodes = [n for n in (kind.node(r, account, region) for r in rows if isinstance(r, dict)) if n]
    if truncated:
        # never a "complete" partition for a list that was cut
        return {
            "status": "failed",
            "items": nodes,
            "error": {
                "code": "internal",
                "message": f"{region}: more {kind.label} than could be read; the list is incomplete",
                "scope": region,
            },
        }
    return {"status": "ok", "items": nodes}


def read_region(ctx, region, account, deadline):
    """Every kind of one region: {name, kinds: {type: {status, items[], error?}}}."""
    bounded = Bounded(ctx, deadline)
    return {
        "name": region,
        "kinds": {k.type: read_kind(bounded, k, region, account) for k in KINDS},
    }


def region_status(scan):
    """The worst status of the region's kinds: failed, else forbidden, else ok."""
    statuses = {part["status"] for part in scan["kinds"].values()}
    return "failed" if "failed" in statuses else "forbidden" if "forbidden" in statuses else "ok"


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def region_listing(scans, limit):
    """One row per region: its worst status and, per kind, how many were found (null when that list
    could not be read; 0 for a region that was read and holds none)."""
    rows = [
        {
            "name": s["name"],
            "status": region_status(s),
            **{
                k.key: len(s["kinds"][k.type]["items"])
                if s["kinds"][k.type]["status"] == "ok"
                else None
                for k in KINDS
            },
        }
        for s in scans
    ]
    items, total, omitted = bound(rows, limit)
    return {"items": items, "total": total, "omitted": omitted}


PART_STATUS = {"ok": "complete", "forbidden": "forbidden"}


def partitions(path, scans, regions_complete):
    """What a full sync of the account sweeps: its regions, and the resources of every kind in every
    region that was tried (an `ok` list with no resource is a complete partition of count 0)."""
    out = [
        {
            "type": "region",
            "parentPath": path,
            "status": "complete" if regions_complete else "excluded",
            "count": len(scans),
        }
    ]
    for scan in scans:
        for kind in KINDS:
            read = scan["kinds"][kind.type]
            part = {
                "type": kind.type,
                "parentPath": f"{path}/regions/{scan['name']}",
                "status": PART_STATUS.get(read["status"], "failed"),
                "count": len(read["items"]),
            }
            if read["status"] != "ok":
                part["reason"] = read["error"]["code"]
            out.append(part)
    return out


def summarize(counts, parts, errors, duration_ms, account_id=None):
    """The rw.discovery.summary.v1 value, also the commit's `summary`. The script builds it a
    second time after the push, with the push's errors and the partitions the platform accepted."""
    totals = {"complete": 0, "failed": 0, "forbidden": 0, "excluded": 0}
    for p in parts:
        totals[p["status"]] += 1
    summary = {"counts": dict(counts), "partitions": totals, "durationMs": duration_ms}
    if errors:
        summary["errors"] = list(errors)
    if account_id:
        summary["root"] = {"canonicalKey": {"name": CANONICAL_KEY, "value": account_id}}
    return summary


def build_sync(path, name, identity, scans, regions_complete, summary):
    """The rw.discovery.v1 document for one full sync of the account at `path`."""
    root = {"type": "account", "name": name}
    items = [
        {
            "identity": {"platform": PLATFORM, "chain": [root]},
            "providerUid": identity["accountId"],
            "displayName": name,
            "document": {"accountId": identity["accountId"]},
        }
    ]
    for scan in scans:
        region = {"type": "region", "name": scan["name"]}
        item = {
            "identity": {"platform": PLATFORM, "chain": [root, region]},
            "displayName": scan["name"],
            "document": {
                "name": scan["name"],
                **({"optInStatus": scan["optInStatus"]} if scan.get("optInStatus") else {}),
            },
        }
        if region_status(scan) != "ok":
            item["status"] = {"discovery": region_status(scan)}
        items.append(item)
        for kind in KINDS:
            for node in scan["kinds"][kind.type]["items"]:
                items.append(
                    {
                        "identity": {
                            "platform": PLATFORM,
                            "chain": [root, region, {"type": kind.type, "name": node["name"]}],
                        },
                        **({"providerUid": node["uid"]} if node["uid"] else {}),
                        "displayName": node.get("display") or node["name"],
                        "document": node["document"],
                        "status": node["status"],
                    }
                )
    batches = [
        {"seq": i // BATCH + 1, "items": items[i : i + BATCH]} for i in range(0, len(items), BATCH)
    ]
    return {
        "apiVersion": "rw.discovery.v1",
        "open": {"source": SOURCE, "scopePath": path, "mode": "full"},
        "batches": batches,
        "commit": {
            "partitions": partitions(path, scans, regions_complete),
            "summary": summary,
        },
    }


def denied_regions_error(scans, kind):
    """One permission_denied error for every region the credential may not list `kind` in (an SCP,
    a permission boundary, a policy that names regions), or None. Listing them in one error keeps
    the run's error list short; each region's own partition still says `forbidden`."""
    names = [s["name"] for s in scans if s["kinds"][kind.type]["status"] == "forbidden"]
    if not names:
        return None
    shown = ", ".join(names[:40]) + (f" and {len(names) - 40} more" if len(names) > 40 else "")
    return {
        "code": "permission_denied",
        "message": (
            f"{kind.action} is denied in {len(names)} region(s): {shown}. A service control "
            "policy or the credential's policy forbids them; set the regions setting to the "
            "regions you use to stop reading these"
        ),
        "scope": kind.scope,
    }


def unread(region):
    """The scan of a region the loop ran out of time for: every kind failed, never reported as
    empty."""
    error = {
        "code": "timeout",
        "message": f"{region}: not read, the {READ_BUDGET}s budget for reading regions is used up",
        "scope": region,
    }
    return {
        "name": region,
        "kinds": {k.type: {"status": "failed", "items": [], "error": error} for k in KINDS},
    }


def scan_errors(scans):
    """The run's errors: one for the forbidden regions of each kind, then each failed list's own
    (a region the loop never reached has one error, however many kinds it holds)."""
    errors = [e for e in (denied_regions_error(scans, k) for k in KINDS) if e]
    for scan in scans:
        for kind in KINDS:
            read = scan["kinds"][kind.type]
            if read["status"] == "failed" and read["error"] not in errors:
                errors.append(read["error"])
    return errors


def discover(ctx, settings):
    """Run discovery on the account this task targets; returns the task's outputs and the sync
    document. A credential that sts rejects (or that is for another account than the target), or
    a region list that cannot be read, raises. A list that cannot be read is reported and
    discovery goes on: the forbidden ones as one error listing the regions, the others each
    scoped to their region."""
    started = time.monotonic()
    name = target_name(ctx.resource)
    path = ctx.resource["path"]
    home = settings.default_region
    identity = read_identity(ctx, home)
    check_target_account(name, identity)

    if settings.regions:
        regions = [{"name": check_region(r), "optInStatus": None} for r in settings.regions]
        regions_complete = False  # the caller narrowed the regions: nothing outside is swept
    else:
        regions = enabled_regions(ctx, home)
        regions_complete = True
    deadline = started + READ_BUDGET
    scans = []
    for region in regions:
        if deadline - time.monotonic() < 1:
            scan = unread(region["name"])  # a per-resource loop has a deadline
        else:
            scan = read_region(ctx, region["name"], identity["accountId"], deadline)
        scan["optInStatus"] = region["optInStatus"]
        scans.append(scan)
    errors = scan_errors(scans)
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])

    parts = partitions(path, scans, regions_complete)
    summary = summarize(
        {
            "account": 1,
            "region": len(scans),
            **{k.type: sum(len(s["kinds"][k.type]["items"]) for s in scans) for k in KINDS},
        },
        parts,
        errors,
        int((time.monotonic() - started) * 1000),
        identity["accountId"],
    )
    sync = build_sync(path, name, identity, scans, regions_complete, summary)
    outputs = {
        "summary": summary,
        "account": {
            "accountId": identity["accountId"],
            "callerArn": identity["callerArn"],
            "homeRegion": home,
            "regionsRead": len(scans),
            "regionsComplete": regions_complete,
        },
        "regions": region_listing(scans, settings.max_items),
    }
    return outputs, sync
