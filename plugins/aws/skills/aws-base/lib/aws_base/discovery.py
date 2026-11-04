"""discover: the account, its regions and their Lambda functions, as facts and as an
rw.discovery.v1 sync.

The aws calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync").

Region is a type (integrations/aws types.yaml). Every region that was tried is a region
item with its own function partition: `complete` (also with count 0, so functions an earlier run
found there are swept when they are gone), or `forbidden` / `failed` when it could not be read, in
which case nothing under it is swept."""

import time

from .awscli import DEFAULT_TIMEOUT, aws_json, aws_paged, check_region
from .errors import RwError

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
    credential for another account would otherwise sync that account's functions under this
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


def function_facts(row):
    """One function row of list-functions, kept to the fields discovery reports."""
    return {
        "name": row["FunctionName"],
        "arn": row.get("FunctionArn"),
        "runtime": row.get("Runtime"),
        "packageType": row.get("PackageType"),
        "memorySizeMb": row.get("MemorySize"),
        "timeoutSeconds": row.get("Timeout"),
        "state": row.get("State"),
        "lastUpdateStatus": row.get("LastUpdateStatus"),
        "lastModified": row.get("LastModified"),
    }


def read_region(ctx, region, timeout=None):
    """The functions of one region: {name, status, functions[], error?}. A region the caller
    cannot read is `forbidden` (permission_denied) or `failed` (anything else), with the error;
    it does not stop the other regions."""
    try:
        rows, truncated = aws_paged(
            ctx,
            ["lambda", "list-functions", "--query", FUNCTIONS_QUERY],
            region=region,
            items_key="Functions",
            timeout=timeout,
        )
    except RwError as exc:
        status = "forbidden" if exc.code == "permission_denied" else "failed"
        return {
            "name": region,
            "status": status,
            "functions": [],
            "error": {"code": exc.code, "message": f"{region}: {exc.message}", "scope": region},
        }
    functions = [function_facts(r) for r in rows if isinstance(r, dict) and r.get("FunctionName")]
    if truncated:
        # never a "complete" partition for a list that was cut
        return {
            "name": region,
            "status": "failed",
            "functions": functions,
            "error": {
                "code": "internal",
                "message": f"{region}: more functions than could be read; the list is incomplete",
                "scope": region,
            },
        }
    return {"name": region, "status": "ok", "functions": functions}


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def region_listing(scans, limit):
    rows = [
        {
            "name": s["name"],
            "status": s["status"],
            "functions": len(s["functions"]) if s["status"] == "ok" else None,
        }
        for s in scans
    ]
    items, total, omitted = bound(rows, limit)
    return {"items": items, "total": total, "omitted": omitted}


def partitions(path, scans, regions_complete):
    """What a full sync of the account sweeps: its regions, and the functions of every region that
    was tried (an `ok` region with no function is a complete partition of count 0)."""
    out = [
        {
            "type": "region",
            "parentPath": path,
            "status": "complete" if regions_complete else "excluded",
            "count": len(scans),
        }
    ]
    for scan in scans:
        part = {
            "type": "lambda-function",
            "parentPath": f"{path}/regions/{scan['name']}",
            "status": {"ok": "complete", "forbidden": "forbidden"}.get(scan["status"], "failed"),
            "count": len(scan["functions"]),
        }
        if scan["status"] != "ok":
            part["reason"] = scan["error"]["code"]
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
        if scan["status"] != "ok":
            item["status"] = {"discovery": scan["status"]}
        items.append(item)
        for fn in scan["functions"]:
            items.append(
                {
                    "identity": {
                        "platform": PLATFORM,
                        "chain": [root, region, {"type": "lambda-function", "name": fn["name"]}],
                    },
                    **({"providerUid": fn["arn"]} if fn["arn"] else {}),
                    "displayName": fn["name"],
                    "document": {k: v for k, v in fn.items() if k != "name" and v is not None},
                    "status": {
                        k: v
                        for k, v in (
                            ("state", fn["state"]),
                            ("lastUpdateStatus", fn["lastUpdateStatus"]),
                        )
                        if v is not None
                    },
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


def denied_regions_error(scans):
    """One permission_denied error for every region the credential may not list (an SCP, a
    permission boundary, a policy that names regions), or None. Listing them in one error keeps
    the run's error list short; each region's own partition still says `forbidden`."""
    names = [s["name"] for s in scans if s["status"] == "forbidden"]
    if not names:
        return None
    shown = ", ".join(names[:40]) + (f" and {len(names) - 40} more" if len(names) > 40 else "")
    return {
        "code": "permission_denied",
        "message": (
            f"lambda:ListFunctions is denied in {len(names)} region(s): {shown}. A service control "
            "policy or the credential's policy forbids them; set the regions setting to the "
            "regions you use to stop reading these"
        ),
        "scope": "regions",
    }


def unread(region):
    """The scan of a region the loop ran out of time for: failed, never reported as empty."""
    return {
        "name": region,
        "status": "failed",
        "functions": [],
        "error": {
            "code": "timeout",
            "message": f"{region}: not read, the {READ_BUDGET}s budget for reading regions is used up",
            "scope": region,
        },
    }


def discover(ctx, settings):
    """Run discovery on the account this task targets; returns the task's outputs and the sync
    document. A credential that sts rejects (or that is for another account than the target), or
    a region list that cannot be read, raises. A region that cannot be read is reported and
    discovery goes on: the forbidden ones as one error listing them, the others each scoped to
    their region."""
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
    scans = []
    for region in regions:
        left = READ_BUDGET - (time.monotonic() - started)
        if left < 1:
            scan = unread(region["name"])  # a per-resource loop has a deadline
        else:
            scan = read_region(ctx, region["name"], timeout=min(DEFAULT_TIMEOUT, left))
        scan["optInStatus"] = region["optInStatus"]
        scans.append(scan)
    denied = denied_regions_error(scans)
    errors = ([denied] if denied else []) + [s["error"] for s in scans if s["status"] == "failed"]
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])

    parts = partitions(path, scans, regions_complete)
    summary = summarize(
        {
            "account": 1,
            "region": len(scans),
            "lambda-function": sum(len(s["functions"]) for s in scans),
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
