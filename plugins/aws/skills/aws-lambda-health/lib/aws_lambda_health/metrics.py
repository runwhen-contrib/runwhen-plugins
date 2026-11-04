"""function-metrics: the Lambda CloudWatch metrics of one function over a look-back window.

Five get-metric-statistics calls (AWS/Lambda Invocations, Errors, Throttles, Duration,
ConcurrentExecutions) over the same window. One call that fails (throttled, forbidden) leaves its
metrics null and is reported by the caller as an error scoped to the metric; the others stand.

No data is not health. Lambda publishes a metric only while the function is invoked, so a window
with no Invocations datapoints says nothing about errors: `invocations` is 0 with `hasData: false`
and `idle: true`, and errors, throttles, the error rate and every threshold boolean are null (not
0, not false). Only when Invocations has datapoints does a metric with none of its own (Errors,
Throttles) count as 0; a metric that has datapoints of its own is always reported (a function
throttled completely has Throttles datapoints and no Invocations, because throttled calls are not
counted as invocations). The dimension is the function's plain name: CloudWatch publishes AWS/Lambda
metrics under `FunctionName=<name>`, an ARN there matches nothing and would read as no data; the
function's existence in the target account is checked first through its ARN (config.read_config).

Periods: 300 s up to a 3-day window, 3600 s above (GetMetricStatistics returns at most 1440
datapoints). The window is `[now - since, now]` and is not aligned to the period, so the first
datapoint can cover less than a full period (a partial first bucket) and the last is still
filling: the totals are of the window, a single datapoint is not a full period of load."""

from datetime import timedelta, timezone

from .awscli import aws_json
from .errors import RwError

NAMESPACE = "AWS/Lambda"
# metric -> statistics asked for; SampleCount of Duration weights its per-period averages
METRICS = (
    ("Invocations", ("Sum",)),
    ("Errors", ("Sum",)),
    ("Throttles", ("Sum",)),
    ("Duration", ("Average", "Maximum", "SampleCount")),
    ("ConcurrentExecutions", ("Maximum",)),
)
MAX_FIVE_MINUTE_SECONDS = 3 * 86400  # up to here 5-minute periods (864 datapoints at most)


def window(now, since_seconds):
    """(start, end) datetimes, UTC, `since_seconds` before `now`; `now` is ctx.now()."""
    end = now.astimezone(timezone.utc).replace(microsecond=0)
    return end - timedelta(seconds=since_seconds), end


def period_for(since_seconds):
    """The period in seconds: a multiple of 60, small enough for GetMetricStatistics' 1440
    datapoint limit."""
    return 300 if since_seconds <= MAX_FIVE_MINUTE_SECONDS else 3600


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_metric(ctx, target, metric, statistics, start, end, period):
    """The datapoints of one metric of the function `target`: a list of dicts as CloudWatch
    returns them. The dimension is the function name (see the module docstring)."""
    doc = aws_json(
        ctx,
        [
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            NAMESPACE,
            "--metric-name",
            metric,
            "--dimensions",
            f"Name=FunctionName,Value={target.name}",
            "--start-time",
            iso(start),
            "--end-time",
            iso(end),
            "--period",
            str(period),
            "--statistics",
            *statistics,
        ],
        region=target.region,
    )
    points = doc.get("Datapoints") if isinstance(doc, dict) else None
    if not isinstance(points, list):
        # a missing key is not "no datapoints": CloudWatch answers [] for that
        raise RwError(
            "internal", f"cloudwatch get-metric-statistics {metric} returned no Datapoints list"
        )
    return [p for p in points if isinstance(p, dict)]


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def total(points, stat="Sum"):
    return _number(sum(p.get(stat) or 0 for p in points))


def duration_facts(points):
    """{averageMs, maxMs, sampleCount} of the Duration datapoints; all null with no datapoints.
    The average is weighted by each datapoint's SampleCount."""
    if not points:
        return {"averageMs": None, "maxMs": None, "sampleCount": 0}
    count = sum(p.get("SampleCount") or 0 for p in points)
    weighted = sum((p.get("Average") or 0) * (p.get("SampleCount") or 0) for p in points)
    return {
        "averageMs": round(weighted / count, 2) if count else None,
        "maxMs": round(max(p.get("Maximum") or 0 for p in points), 2),
        "sampleCount": _number(count),
    }


def count_of(points, has_data):
    """The sum of a count metric: None when its call failed; its own total when it has
    datapoints; otherwise 0 only when Invocations has datapoints (the function ran and the metric
    was not published, which is 0) and None when it does not (nothing is known)."""
    if points is None:
        return None
    if points:
        return total(points)
    return 0 if has_data else None


def summarize(points_by_metric, start, end, period, timeout_seconds, st):
    """The `metrics` output from the datapoints of each metric (None: that call failed).

    The booleans are the threshold crossings the settings define: errorRateAboveThreshold
    (rate > errorRateThreshold), throttlesAboveThreshold (throttles >= throttleThreshold) and
    durationNearTimeout. A boolean is null when the numbers it needs were not read or there is
    nothing to compare (no invocations), never false by default."""
    inv_points = points_by_metric.get("Invocations")
    has_data = None if inv_points is None else bool(inv_points)
    invocations = None if inv_points is None else total(inv_points)
    errors = count_of(points_by_metric.get("Errors"), has_data)
    throttles = count_of(points_by_metric.get("Throttles"), has_data)
    dur_points = points_by_metric.get("Duration")
    duration = None if dur_points is None else duration_facts(dur_points)
    conc_points = points_by_metric.get("ConcurrentExecutions")
    concurrent_max = (
        round(max(p.get("Maximum") or 0 for p in conc_points), 2) if conc_points else None
    )
    rate = None
    if invocations and errors is not None:
        rate = round(errors / invocations, 4)
    near_timeout = None
    if duration is not None and duration["maxMs"] is not None and timeout_seconds:
        near_timeout = duration["maxMs"] >= st.duration_timeout_ratio * timeout_seconds * 1000
    return {
        "window": {
            "start": iso(start),
            "end": iso(end),
            "seconds": int((end - start).total_seconds()),
        },
        "periodSeconds": period,
        "hasData": has_data,
        "idle": None if invocations is None else invocations == 0,
        "invocations": invocations,
        "errors": errors,
        "throttles": throttles,
        "errorRate": rate,
        "duration": duration,
        "concurrentExecutionsMax": _number(concurrent_max) if concurrent_max is not None else None,
        "timeoutSeconds": timeout_seconds,
        "datapoints": {
            name: (None if pts is None else len(pts)) for name, pts in points_by_metric.items()
        },
        "errorRateAboveThreshold": None if rate is None else rate > st.error_rate_threshold,
        "throttlesAboveThreshold": None
        if throttles is None
        else throttles >= st.throttle_threshold,
        "durationNearTimeout": near_timeout,
    }
