"""bucket-size: how many bytes each bucket holds, summed over its objects by `gcloud storage du`.

`du` walks the bucket's objects: it is exact and slow on a large bucket, so it has its own timeout
(`sizeTimeout`) and the task stops starting new buckets when its time budget is nearly used up."""

import re
import time

from .buckets import listing, per_bucket, select
from .errors import RwError
from .gcloud import gcloud_text

TIB = 1024**4
TASK_BUDGET = 900  # seconds: bucket-size's `timeout` in tasks.yaml (a test keeps them equal)
RESERVE = 10.0  # seconds kept free after the last bucket for the result
LINE = re.compile(r"^\s*(\d+)\s+(gs://\S+?)/?\s*$")


def parse_du(text, bucket):
    """The byte total `gcloud storage du --summarize gs://<bucket>` printed: the line
    `<bytes>  gs://<bucket>[/]`. No such line is an error, never zero bytes."""
    for line in text.splitlines():
        match = LINE.match(line)
        if match and match.group(2) == f"gs://{bucket}":
            return int(match.group(1))
    raise RwError("internal", f"gcloud storage du printed no total for gs://{bucket}")


def bucket_size(ctx, bucket, timeout, threshold_tib):
    text = gcloud_text(ctx, "storage", "du", "--summarize", f"gs://{bucket}", timeout=timeout)
    size = parse_du(text, bucket)
    return {
        "name": bucket,
        "sizeBytes": size,
        "sizeTiB": round(size / TIB, 4),
        "overThreshold": size / TIB > threshold_tib,
    }


def totals(entries, threshold_tib, not_measured):
    return {
        "examined": len(entries),
        "notMeasured": not_measured,
        "totalBytes": sum(e["sizeBytes"] for e in entries),
        "thresholdTiB": threshold_tib,
        "overThreshold": sum(1 for e in entries if e["overThreshold"]),
    }


def deadline_from(started):
    """The monotonic time by which the last `du` must end, for a task that started at `started`."""
    return started + TASK_BUDGET - RESERVE


def run(ctx, st, deadline, clock=time.monotonic):
    """`deadline` is the monotonic time by which the last `du` must end."""
    project, names, total, single = select(ctx, st.max_items, st.command_timeout)
    skipped = []

    def work(name):
        remaining = deadline - clock()
        if remaining < 5:
            skipped.append(name)
            return None
        return bucket_size(ctx, name, min(st.size_timeout, remaining), st.usage_threshold_tib)

    results = per_bucket(ctx, names, single, work)
    entries = sorted(
        (e for e in results if e is not None), key=lambda e: (-e["sizeBytes"], e["name"])
    )
    if skipped:
        ctx.error(
            "timeout",
            f"{len(skipped)} buckets were not measured: the task's time budget ran out",
            scope="budget",
            details={"notMeasured": skipped[:50]},
        )
    return {
        "sizes": listing(entries, total, len(names)),
        "totals": totals(entries, st.usage_threshold_tib, len(skipped)),
    }
