"""performance: request volume and latency of the vault over a window."""

from .common import resolve, window
from .errors import RwError
from .metrics import read_series, summarize


def _series(
    ctx, subscription, resource_id, metric, aggregations, key, extra, threshold, st, seconds
):
    data = read_series(
        ctx, subscription, resource_id, metric, aggregations, st.metric_interval, seconds
    )
    out = summarize(data, key, extra=extra, threshold=threshold, above=True)
    out["metric"] = metric
    return out


def read(ctx, window_input, st):
    seconds = window(window_input)
    subscription, name, resource_id, _ = resolve(ctx)
    out = {
        "vault": name,
        "windowSeconds": int(seconds),
        "interval": st.metric_interval,
        "requests": None,
        "latency": None,
    }
    failed = []
    for field, metric, aggregations, key, extra, threshold in (
        ("requests", "ServiceApiHit", ["Count"], "count", None, st.request_threshold),
        (
            "latency",
            "ServiceApiLatency",
            ["Average", "Maximum"],
            "average",
            {"maximum": "maximum"},
            st.latency_threshold_ms,
        ),
    ):
        try:
            out[field] = _series(
                ctx,
                subscription,
                resource_id,
                metric,
                aggregations,
                key,
                extra,
                threshold,
                st,
                seconds,
            )
        except RwError as exc:
            failed.append((field, exc))
    if len(failed) == 2:
        raise failed[0][1]
    for field, exc in failed:
        ctx.error(exc.code, exc.message, scope=field)
    return out
