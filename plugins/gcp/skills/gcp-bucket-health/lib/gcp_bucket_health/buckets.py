"""Which buckets a task looks at, and how a per-bucket failure is handled.

A `gcp/bucket` target is that bucket: one `gcloud storage buckets describe --raw`. A `gcp/project`
target is the project's buckets: one `gcloud storage buckets list --raw`, whose rows are the same
full bucket resources `describe` returns (the list asks for the FULL projection, so each row holds
iamConfiguration, acl and defaultObjectAcl), the first `maxItems` of them by name. A task that
needs only those fields makes no further call per bucket. A list that fails raises: with no list
there is nothing to report."""

import time

from .errors import RwError
from .gcloud import gcloud_json, target_path

MAX_ERRORS = 50  # per-bucket errors reported one by one; the rest are counted in one error
MIN_REMAINING = 5.0  # seconds a per-bucket call needs to be worth starting


def listing(entries, total, looked_at):
    """The `{items, total, omitted}` shape of a bucket list output: `total` buckets exist,
    `looked_at` of them were examined (the first maxItems), so `omitted` were left out by
    maxItems. A bucket whose examination failed has an error scoped to its name and no entry."""
    return {"items": entries, "total": total, "omitted": total - looked_at}


def totals_of(entries, total, looked_at, *, undecided=0):
    """The counts every task's `totals` output starts with: `examined` entries, `total` buckets
    the target covers, `omitted` by maxItems, and `complete`: every bucket of the target has an
    entry and none of them is undecided."""
    return {
        "examined": len(entries),
        "total": total,
        "omitted": total - looked_at,
        "complete": total == looked_at == len(entries) and not undecided,
    }


def describe(ctx, bucket, timeout):
    """The bucket's own resource, as the API reports it (`gcloud storage buckets describe --raw`)."""
    row = gcloud_json(
        ctx, "storage", "buckets", "describe", f"gs://{bucket}", "--raw", timeout=timeout
    )
    if not isinstance(row, dict) or not row.get("name"):
        raise RwError("internal", f"gcloud returned no bucket resource for {bucket}")
    return row


def select(ctx, max_items, timeout):
    """(project, bucket resources to look at, total buckets, whether the target is one bucket)."""
    project, bucket = target_path(ctx.resource)
    if bucket is not None:
        return project, [describe(ctx, bucket, timeout)], 1, True
    rows = gcloud_json(
        ctx, "storage", "buckets", "list", f"--project={project}", "--raw", timeout=timeout
    )
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("name") for r in rows):
        raise RwError("internal", "gcloud returned no bucket list")
    rows = sorted(rows, key=lambda r: r["name"])
    return project, rows[:max_items], len(rows), False


def report_failures(ctx, failures):
    """`ctx.error` for each `(bucket, RwError)`: the first MAX_ERRORS scoped to their bucket, the
    rest in one error scoped `buckets` that carries their count and codes."""
    for name, exc in failures[:MAX_ERRORS]:
        ctx.error(exc.code, f"{name}: {exc.message}", scope=name)
    rest = failures[MAX_ERRORS:]
    if rest:
        codes = {}
        for _, exc in rest:
            codes[exc.code] = codes.get(exc.code, 0) + 1
        common = max(sorted(codes), key=codes.get)
        ctx.error(
            common,
            f"{len(rest)} more buckets failed (only the first {MAX_ERRORS} are listed one by one)",
            scope="buckets",
            details={"count": len(rest), "codes": codes},
        )


def per_bucket(ctx, rows, single, work, deadline=None, clock=time.monotonic):
    """`work(row)` for each bucket resource, collecting what it returns. A failure on the only
    bucket raises. On a project a failure becomes an error scoped to the bucket (`report_failures`)
    and the rest go on, unless every bucket failed, which raises the first failure: nothing to
    report. With a `deadline` (a monotonic time), a bucket that would start with under
    MIN_REMAINING seconds left is not started: it fails with `timeout` and the result is partial.
    No threads: the order of the calls, and so a replay, is the same every run."""
    results, failures = [], []
    for row in rows:
        name = row["name"]
        try:
            if deadline is not None and deadline - clock() < MIN_REMAINING:
                raise RwError("timeout", "not started: the task's time budget ran out")
            results.append(work(row))
        except RwError as exc:
            if single:
                raise
            failures.append((name, exc))
    report_failures(ctx, failures)
    if rows and not results:
        raise failures[0][1]
    return results
