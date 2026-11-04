"""bucket-size: how many bytes each bucket holds.

The size comes from Cloud Monitoring (`monitoring.py`): one query per project, whatever the number
of buckets, summed over the object types of each bucket. `gcloud storage du` walks every object, so
it scales with the object count and is no primary method; it is the fallback for a single-bucket
target only, when Monitoring has no series for that bucket (a new or empty bucket, which the
metric does not track), and then has its own timeout (`sizeTimeout`)."""

import re
import time

from .buckets import listing, select, totals_of
from .errors import RwError
from .gcloud import gcloud_text
from .monitoring import access_token, fetch, sizes_by_bucket

TIB = 1024**4
TASK_BUDGET = 600  # seconds: bucket-size's `timeout` in tasks.yaml (a test keeps them equal)
RESERVE = 10.0  # seconds kept free after the last call for the result
MIN_REMAINING = 5.0  # seconds a call needs to be worth starting
MAX_NAMES = 50  # buckets named in one error's details
# `gcloud storage du --summarize` prints '{size:<13}{url}': the size padded to 13 characters, so a
# size of 13 digits (1 TB and up) runs straight into `gs://`. Buckets print without a trailing `/`.
LINE = re.compile(r"^\s*(\d+)\s*(gs://\S+?)/?\s*$")


def parse_du(text, bucket):
    """The byte total `gcloud storage du --summarize gs://<bucket>` printed: the line
    `<bytes><padding>gs://<bucket>`. No such line is an error, never zero bytes."""
    for line in text.splitlines():
        match = LINE.match(line)
        if match and match.group(2) == f"gs://{bucket}":
            return int(match.group(1))
    raise RwError("internal", f"gcloud storage du printed no total for gs://{bucket}")


def entry(name, size, source, by_type, threshold_tib):
    return {
        "name": name,
        "sizeBytes": size,
        "sizeTiB": round(size / TIB, 4),
        "overThreshold": size / TIB > threshold_tib,
        "source": source,
        "byType": by_type,
    }


def du_entry(ctx, bucket, timeout, threshold_tib):
    """The fallback: `du` of one bucket (live objects only)."""
    text = gcloud_text(ctx, "storage", "du", "--summarize", f"gs://{bucket}", timeout=timeout)
    size = parse_du(text, bucket)
    return entry(bucket, size, "du", {"live-object": size}, threshold_tib)


def deadline_from(started):
    """The monotonic time by which the last call must end, for a task that started at `started`."""
    return started + TASK_BUDGET - RESERVE


def totals(entries, total, looked_at, threshold_tib, not_measured):
    out = totals_of(entries, total, looked_at, undecided=not_measured)
    out.update(
        notMeasured=not_measured,
        totalBytes=sum(e["sizeBytes"] for e in entries),
        thresholdTiB=threshold_tib,
        overThreshold=sum(1 for e in entries if e["overThreshold"]),
    )
    return out


def run(ctx, st, deadline, clock=time.monotonic):
    """`deadline` is the monotonic time by which the last call must end."""
    project, rows, total, single = select(ctx, st.max_items, st.command_timeout)
    names = [r["name"] for r in rows]
    entries, missing = [], []
    if names:
        token = access_token(ctx, st.command_timeout)
        series = fetch(
            ctx, token, project, names[0] if single else None, st.command_timeout, deadline, clock
        )
        sizes = sizes_by_bucket(series, project)
        for name in names:
            if name in sizes:
                by_type = sizes[name]
                entries.append(
                    entry(
                        name, sum(by_type.values()), "monitoring", by_type, st.usage_threshold_tib
                    )
                )
            else:
                missing.append(name)
        if single and missing:
            remaining = deadline - clock()
            if remaining < MIN_REMAINING:
                raise RwError("timeout", "not started: the task's time budget ran out")
            entries.append(
                du_entry(ctx, missing[0], min(st.size_timeout, remaining), st.usage_threshold_tib)
            )
            missing = []
        if missing:
            ctx.error(
                "not_found",
                f"{len(missing)} buckets have no Cloud Monitoring series for "
                "storage/v2/total_bytes (a new or empty bucket, or a size not reported yet): "
                "their size is unknown, not zero",
                scope="monitoring",
                details={"count": len(missing), "buckets": missing[:MAX_NAMES]},
            )
    entries.sort(key=lambda e: (-e["sizeBytes"], e["name"]))
    return {
        "sizes": listing(entries, total, len(names)),
        "totals": totals(entries, total, len(names), st.usage_threshold_tib, len(missing)),
    }
