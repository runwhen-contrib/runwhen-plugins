"""availability: the vault's Availability metric over a window, every grain of it."""

from .common import resolve, window
from .metrics import read_series, summarize


def read(ctx, window_input, st):
    seconds = window(window_input)
    subscription, name, resource_id, _ = resolve(ctx)
    data = read_series(
        ctx, subscription, resource_id, "Availability", ["Average"], st.metric_interval, seconds
    )
    series = summarize(data, "average", threshold=st.availability_min_percent, above=False)
    return {
        "vault": name,
        "metric": "Availability",
        "unit": "Percent",
        "windowSeconds": int(seconds),
        "interval": st.metric_interval,
        "thresholdPercent": st.availability_min_percent,
        "hasData": series["withData"] > 0,
        "belowThreshold": series["crossed"],
        "grainsBelowThreshold": series["crossings"],
        "grains": series["grains"],
        "grainsWithData": series["withData"],
        "grainsMissing": series["missing"],
        "min": series["min"],
        "max": series["max"],
        "last": series["last"],
        "points": series["points"],
        "pointsOmitted": series["pointsOmitted"],
    }
