"""Which buckets a task looks at, and how a per-bucket failure is handled.

A `gcp/bucket` target is that bucket. A `gcp/project` target is the project's buckets, listed with
`gcloud storage buckets list`, the first `maxItems` of them in the order gcloud returns (by name).
A list that fails raises: with no list there is nothing to report."""

from .errors import RwError
from .gcloud import gcloud_json, target_path


def listing(entries, total, looked_at):
    """The `{items, total, omitted}` shape of a bucket list output: `total` buckets exist,
    `looked_at` of them were examined (the first maxItems), so `omitted` were left out by
    maxItems. A bucket whose examination failed has an error scoped to its name and no entry."""
    return {"items": entries, "total": total, "omitted": total - looked_at}


def select(ctx, max_items, timeout):
    """(project, bucket names to look at, total buckets, whether the target is one bucket)."""
    project, bucket = target_path(ctx.resource)
    if bucket is not None:
        return project, [bucket], 1, True
    rows = gcloud_json(
        ctx, "storage", "buckets", "list", f"--project={project}", "--raw", timeout=timeout
    )
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("name") for r in rows):
        raise RwError("internal", "gcloud returned no bucket list")
    names = sorted(r["name"] for r in rows)
    return project, names[:max_items], len(names), False


def describe(ctx, bucket, timeout):
    """The bucket's own resource, as the API reports it (`gcloud storage buckets describe --raw`)."""
    row = gcloud_json(
        ctx, "storage", "buckets", "describe", f"gs://{bucket}", "--raw", timeout=timeout
    )
    if not isinstance(row, dict):
        raise RwError("internal", f"gcloud returned no bucket resource for {bucket}")
    return row


def per_bucket(ctx, names, single, work):
    """`work(name)` for each bucket, collecting what it returns. A failure on the only bucket
    raises. On a project a failure becomes `ctx.error(code, message, scope=<bucket>)` and the
    rest go on, unless every bucket failed, which raises the first failure: nothing to report."""
    results, first = [], None
    for name in names:
        try:
            results.append(work(name))
        except RwError as exc:
            if single:
                raise
            first = first or exc
            ctx.error(exc.code, f"{name}: {exc.message}", scope=name)
    if names and not results:
        raise first
    return results
