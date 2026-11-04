"""function-metrics: execution count, errors and latency of one function over a window.

Four Cloud Monitoring queries over the same window, each aggregated by Monitoring itself
(`ALIGN_DELTA` over the whole window, so one point per series, then a cross-series reducer):

* executions by outcome: REDUCE_SUM grouped by the outcome label;
* latency percentiles 50, 95 and 99: REDUCE_PERCENTILE_x, which merges the distributions of all
  series before taking the percentile.

The metrics differ by generation, because a 2nd gen function is a Cloud Run service:

| | 1st gen | 2nd gen |
| --- | --- | --- |
| resource | `cloud_function` (function_name, region) | `cloud_run_revision` (service_name, location) |
| count | `cloudfunctions.googleapis.com/function/execution_count`, label `status` | `run.googleapis.com/request_count`, label `response_code_class` |
| error | a status other than `ok` | class `5xx` |
| latency | `.../function/execution_times` (nanoseconds) | `run.googleapis.com/request_latencies` (milliseconds) |

One query that fails (throttled, forbidden) leaves its numbers null and is reported by the caller as
an error scoped to the query; the others stand. No data is not health: a function that ran nothing
in the window publishes no series, so `executions` is 0 with `hasData: false` and errors, the error
rate and the threshold booleans are null (not 0, not false)."""

from datetime import timedelta, timezone

from .errors import RwError

PERCENTILES = (50, 95, 99)
MAX_OUTCOMES = 50
TASK_BUDGET = 120  # seconds: function-metrics' `timeout` in tasks.yaml (a test keeps them equal)
RESERVE = 10.0  # seconds kept free after the last call for the result
GEN1 = {
    "resource": 'resource.type="cloud_function" AND resource.labels.function_name="{name}"'
    ' AND resource.labels.region="{region}"',
    "count": "cloudfunctions.googleapis.com/function/execution_count",
    "label": "metric.label.status",
    "key": "status",
    "latency": "cloudfunctions.googleapis.com/function/execution_times",
    "to_ms": 1e-6,
}
GEN2 = {
    "resource": 'resource.type="cloud_run_revision" AND resource.labels.service_name="{name}"'
    ' AND resource.labels.location="{region}"',
    "count": "run.googleapis.com/request_count",
    "label": "metric.label.response_code_class",
    "key": "response_code_class",
    "latency": "run.googleapis.com/request_latencies",
    "to_ms": 1.0,
}


def spec_for(generation):
    if generation == 2:
        return GEN2
    if generation == 1:
        return GEN1
    raise RwError("internal", "the function's generation is unknown: its metrics cannot be chosen")


def window(now, since_seconds):
    """(start, end) datetimes, UTC; `end` is the run's reference time to the second."""
    end = now.astimezone(timezone.utc).replace(microsecond=0)
    return end - timedelta(seconds=since_seconds), end


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def queries(spec, seconds):
    """[(query name, metric, aggregation pairs)] in the order they are asked."""
    base = [("alignmentPeriod", f"{seconds}s"), ("perSeriesAligner", "ALIGN_DELTA")]
    out = [
        (
            "executions",
            spec["count"],
            [*base, ("crossSeriesReducer", "REDUCE_SUM"), ("groupByFields", spec["label"])],
        )
    ]
    for p in PERCENTILES:
        out.append(
            (f"p{p}", spec["latency"], [*base, ("crossSeriesReducer", f"REDUCE_PERCENTILE_{p}")])
        )
    return out


def metric_filter(spec, metric, name, region):
    """The Monitoring filter of one metric of the function; `name` is the function id (1st gen) or
    the Cloud Run service (2nd gen). Both were validated as ids before they got here."""
    return f'metric.type = "{metric}" AND ' + spec["resource"].format(name=name, region=region)


def _value(point):
    value = point.get("value") if isinstance(point, dict) else None
    value = value if isinstance(value, dict) else {}
    raw = value.get("int64Value", value.get("doubleValue"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def outcome_counts(series, spec):
    """{outcome: executions} from the REDUCE_SUM series, one per outcome label value. A series with
    no label value is counted as `unspecified`."""
    out = {}
    for item in series:
        metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        labels = metric.get("labels") if isinstance(metric.get("labels"), dict) else {}
        outcome = labels.get(spec["key"])
        outcome = outcome if isinstance(outcome, str) and outcome else "unspecified"
        total = sum(v for v in (_value(p) for p in item.get("points") or []) if v is not None)
        out[outcome] = out.get(outcome, 0) + int(round(total))
    return dict(sorted(out.items())[:MAX_OUTCOMES])


def percentile_ms(series, spec):
    """The percentile of the newest point of the (single) series, in milliseconds, or None with
    no series or no value."""
    best, best_end = None, ""
    for item in series:
        for point in item.get("points") or []:
            value = _value(point)
            end = str(((point or {}).get("interval") or {}).get("endTime") or "")
            if value is not None and value >= 0 and (best is None or end >= best_end):
                best, best_end = value, end
    return None if best is None else round(best * spec["to_ms"], 2)


def is_error(outcome, generation):
    return outcome == "5xx" if generation == 2 else outcome != "ok"


def summarize(spec, generation, results, start, end, timeout_seconds, st):
    """The `metrics` output. `results` maps a query name to its series, or None when it failed."""
    counted = results.get("executions")
    outcomes = None if counted is None else outcome_counts(counted, spec)
    executions = None if outcomes is None else sum(outcomes.values())
    has_data = None if outcomes is None else bool(outcomes) and executions > 0
    errors = None
    if outcomes and executions:
        errors = sum(n for o, n in outcomes.items() if is_error(o, generation))
    rate = round(errors / executions, 4) if errors is not None and executions else None
    pcts = {}
    for p in PERCENTILES:
        series = results.get(f"p{p}")
        pcts[f"p{p}Ms"] = None if series is None else percentile_ms(series, spec)
    latency = None if all(results.get(f"p{p}") is None for p in PERCENTILES) else pcts
    p99 = pcts["p99Ms"]
    return {
        "window": {
            "start": iso(start),
            "end": iso(end),
            "seconds": int((end - start).total_seconds()),
        },
        "generation": generation,
        "hasData": has_data,
        "idle": None if executions is None else executions == 0,
        "executions": executions,
        "errors": errors,
        "errorRate": rate,
        "byOutcome": outcomes,
        "latency": latency,
        "timeoutSeconds": timeout_seconds,
        "series": {name: (None if s is None else len(s)) for name, s in results.items()},
        "errorRateAboveThreshold": None if rate is None else rate > st.error_rate_threshold,
        "latencyNearTimeout": None
        if p99 is None or not timeout_seconds
        else p99 >= st.latency_timeout_ratio * timeout_seconds * 1000,
    }


def deadline_from(started):
    """The monotonic time by which the last call must end, for a task that started at `started`."""
    return started + TASK_BUDGET - RESERVE
