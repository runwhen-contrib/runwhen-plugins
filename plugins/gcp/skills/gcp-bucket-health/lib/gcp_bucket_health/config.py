"""bucket-config: a bucket's settings as facts, from the API's own bucket resource (the rows of
one `buckets list`, or one `buckets describe`: no call per bucket)."""

from .buckets import listing, per_bucket, select, totals_of

TEXT = 1024
MAX_LABELS = 50


def clip(value):
    """A string cut to 1024 characters, or None when it is not a string."""
    if not isinstance(value, str):
        return None
    return value if len(value) <= TEXT else value[: TEXT - 3] + "..."


def int_or_none(value):
    """The API prints int64 fields as strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def labels(row):
    raw = row.get("labels")
    if not isinstance(raw, dict):
        return {}, False
    kept = sorted((k, v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str))
    return {clip(k): clip(v) for k, v in kept[:MAX_LABELS]}, len(kept) > MAX_LABELS


def bucket_config(row):
    """The `buckets` output entry for one bucket resource."""
    iam = row.get("iamConfiguration") or {}
    ubla = (iam.get("uniformBucketLevelAccess") or iam.get("bucketPolicyOnly") or {}).get("enabled")
    retention = row.get("retentionPolicy")
    soft = (row.get("softDeletePolicy") or {}).get("retentionDurationSeconds")
    key = (row.get("encryption") or {}).get("defaultKmsKeyName")
    shown, cut = labels(row)
    return {
        "name": row["name"],
        "location": clip(row.get("location")),
        "locationType": clip(row.get("locationType")),
        "storageClass": clip(row.get("storageClass")),
        "created": clip(row.get("timeCreated")),
        "updated": clip(row.get("updated")),
        "versioning": bool((row.get("versioning") or {}).get("enabled")),
        "uniformBucketLevelAccess": bool(ubla),
        "publicAccessPrevention": clip(iam.get("publicAccessPrevention")),
        "retention": (
            {
                "periodSeconds": int_or_none(retention.get("retentionPeriod")),
                "locked": bool(retention.get("isLocked")),
            }
            if isinstance(retention, dict)
            else None
        ),
        "lifecycleRuleCount": len((row.get("lifecycle") or {}).get("rule") or []),
        "softDeleteSeconds": int_or_none(soft),
        "cmek": bool(key),
        "kmsKey": clip(key),
        "labels": shown,
        "labelsTruncated": cut,
    }


def run(ctx, st):
    project, rows, total, single = select(ctx, st.max_items, st.command_timeout)
    entries = per_bucket(ctx, rows, single, bucket_config)
    return {
        "buckets": listing(entries, total, len(rows)),
        "totals": totals_of(entries, total, len(rows)),
    }
