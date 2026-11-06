"""The collector's own metrics as facts about exporter queues and drop counters (pure).

Names differ between collector versions: an `otelcol_` prefix (or none) and a `_total` suffix (or
none) are both read, so `otelcol_exporter_send_failed_spans` and `exporter_send_failed_spans_total`
are the same series. Counters are cumulative since the process started.
"""

import re
from collections import defaultdict

from k8s_otelcollector_health.common import NESTED_LIMIT, bad, number

PREFIX = "otelcol_"
DATA_TYPES = {"spans": "traces", "metric_points": "metrics", "log_records": "logs"}
FAMILIES = ("exporter_", "receiver_", "processor_", "process_")
_COUNTER = re.compile(
    r"^(?P<component>exporter)_(?P<counter>send_failed|enqueue_failed)_(?P<kind>[a-z_]+)$"
    r"|^(?P<component2>receiver)_(?P<counter2>refused)_(?P<kind2>[a-z_]+)$"
    r"|^(?P<component3>processor)_(?P<counter3>dropped|refused)_(?P<kind3>[a-z_]+)$"
)
_UPTIME = re.compile(r"^process_uptime(_seconds)?$")


def canonical(name):
    """Metric name without the otelcol_ prefix and the _total suffix."""
    if name.startswith(PREFIX):
        name = name[len(PREFIX) :]
    return name[: -len("_total")] if name.endswith("_total") else name


def total(values):
    """Sum of values, None when none of them is a number."""
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _num(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def view(samples, pod, threshold_percent, what="the collector's metrics page"):
    """(queues, counters, facts) for one pod from its parsed samples.

    A page with no exporter, receiver, processor or process series is not a collector's metrics
    (a different port, a login page) and is an `internal` error.
    """
    collector_series = sum(1 for s in samples if canonical(s.name).startswith(FAMILIES))
    if not collector_series:
        raise bad(
            what,
            f"none of its {len(samples)} series is a collector series (exporter_*, receiver_*, processor_*, process_*)",
        )
    sizes, capacities = defaultdict(list), defaultdict(list)
    counters, errors = defaultdict(list), defaultdict(set)
    uptime = None
    for s in samples:
        name = canonical(s.name)
        if name in ("exporter_queue_size", "exporter_queue_capacity"):
            exporter = s.labels.get("exporter")
            if not exporter:
                raise bad(what, f"{s.name} has no exporter label")
            key = (exporter, s.labels.get("data_type"))
            (sizes if name.endswith("size") else capacities)[key].append(s.value)
        elif _UPTIME.match(name):
            uptime = s.value
        else:
            m = _COUNTER.match(name)
            if not m:
                continue
            g = m.groupdict()
            component = g["component"] or g["component2"] or g["component3"]
            counter = g["counter"] or g["counter2"] or g["counter3"]
            kind = g["kind"] or g["kind2"] or g["kind3"]
            entity = s.labels.get(component)
            if not entity:
                raise bad(what, f"{s.name} has no {component} label")
            key = (component, entity, counter, DATA_TYPES.get(kind, kind))
            counters[key].append(s.value)
            if s.labels.get("error_type"):
                errors[key].add(s.labels["error_type"])
    queues = []
    for key in sorted(set(sizes) | set(capacities), key=lambda k: (k[0], k[1] or "")):
        size, capacity = total(sizes.get(key, [])), total(capacities.get(key, []))
        pct = number(100.0 * size / capacity) if size is not None and capacity else None
        queues.append(
            {
                "pod": pod,
                "exporter": key[0],
                "dataType": key[1],
                "size": _num(size),
                "capacity": _num(capacity),
                "utilizationPercent": pct,
                "atOrAboveThreshold": None if pct is None else pct >= threshold_percent,
                "full": None if pct is None else size >= capacity,
            }
        )
    queues.sort(
        key=lambda q: (
            -(q["utilizationPercent"] if q["utilizationPercent"] is not None else -1),
            q["exporter"],
            q["dataType"] or "",
        )
    )
    rows = []
    for key, values in counters.items():
        component, entity, counter, data_type = key
        rows.append(
            {
                "pod": pod,
                "component": component,
                "name": entity,
                "counter": counter,
                "dataType": data_type,
                "value": _num(total(values)),
                "errorTypes": sorted(errors.get(key, ()))[:NESTED_LIMIT],
            }
        )
    rows.sort(
        key=lambda r: (-(r["value"] or 0), r["component"], r["name"], r["counter"], r["dataType"])
    )
    facts = {
        "uptimeSeconds": None if uptime is None else number(uptime),
        "seriesCount": len(samples),
    }
    return queues, rows, facts


def pod_entry(pod, facts=None, reason=None):
    """The pods output entry: queried with its facts, or not queried with the reason."""
    return {
        "pod": pod,
        "queried": facts is not None,
        "notQueriedReason": reason,
        "uptimeSeconds": facts["uptimeSeconds"] if facts else None,
        "seriesCount": facts["seriesCount"] if facts else None,
    }


def merge(per_pod):
    """The pods' queues and counters as two lists, worst first."""
    queues = [q for qs, _ in per_pod for q in qs]
    counters = [c for _, cs in per_pod for c in cs]
    queues.sort(
        key=lambda q: (
            -(q["utilizationPercent"] if q["utilizationPercent"] is not None else -1),
            q["pod"],
            q["exporter"],
            q["dataType"] or "",
        )
    )
    counters.sort(
        key=lambda r: (
            -(r["value"] or 0),
            r["pod"],
            r["component"],
            r["name"],
            r["counter"],
            r["dataType"],
        )
    )
    return queues, counters
