"""Azure Monitor metrics through `az monitor metrics list`: one metric over a window, reduced to
facts. A grain with no datapoint is counted, never read as healthy or as zero."""

from .az import az_json
from .common import NESTED_LIMIT, span
from .errors import RwError


def read_series(ctx, subscription, resource_id, metric, aggregations, interval, window_seconds):
    """The datapoints of `metric` over the window that ends at ctx.now(): a list of
    {timeStamp, <aggregation>: value} dicts as Azure returns them. A metric Azure reports an
    error for raises; a resource with no data at all gives an empty list."""
    start, end = span(ctx.now(), window_seconds)
    reply = az_json(
        ctx,
        [
            "monitor", "metrics", "list",
            "--resource", resource_id,
            "--metrics", metric,
            "--aggregation", *aggregations,
            "--interval", interval,
            "--start-time", start,
            "--end-time", end,
            "--subscription", subscription,
        ],
    )  # fmt: skip
    values = reply.get("value") if isinstance(reply, dict) else None
    if not isinstance(values, list) or not values:
        raise RwError("internal", f"az monitor metrics list returned no value for {metric}")
    entry = values[0]
    if entry.get("errorCode") not in (None, "Success"):
        raise RwError("internal", f"Azure Monitor reported {entry['errorCode']} for {metric}")
    series = entry.get("timeseries") or []
    return list((series[0].get("data") if series else None) or [])


def summarize(data, key, *, extra=None, threshold=None, above=True):
    """Facts about one aggregation (`key`: average, count, maximum...) of a series.

    points: the newest NESTED_LIMIT datapoints that have a value, oldest first, each
    {timestamp, value} plus the `extra` aggregations (a dict of output name -> key) when present.
    grains: how many intervals the window holds; withData: those with a value; missing: those
    without. min/max/last are over every datapoint with a value, not only the points returned, and
    are null when there is none. With a `threshold`, crossings counts the datapoints beyond it
    (above it when `above`, below it otherwise) and crossed says whether there was any."""
    have = [d for d in data if isinstance(d.get(key), (int, float))]
    values = [d[key] for d in have]
    shown = have[-NESTED_LIMIT:]
    points = []
    for d in shown:
        point = {"timestamp": d["timeStamp"], "value": d[key]}
        for name, other in (extra or {}).items():
            if isinstance(d.get(other), (int, float)):
                point[name] = d[other]
        points.append(point)
    out = {
        "grains": len(data),
        "withData": len(have),
        "missing": len(data) - len(have),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "last": values[-1] if values else None,
        "points": points,
        "pointsOmitted": len(have) - len(shown),
    }
    if threshold is not None:
        crossings = sum(1 for v in values if (v > threshold if above else v < threshold))
        out["threshold"] = threshold
        out["crossings"] = crossings
        out["crossed"] = crossings > 0
    return out
