"""function-metrics: the Lambda CloudWatch metrics of one function over a look-back window.

Four get-metric-statistics calls (AWS/Lambda Invocations, Errors, Throttles, Duration) over the
same window. One call that fails (throttled, forbidden) leaves its metrics null and is reported by
the caller as an error scoped to the metric; the others stand. A metric with no datapoints is not
zero: Lambda publishes a metric only while the function is invoked, so `datapoints` is reported
beside every value."""

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


def read_metric(ctx, region, function, metric, statistics, start, end, period):
    """The datapoints of one metric: a list of dicts as CloudWatch returns them."""
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
            f"Name=FunctionName,Value={function}",
            "--start-time",
            iso(start),
            "--end-time",
            iso(end),
            "--period",
            str(period),
            "--statistics",
            *statistics,
        ],
        region=region,
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


def summarize(points_by_metric, start, end, period, timeout_seconds, st):
    """The `metrics` output from the datapoints of each metric (None: that call failed).

    The booleans are the threshold crossings the settings define: errorRateAboveThreshold,
    throttlesAboveThreshold, durationNearTimeout. A boolean is null when the numbers it needs
    were not read."""
    inv_points = points_by_metric.get("Invocations")
    err_points = points_by_metric.get("Errors")
    thr_points = points_by_metric.get("Throttles")
    dur_points = points_by_metric.get("Duration")
    invocations = None if inv_points is None else total(inv_points)
    errors = None if err_points is None else total(err_points)
    throttles = None if thr_points is None else total(thr_points)
    duration = None if dur_points is None else duration_facts(dur_points)
    rate = None
    if invocations and errors is not None:
        rate = round(errors / invocations, 4)
    near_timeout = None
    if duration is not None and duration["maxMs"] is not None and timeout_seconds:
        near_timeout = duration["maxMs"] >= st.duration_timeout_ratio * timeout_seconds * 1000
    elif duration is not None and duration["maxMs"] is None:
        near_timeout = False
    return {
        "window": {
            "start": iso(start),
            "end": iso(end),
            "seconds": int((end - start).total_seconds()),
        },
        "periodSeconds": period,
        "invocations": invocations,
        "errors": errors,
        "throttles": throttles,
        "errorRate": rate,
        "duration": duration,
        "timeoutSeconds": timeout_seconds,
        "datapoints": {
            name: (None if pts is None else len(pts)) for name, pts in points_by_metric.items()
        },
        "errorRateAboveThreshold": (
            None
            if errors is None or invocations is None
            else (rate is not None and rate >= st.error_rate_threshold)
        ),
        "throttlesAboveThreshold": None
        if throttles is None
        else throttles >= st.throttle_threshold,
        "durationNearTimeout": near_timeout,
    }
