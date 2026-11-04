"""instance-metrics: the AWS/EC2 CloudWatch metrics of one instance over a look-back window.

One get-metric-statistics call per metric (CPUUtilization; NetworkIn and NetworkOut; EBSReadBytes,
EBSWriteBytes, EBSReadOps and EBSWriteOps; for a burstable type CPUCreditBalance and
CPUCreditUsage) over the same window, the dimension being the instance id. One call that fails
(throttled, forbidden, out of time) leaves its metrics null and is reported by the caller as an
error scoped to the metric; the others stand. The loop has a deadline (READ_BUDGET): a metric it
did not reach is a `timeout` error, never an empty one.

No data is not zero and not health. A stopped instance, an instance launched a minute ago and an
instance whose monitoring is not in yet all have no datapoints: every number that needs data is
then null and `hasData` is false, and so is every threshold boolean that needs it. EC2 publishes
a metric for a running instance even when the value is 0, so a metric without datapoints is
unknown, never 0 (unlike a Lambda function's Errors).

Periods: 300 s up to a 3-day window, 3600 s above (GetMetricStatistics returns at most 1440
datapoints). The window is `[now - since, now]` and is not aligned to the period, so the first
datapoint can cover less than a full period and the last is still filling."""

import time
from datetime import timedelta, timezone

from .awscli import DEFAULT_TIMEOUT, aws_json
from .errors import RwError

NAMESPACE = "AWS/EC2"
# metric -> statistics asked for, and whether only a burstable type has it
METRICS = (
    ("CPUUtilization", ("Average", "Maximum", "SampleCount"), False),
    ("NetworkIn", ("Sum",), False),
    ("NetworkOut", ("Sum",), False),
    ("EBSReadBytes", ("Sum",), False),
    ("EBSWriteBytes", ("Sum",), False),
    ("EBSReadOps", ("Sum",), False),
    ("EBSWriteOps", ("Sum",), False),
    ("CPUCreditBalance", ("Minimum", "Average"), True),
    ("CPUCreditUsage", ("Sum",), True),
)
MAX_FIVE_MINUTE_SECONDS = 3 * 86400  # up to here 5-minute periods (864 datapoints at most)
READ_BUDGET = 90.0  # seconds the metric loop may take (the task's timeout is 120 s)


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


def wanted(burstable):
    """The metrics to read for an instance of this kind: [(name, statistics)]."""
    return [(name, stats) for name, stats, burst_only in METRICS if burstable or not burst_only]


def read_metric(ctx, target, metric, statistics, start, end, period, timeout=None):
    """The datapoints of one metric of the instance `target`: a list of dicts as CloudWatch
    returns them."""
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
            f"Name=InstanceId,Value={target.instance_id}",
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
        timeout=timeout,
    )
    points = doc.get("Datapoints") if isinstance(doc, dict) else None
    if not isinstance(points, list):
        # a missing key is not "no datapoints": CloudWatch answers [] for that
        raise RwError(
            "internal", f"cloudwatch get-metric-statistics {metric} returned no Datapoints list"
        )
    return [p for p in points if isinstance(p, dict)]


def read_all(ctx, target, burstable, start, end, period, started):
    """({metric: datapoints or None}, [(metric, RwError)]) for every metric of this instance. The
    loop stops reading when READ_BUDGET seconds have passed since `started` (time.monotonic());
    the metrics it did not reach are failures with code timeout."""
    points, failures = {}, []
    for metric, statistics in wanted(burstable):
        left = READ_BUDGET - (time.monotonic() - started)
        try:
            if left < 1:
                raise RwError(
                    "timeout", f"not read, the {READ_BUDGET:.0f}s budget for the metrics is used up"
                )
            points[metric] = read_metric(
                ctx,
                target,
                metric,
                statistics,
                start,
                end,
                period,
                timeout=min(DEFAULT_TIMEOUT, left),
            )
        except RwError as exc:
            points[metric] = None
            failures.append((metric, exc))
    return points, failures


def _number(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def total(points, stat="Sum"):
    """The sum of `stat` over the datapoints, None for no datapoints (unknown, not 0)."""
    if not points:
        return None
    return _number(round(sum(p.get(stat) or 0 for p in points), 2))


def cpu_facts(points):
    """{averagePercent, maxPercent, sampleCount}; averagePercent is weighted by each datapoint's
    SampleCount. All null (sampleCount 0) with no datapoints."""
    if not points:
        return {"averagePercent": None, "maxPercent": None, "sampleCount": 0}
    count = sum(p.get("SampleCount") or 0 for p in points)
    weighted = sum((p.get("Average") or 0) * (p.get("SampleCount") or 0) for p in points)
    return {
        "averagePercent": round(weighted / count, 2) if count else None,
        "maxPercent": round(max(p.get("Maximum") or 0 for p in points), 2),
        "sampleCount": _number(count),
    }


def credit_facts(balance, usage):
    """{minimum, average, usage}: the lowest CPUCreditBalance, the mean of its per-period
    averages and the credits spent; each null with no datapoints of its own."""
    return {
        "minimum": round(min(p.get("Minimum") or 0 for p in balance), 2) if balance else None,
        "average": (
            round(sum(p.get("Average") or 0 for p in balance) / len(balance), 2)
            if balance
            else None
        ),
        "usage": total(usage),
    }


def summarize(points, start, end, period, config, st):
    """The `metrics` output from the datapoints of each metric (None: that call failed; absent:
    not read for this kind of instance). `config` is the instance's description (state, type,
    monitoring).

    The booleans are the threshold crossings the settings define: cpuAverageAboveThreshold and
    cpuMaxAboveThreshold (> cpuUtilizationThreshold, strictly) and cpuCreditBalanceBelowThreshold
    (the lowest balance < cpuCreditBalanceThreshold, strictly). A boolean is null when the number
    it needs was not read or there is no data, never false by default."""
    burstable = config["burstable"]
    cpu_points = points.get("CPUUtilization")
    cpu = None if cpu_points is None else cpu_facts(cpu_points)
    has_data = None if cpu_points is None else bool(cpu_points)

    def group(names):
        return {n: points.get(n) for n in names}

    net, ebs = (
        group(("NetworkIn", "NetworkOut")),
        group(("EBSReadBytes", "EBSWriteBytes", "EBSReadOps", "EBSWriteOps")),
    )
    credits = None
    if burstable:
        balance, usage = points.get("CPUCreditBalance"), points.get("CPUCreditUsage")
        credits = credit_facts(balance, usage)
        if balance is None:  # the call failed: unknown, which is not "no datapoints"
            credits["minimum"] = credits["average"] = None
        if usage is None:
            credits["usage"] = None
        credits["hasData"] = None if balance is None else bool(balance)
    average = cpu["averagePercent"] if cpu else None
    peak = cpu["maxPercent"] if cpu else None
    low = credits["minimum"] if credits else None
    return {
        "window": {
            "start": iso(start),
            "end": iso(end),
            "seconds": int((end - start).total_seconds()),
        },
        "periodSeconds": period,
        "instanceState": config["state"]["name"],
        "instanceType": config["instanceType"],
        "burstable": burstable,
        "monitoring": config["monitoring"],
        "hasData": has_data,
        "cpu": cpu,
        "network": {
            "inBytes": total(net["NetworkIn"]),
            "outBytes": total(net["NetworkOut"]),
            "hasData": _any_data(net.values()),
        },
        "ebs": {
            "readBytes": total(ebs["EBSReadBytes"]),
            "writeBytes": total(ebs["EBSWriteBytes"]),
            "readOps": total(ebs["EBSReadOps"]),
            "writeOps": total(ebs["EBSWriteOps"]),
            "hasData": _any_data(ebs.values()),
        },
        "cpuCredits": credits,
        "datapoints": {
            name: (None if points.get(name) is None else len(points[name]))
            for name, _stats, _burst in METRICS
        },
        "cpuAverageAboveThreshold": (
            None if average is None else average > st.cpu_utilization_threshold
        ),
        "cpuMaxAboveThreshold": None if peak is None else peak > st.cpu_utilization_threshold,
        "cpuCreditBalanceBelowThreshold": (
            None if low is None else low < st.cpu_credit_balance_threshold
        ),
    }


def _any_data(groups):
    """True when any metric of the group has datapoints, None when none was read (every call
    failed), else False."""
    groups = list(groups)
    if all(g is None for g in groups):
        return None
    return any(bool(g) for g in groups)
