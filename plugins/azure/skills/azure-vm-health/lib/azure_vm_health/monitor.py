"""metrics: CPU, available memory, disk and network of a virtual machine or scale set from Azure
Monitor (`az monitor metrics list`), one metric per call, each reduced to facts.

The platform metrics come from the hypervisor and the monitoring pipeline, never from the guest
operating system: CPU, disk and network are always published, `Available Memory Bytes` only where
Azure has a memory reading for the resource. A metric with no datapoint is `hasData: false`, an
interval without a value is counted, and neither is read as healthy or as zero."""

import re
import time

from .common import window
from .errors import RwError
from .metrics import read_series, summarize
from .target import resolve

# Azure's 400 for a metric it has no configuration for on this resource (memory on an image Azure
# has no reading for): that is a metric which is not there, not a failed call
NO_SUCH_METRIC = re.compile(r"Failed to find metric configuration|metric .* is not supported", re.I)
BUDGET = 240  # seconds in which a new metric call may still start (the task timeout is 360s)
MB = 1024 * 1024
# field -> (metric, unit, aggregations to ask for, the aggregation that is reported, the others
# reported beside it, threshold kind). The memory floor applies to the lowest reading of an
# interval (the classic bundle's rule); CPU to the highest.
METRICS = (
    ("cpu", "Percentage CPU", "Percent", ["Average", "Maximum"], "maximum", {"average": "average"}, "cpu"),
    ("availableMemory", "Available Memory Bytes", "Bytes", ["Average", "Minimum"], "minimum", {"average": "average"}, "memory"),
    ("diskRead", "Disk Read Bytes", "Bytes", ["Total"], "total", None, None),
    ("diskWrite", "Disk Write Bytes", "Bytes", ["Total"], "total", None, None),
    ("networkIn", "Network In Total", "Bytes", ["Total"], "total", None, None),
    ("networkOut", "Network Out Total", "Bytes", ["Total"], "total", None, None),
)  # fmt: skip


def series_facts(ctx, subscription, resource_id, spec, th, interval, seconds):
    field, metric, unit, aggregations, key, extra, rule = spec
    unavailable = False
    try:
        data = read_series(ctx, subscription, resource_id, metric, aggregations, interval, seconds)
    except RwError as exc:
        if not NO_SUCH_METRIC.search(exc.message):
            raise
        data, unavailable = [], True  # no data, said so: never a healthy reading
    if rule == "cpu":
        out = summarize(data, key, extra=extra, threshold=th.cpu_threshold_percent, above=True)
    elif rule == "memory":
        out = summarize(
            data, key, extra=extra, threshold=th.available_memory_floor_mb * MB, above=False
        )
    else:
        out = summarize(data, key, extra=extra)
    out["metric"] = metric
    out["unit"] = unit
    out["aggregation"] = key
    out["hasData"] = out["withData"] > 0
    out["unavailable"] = unavailable
    return out


def read(ctx, window_input, st, th, *, clock=time.monotonic, budget=BUDGET):
    seconds = window(window_input)
    kind, subscription, group, name, resource_id, _ = resolve(ctx)
    started = clock()
    out = {
        "resource": name,
        "resourceType": kind,
        "windowSeconds": int(seconds),
        "interval": st.metric_interval,
        "cpuThresholdPercent": th.cpu_threshold_percent,
        "availableMemoryFloorBytes": int(th.available_memory_floor_mb * MB),
    }
    failed = []
    for spec in METRICS:
        field = spec[0]
        out[field] = None
        if clock() - started >= budget:
            failed.append(
                (
                    field,
                    RwError("timeout", f"the {budget}s budget for metrics ran out before {field}"),
                )
            )
            continue
        try:
            out[field] = series_facts(
                ctx, subscription, resource_id, spec, th, st.metric_interval, seconds
            )
        except RwError as exc:
            failed.append((field, exc))
    if len(failed) == len(METRICS):
        raise failed[0][1]
    for field, exc in failed:
        ctx.error(exc.code, exc.message, scope=field)
    return out
