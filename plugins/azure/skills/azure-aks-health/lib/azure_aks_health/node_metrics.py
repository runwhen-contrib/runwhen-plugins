"""node-metrics: the nodes' CPU, memory and disk use over a window, from Azure Monitor's platform
metrics for the cluster (node_cpu_usage_percentage, node_memory_working_set_percentage,
node_disk_usage_percentage), averaged and at the busiest node, per interval.

One metric that cannot be read is reported with its own error and the others stand. An interval with
no datapoint is counted, never read as healthy or as zero (a stopped cluster reports none)."""

from .cluster import resolve
from .common import window
from .errors import RwError
from .metrics import read_series, summarize

SERIES = (  # (output field, metric, the threshold's name on the thresholds object)
    ("cpu", "node_cpu_usage_percentage", "cpu_percent"),
    ("memory", "node_memory_working_set_percentage", "memory_percent"),
    ("disk", "node_disk_usage_percentage", "disk_percent"),
)


def series(data, metric, threshold):
    """Facts about one metric: `summarize` over the average, with each interval's maximum (the
    busiest node) beside it, and the threshold applied to both."""
    avg = summarize(data, "average", extra={"maximum": "maximum"}, threshold=threshold, above=True)
    peaks = [d["maximum"] for d in data if isinstance(d.get("maximum"), (int, float))]
    over_peak = sum(1 for v in peaks if v > threshold)
    return {
        "metric": metric,
        "hasData": avg["withData"] > 0,
        "thresholdPercent": threshold,
        "grains": avg["grains"],
        "grainsWithData": avg["withData"],
        "grainsMissing": avg["missing"],
        "averageMin": avg["min"],
        "averageMax": avg["max"],
        "averageLast": avg["last"],
        "lastGrainMissing": avg["lastGrainMissing"],
        "peakMax": max(peaks) if peaks else None,
        "grainsAverageOverThreshold": avg["crossings"],
        "averageOverThreshold": avg["crossed"],
        "grainsPeakOverThreshold": over_peak,
        "peakOverThreshold": over_peak > 0,
        "points": avg["points"],
        "pointsOmitted": avg["pointsOmitted"],
    }


def read(ctx, window_input, st, th):
    seconds = window(window_input)
    subscription, _, name, resource_id, _ = resolve(ctx)
    out = {
        "cluster": name,
        "windowSeconds": int(seconds),
        "interval": st.metric_interval,
        **{field: None for field, _, _ in SERIES},
    }
    failed = []
    for field, metric, threshold in SERIES:
        try:
            data = read_series(
                ctx,
                subscription,
                resource_id,
                metric,
                ["Average", "Maximum"],
                st.metric_interval,
                seconds,
            )
            out[field] = series(data, metric, getattr(th, threshold))
        except RwError as exc:
            failed.append((field, exc))
    if len(failed) == len(SERIES):
        raise failed[0][1]
    for field, exc in failed:
        ctx.error(exc.code, exc.message, scope=field)
    return out
