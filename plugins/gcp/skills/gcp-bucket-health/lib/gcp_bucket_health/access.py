"""public-access: whether a bucket names allUsers or allAuthenticatedUsers, in its IAM policy, in
its legacy ACLs or in its default object ACL (the ACL new objects start with). Facts only: the
grant, how it is expressed and whether public access prevention makes it ineffective.

The ACLs and the access settings come from the bucket resource the listing already returned (no
call per bucket); the IAM policy is the one read per bucket, under a time budget."""

import time

from .buckets import listing, per_bucket, select, totals_of
from .config import MAX_LABELS, clip
from .errors import RwError
from .gcloud import gcloud_json

PUBLIC = ("allUsers", "allAuthenticatedUsers")
NESTED = MAX_LABELS  # nested lists are cut at 50
TASK_BUDGET = 600  # seconds: public-access's `timeout` in tasks.yaml (a test keeps them equal)
RESERVE = 10.0  # seconds kept free after the last bucket for the result


def public_iam_bindings(policy):
    """[{role, members, conditional}] for the bindings that name a public member."""
    found = []
    for binding in policy.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        members = [m for m in binding.get("members") or [] if m in PUBLIC]
        if members:
            found.append(
                {
                    "role": clip(binding.get("role")),
                    "members": sorted(set(members)),
                    "conditional": bool(binding.get("condition")),
                }
            )
    return found


def public_acl_entries(row, field="acl"):
    """[{entity, role}] for the entries of the bucket's `acl` (or `defaultObjectAcl`) that name a
    public entity."""
    return [
        {"entity": e["entity"], "role": clip(e.get("role"))}
        for e in row.get(field) or []
        if isinstance(e, dict) and e.get("entity") in PUBLIC
    ]


def read_policy(ctx, bucket, timeout):
    policy = gcloud_json(
        ctx, "storage", "buckets", "get-iam-policy", f"gs://{bucket}", timeout=timeout
    )
    if not isinstance(policy, dict):
        raise RwError("internal", f"gcloud returned no IAM policy for {bucket}")
    return policy


def bucket_access(ctx, row, timeout):
    """The `access` entry of one bucket resource. The resource is the primary read (it fails the
    bucket when it fails). The IAM policy is a second read that may fail on its own (denied, or
    throttled): then `publicViaIam` is null and the error is scoped to the bucket (partial)."""
    bucket = row["name"]
    iam = row.get("iamConfiguration") or {}
    uniform = bool(
        (iam.get("uniformBucketLevelAccess") or iam.get("bucketPolicyOnly") or {}).get("enabled")
    )
    prevention = clip(iam.get("publicAccessPrevention"))
    bindings = None
    try:
        bindings = public_iam_bindings(read_policy(ctx, bucket, timeout))
    except RwError as exc:
        if exc.code not in ("permission_denied", "rate_limited"):
            raise
        ctx.error(exc.code, f"{bucket}: IAM policy: {exc.message}", scope=bucket)
    # ACLs, default object ACLs included, do not apply to a bucket with uniform bucket-level
    # access: IAM is the only path
    acl = [] if uniform else public_acl_entries(row, "acl")
    default_acl = [] if uniform else public_acl_entries(row, "defaultObjectAcl")
    via_iam = None if bindings is None else bool(bindings)
    via_acl = bool(acl)
    via_default = bool(default_acl)
    grant = True if (via_iam or via_acl or via_default) else (None if via_iam is None else False)
    return {
        "name": bucket,
        "publicGrant": grant,
        "publicViaIam": via_iam,
        "publicViaAcl": via_acl,
        "publicViaDefaultObjectAcl": via_default,
        "uniformBucketLevelAccess": uniform,
        "publicAccessPrevention": prevention,
        "blockedByPrevention": bool(grant) and prevention == "enforced",
        "iamBindings": (bindings or [])[:NESTED],
        "aclEntries": acl[:NESTED],
        "defaultObjectAclEntries": default_acl[:NESTED],
    }


def totals(entries, total, looked_at, threshold):
    """Counts over the buckets examined: `publicBuckets` have a public grant, `unknown` could not
    be decided (IAM policy unreadable and no ACL grant), `overThreshold` is publicBuckets above
    the setting; `total`, `omitted` and `complete` as in every task's totals."""
    public = sum(1 for e in entries if e["publicGrant"] is True)
    unknown = sum(1 for e in entries if e["publicGrant"] is None)
    out = totals_of(entries, total, looked_at, undecided=unknown)
    out.update(
        publicBuckets=public,
        unknown=unknown,
        threshold=threshold,
        overThreshold=public > threshold,
    )
    return out


def deadline_from(started):
    """The monotonic time by which the last bucket must have started, for a task that started at
    `started`."""
    return started + TASK_BUDGET - RESERVE


def run(ctx, st, deadline, clock=time.monotonic):
    project, rows, total, single = select(ctx, st.max_items, st.command_timeout)
    entries = per_bucket(
        ctx,
        rows,
        single,
        lambda row: bucket_access(ctx, row, st.command_timeout),
        deadline=deadline,
        clock=clock,
    )
    return {
        "access": listing(entries, total, len(rows)),
        "totals": totals(entries, total, len(rows), st.public_bucket_threshold),
    }
