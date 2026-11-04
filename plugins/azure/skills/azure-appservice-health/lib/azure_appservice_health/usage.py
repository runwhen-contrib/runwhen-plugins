"""http-metrics, function-metrics and plan-metrics: Azure Monitor metrics of the app or its plan.

The metrics come from Azure Monitor (`az monitor metrics list`, one metric per call so that one that
cannot be read does not take the others down), never from a scan of the app. A grain with no
datapoint is counted and never read as zero or as healthy. Each metric is a field of the output:
null when it could not be read, with the reason as an error scoped to the field."""

import time
from types import SimpleNamespace

from .az import command_timeout
from .common import number, window
from .errors import RwError
from .metrics import read_series, summarize
from .plan import plan_target, read_plan
from .site import is_function_app, read_site
from .target import PLAN, WEB_APP, own_id, plan_id, target

BUDGET = 330  # seconds for the whole task (tasks.yaml: timeout 360s), less a margin for the result
# What each metric is read as: (output field, Azure Monitor metric, aggregations, the aggregation the
# totals and the threshold are about, the other aggregations shown beside it, threshold name)
HTTP = (
    ("requests", "Requests", ["Total"], "total", None, None),
    ("http5xx", "Http5xx", ["Total"], "total", None, "http5xx"),
    ("http4xx", "Http4xx", ["Total"], "total", None, "http4xx"),
    ("responseTime", "HttpResponseTime", ["Average", "Maximum"], "average", {"maximum": "maximum"}, "response_time"),
)  # fmt: skip
FUNCTION = (
    ("executions", "FunctionExecutionCount", ["Total"], "total", None, None),
    ("executionUnits", "FunctionExecutionUnits", ["Total"], "total", None, None),
    ("httpServerErrors", "Http5xx", ["Total"], "total", None, "http5xx"),
)  # fmt: skip
PLAN_METRICS = (
    ("cpu", "CpuPercentage", ["Average", "Maximum"], "average", {"maximum": "maximum"}, "cpu"),
    ("memory", "MemoryPercentage", ["Average", "Maximum"], "average", {"maximum": "maximum"}, "memory"),
    ("diskQueue", "DiskQueueLength", ["Average"], "average", None, "disk_queue"),
)  # fmt: skip


def read_thresholds(settings):
    """The thresholds the metric tasks declare, checked before any az call."""
    return SimpleNamespace(
        http5xx=number(settings, "http5xxThreshold", "http5xx_threshold", 0, 10**9),
        http4xx=number(settings, "http4xxThreshold", "http4xx_threshold", 0, 10**9),
        response_time=number(
            settings, "responseTimeThresholdSeconds", "response_time_threshold_seconds", 0, 10**6
        ),
        cpu=number(settings, "cpuThresholdPercent", "cpu_threshold_percent", 0, 100),
        memory=number(settings, "memoryThresholdPercent", "memory_threshold_percent", 0, 100),
        disk_queue=number(settings, "diskQueueThreshold", "disk_queue_threshold", 0, 10**9),
    )


def total(data, key):
    """Sum of an aggregation over every grain that has a value (all of them, not only the points
    returned), or None when no grain has one: no data is not a total of zero."""
    values = [d[key] for d in data if isinstance(d.get(key), (int, float))]
    return sum(values) if values else None


def read_set(ctx, subscription, resource_id, specs, th, st, seconds, clock=None):
    """{field: summary or None} for each metric of `specs`. A metric that fails is None with an error
    scoped to its field; when every one fails, the first error is raised. A call is only started
    when it fits in the task's time budget (each may take commandTimeout), else it is a timeout."""
    clock = clock or time.monotonic  # looked up now, so a test can replace it
    started, need = clock(), command_timeout(ctx.settings)
    out, failed = {}, []
    for field, metric, aggregations, key, extra, threshold in specs:
        out[field] = None
        if clock() - started + need > BUDGET:
            failed.append(
                (
                    field,
                    RwError("timeout", f"no time left in the {BUDGET}s budget to read {metric}"),
                )
            )
            continue
        try:
            data = read_series(
                ctx, subscription, resource_id, metric, aggregations, st.metric_interval, seconds
            )
        except RwError as exc:
            failed.append((field, exc))
            continue
        limit = getattr(th, threshold) if threshold else None
        series = summarize(data, key, extra=extra, threshold=limit, above=True)
        series["metric"] = metric
        series["total"] = total(data, key) if key == "total" else None
        out[field] = series
    if len(failed) == len(specs):
        raise failed[0][1]
    for field, exc in failed:
        ctx.error(exc.code, exc.message, scope=field)
    return out


def web_metrics(ctx, window_input, st, th, specs, *, functions_only=False):
    """http-metrics (`functions_only` false) and function-metrics (true) of the web app target."""
    seconds = window(window_input)
    t = target(ctx.resource, (WEB_APP,))
    site = read_site(ctx, t)
    if functions_only and not is_function_app(site):
        raise RwError(
            "invalid_input", f"{t.name} is not a function app (kind {site.get('kind')!r})"
        )
    out = {
        "app": t.name,
        "kind": str(site.get("kind") or ""),
        "isFunctionApp": is_function_app(site),
        "windowSeconds": int(seconds),
        "interval": st.metric_interval,
    }
    out.update(read_set(ctx, t.subscription, own_id(t), specs, th, st, seconds))
    return out


def plan_metrics(ctx, window_input, st, th):
    """plan-metrics of the plan target, or of the plan the web app target runs on."""
    seconds = window(window_input)
    t, _ = plan_target(ctx, target(ctx.resource, (WEB_APP, PLAN)))
    read_plan(ctx, t)  # the plan first: one that is gone is not_found
    out = {"plan": t.name, "windowSeconds": int(seconds), "interval": st.metric_interval}
    resource_id = plan_id(t.subscription, t.group, t.name)
    out.update(read_set(ctx, t.subscription, resource_id, PLAN_METRICS, th, st, seconds))
    return out
