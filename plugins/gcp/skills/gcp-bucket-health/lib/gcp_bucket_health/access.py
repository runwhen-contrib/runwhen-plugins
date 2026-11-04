"""public-access: whether a bucket names allUsers or allAuthenticatedUsers, in its IAM policy and in
its legacy ACLs. Facts only: the grant, how it is expressed and whether public access prevention
makes it ineffective."""

from .buckets import describe, listing, per_bucket, select
from .config import MAX_LABELS, clip
from .errors import RwError
from .gcloud import gcloud_json

PUBLIC = ("allUsers", "allAuthenticatedUsers")
NESTED = MAX_LABELS  # nested lists are cut at 50


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


def public_acl_entries(row):
    """[{entity, role}] for the bucket ACL entries that name a public entity."""
    return [
        {"entity": e["entity"], "role": clip(e.get("role"))}
        for e in row.get("acl") or []
        if isinstance(e, dict) and e.get("entity") in PUBLIC
    ]


def read_policy(ctx, bucket, timeout):
    policy = gcloud_json(
        ctx, "storage", "buckets", "get-iam-policy", f"gs://{bucket}", timeout=timeout
    )
    if not isinstance(policy, dict):
        raise RwError("internal", f"gcloud returned no IAM policy for {bucket}")
    return policy


def bucket_access(ctx, bucket, timeout):
    """The `access` entry of one bucket. The bucket resource is the primary read (it fails the
    bucket when it fails). The IAM policy is a second read that may be forbidden on its own: then
    `publicViaIam` is null and the error is scoped to the bucket (partial)."""
    row = describe(ctx, bucket, timeout)
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
    # ACLs do not apply to a bucket with uniform bucket-level access: IAM is the only path
    acl = [] if uniform else public_acl_entries(row)
    via_iam = None if bindings is None else bool(bindings)
    via_acl = bool(acl)
    grant = True if (via_iam or via_acl) else (None if via_iam is None else False)
    return {
        "name": bucket,
        "publicGrant": grant,
        "publicViaIam": via_iam,
        "publicViaAcl": via_acl,
        "uniformBucketLevelAccess": uniform,
        "publicAccessPrevention": prevention,
        "blockedByPrevention": bool(grant) and prevention == "enforced",
        "iamBindings": (bindings or [])[:NESTED],
        "aclEntries": acl[:NESTED],
    }


def totals(entries, threshold):
    """Counts over the buckets examined: `publicBuckets` have a public grant, `unknown` could not
    be decided (IAM policy unreadable and no ACL grant), `overThreshold` is publicBuckets above
    the setting."""
    public = sum(1 for e in entries if e["publicGrant"] is True)
    return {
        "examined": len(entries),
        "publicBuckets": public,
        "unknown": sum(1 for e in entries if e["publicGrant"] is None),
        "threshold": threshold,
        "overThreshold": public > threshold,
    }


def run(ctx, st):
    project, names, total, single = select(ctx, st.max_items, st.command_timeout)
    entries = per_bucket(
        ctx, names, single, lambda name: bucket_access(ctx, name, st.command_timeout)
    )
    return {
        "access": listing(entries, total, len(names)),
        "totals": totals(entries, st.public_bucket_threshold),
    }
