"""Pure functions behind node-utilization and overcommit.

Quantities go through common.parse_quantity, so whole cores ("8"), millicores
("500m"), nanocores ("123456789n") and every memory suffix are understood. The
classic check read a whole-core allocatable "8" as 8 millicores.
"""

from fractions import Fraction

from k8s_node_health.common import (
    cap,
    cpu_millicores,
    memory_bytes,
    number,
    parse_quantity,
    percent,
)

TERMINATED = ("Succeeded", "Failed")


def allocatable(node):
    """{cpuMillicores, memoryBytes, pods} from a Node's status.allocatable (None where unset)."""
    alloc = (node.get("status") or {}).get("allocatable") or {}
    pods = alloc.get("pods")
    return {
        "cpuMillicores": cpu_millicores(alloc.get("cpu")),
        "memoryBytes": memory_bytes(alloc.get("memory")),
        "pods": int(pods) if pods not in (None, "") else None,
    }


# ------------------------------------------------------ node-utilization ----


def _at_or_above(used, total, threshold):
    """used/total >= threshold percent; None when either is unknown."""
    if used is None or not total:
        return None
    return used * 100 >= total * threshold


def _any(*flags):
    """True if any flag is true, None if none is true and one is unknown, else False."""
    if any(f is True for f in flags):
        return True
    return None if any(f is None for f in flags) else False


def utilization(nodes, metrics_items, threshold, max_items):
    """Per node: metrics.k8s.io usage against allocatable for CPU and memory, highest first.

    `metrics_items` are NodeMetrics objects. A node without one has null usage
    and null `atOrAbove` (unknown), never zero usage.
    """
    metrics = {m["metadata"]["name"]: m for m in metrics_items}
    rows = []
    for node in nodes:
        name = node["metadata"]["name"]
        alloc = allocatable(node)
        m = metrics.get(name)
        usage = (m or {}).get("usage") or {}
        cpu = cpu_millicores(usage.get("cpu"))
        mem = memory_bytes(usage.get("memory"))
        cpu_at = _at_or_above(cpu, alloc["cpuMillicores"], threshold)
        mem_at = _at_or_above(mem, alloc["memoryBytes"], threshold)
        rows.append(
            {
                "name": name,
                "timestamp": (m or {}).get("timestamp"),
                "window": (m or {}).get("window"),
                "cpu": {
                    "usageMillicores": cpu,
                    "allocatableMillicores": alloc["cpuMillicores"],
                    "percent": percent(cpu, alloc["cpuMillicores"]),
                    "atOrAbove": cpu_at,
                },
                "memory": {
                    "usageBytes": mem,
                    "allocatableBytes": alloc["memoryBytes"],
                    "percent": percent(mem, alloc["memoryBytes"]),
                    "atOrAbove": mem_at,
                },
                "atOrAbove": _any(cpu_at, mem_at),
            }
        )

    def busiest(r):
        pcts = [p for p in (r["cpu"]["percent"], r["memory"]["percent"]) if p is not None]
        return (-(max(pcts) if pcts else -1), r["name"])

    rows.sort(key=busiest)
    inspected = {
        "nodes": len(rows),
        "withMetrics": sum(r["timestamp"] is not None for r in rows),
        "utilizationPercent": threshold,
    }
    return cap(rows, max_items), inspected


# ------------------------------------------------------------ overcommit ----


def _amount(container, section, resource):
    """A container's request or limit for cpu/memory in base units, or None when it sets none."""
    return parse_quantity((((container.get("resources") or {}).get(section)) or {}).get(resource))


def effective(pod, section, resource):
    """(pod total, long-running containers without a value) for `requests` or `limits`.

    Pod-level `spec.resources` (1.34+), when it sets the value, is the pod
    total and nothing is counted missing. Otherwise the effective rule: the
    larger of the app containers plus all native sidecars (init containers
    with restartPolicy Always), and the peak while the init containers run
    (each regular init container plus the sidecars started before it). Either
    way `spec.overhead` is added to requests, and to limits only when the limit
    total is above zero (a pod with no limit stays unlimited). Containers that
    set nothing add 0 and are counted so a caller knows the sum understates.
    """
    spec = pod.get("spec") or {}
    zero = Fraction(0)
    pod_level = parse_quantity((((spec.get("resources") or {}).get(section)) or {}).get(resource))
    missing = 0
    if pod_level is not None:
        total = pod_level
    else:
        app = zero
        for c in spec.get("containers") or []:
            a = _amount(c, section, resource)
            missing += a is None
            app += a or zero
        sidecars = zero
        peak = zero
        for c in spec.get("initContainers") or []:
            a = _amount(c, section, resource)
            if c.get("restartPolicy") == "Always":
                missing += a is None
                sidecars += a or zero
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + (a or zero))
        total = max(app + sidecars, peak)
    overhead = parse_quantity(((spec.get("overhead") or {}).get(resource))) or zero
    if section == "requests" or total > 0:
        total += overhead
    return total, missing


def _is_active(pod):
    return (pod.get("status") or {}).get("phase") not in TERMINATED


def _block(totals, alloc, convert, unit):
    """requests/limits sums of one resource on one node, with percent and above-allocatable flags."""
    out = {}
    for section in ("requests", "limits"):
        total, missing = totals[section]
        value = convert(total)
        out[f"{section}{unit}"] = value
        out[f"{section}Percent"] = percent(value, alloc)
        out[f"{section}AboveAllocatable"] = None if not alloc else value > alloc
        out[f"containersWithout{section.capitalize()}"] = missing
    return out


SECTIONS = ("requests", "limits")
RESOURCES = ("cpu", "memory")
TOP_PODS = 5


def _pod_amounts(pod):
    """({(section, resource): total}, {(section, resource): containers missing a value}) for a pod."""
    totals, missing = {}, {}
    for section in SECTIONS:
        for resource in RESOURCES:
            totals[section, resource], missing[section, resource] = effective(
                pod, section, resource
            )
    return totals, missing


def _share(totals, section, alloc):
    """A pod's cpu and memory for one section as amounts and percent of the node's allocatable."""
    cpu = number(totals[section, "cpu"] * 1000)
    mem = int(round(totals[section, "memory"]))
    return {
        "cpuMillicores": cpu,
        "cpuPercent": percent(cpu, alloc["cpuMillicores"]),
        "memoryBytes": mem,
        "memoryPercent": percent(mem, alloc["memoryBytes"]),
    }


def _rank(share):
    pcts = [p for p in (share["cpuPercent"], share["memoryPercent"]) if p is not None]
    return max(pcts) if pcts else 0


def _above(share, alloc, threshold):
    """Whether the pod's cpu or memory exceeds `threshold` percent of the node's allocatable."""
    flags = []
    for amount, total in (
        (share["cpuMillicores"], alloc["cpuMillicores"]),
        (share["memoryBytes"], alloc["memoryBytes"]),
    ):
        flags.append(None if not total else amount * 100 > total * threshold)
    return _any(*flags) if any(f is not None for f in flags) else None


def _top_pods(entries, section, count=TOP_PODS):
    """The `count` pods with the largest `section` share, leaving out pods that set none."""
    chosen = [
        e for e in entries if e[section]["cpuMillicores"] > 0 or e[section]["memoryBytes"] > 0
    ]
    chosen.sort(key=lambda e: (-_rank(e[section]), e["namespace"], e["name"]))
    return chosen[:count]


def overcommit(nodes, pods, max_items, pod_limit_percent=90):
    """Per node: summed requests and limits of its non-terminated pods against allocatable.

    Returns (rows capped at max_items, inspected). Succeeded/Failed pods and
    pods with no node yet (Pending, unscheduled) are not counted; pods on a
    node that is not in `nodes` are counted in inspected.podsOnOtherNodes.
    Each row also has the five pods with the largest limits and requests, and
    whether the largest single pod's limits pass `pod_limit_percent` of the
    node (the classic per-pod assertion).
    """
    by_node = {n["metadata"]["name"]: [] for n in nodes}
    unscheduled = other = 0
    for pod in pods:
        if not _is_active(pod):
            continue
        node_name = (pod.get("spec") or {}).get("nodeName")
        if not node_name:
            unscheduled += 1
        elif node_name in by_node:
            by_node[node_name].append(pod)
        else:
            other += 1
    rows = []
    for node in nodes:
        name = node["metadata"]["name"]
        alloc = allocatable(node)
        cpu = {s: [Fraction(0), 0] for s in SECTIONS}
        mem = {s: [Fraction(0), 0] for s in SECTIONS}
        entries = []
        for pod in by_node[name]:
            totals, missing = _pod_amounts(pod)
            for sums, resource in ((cpu, "cpu"), (mem, "memory")):
                for section in SECTIONS:
                    sums[section][0] += totals[section, resource]
                    sums[section][1] += missing[section, resource]
            limits = _share(totals, "limits", alloc)
            entries.append(
                {
                    "namespace": pod["metadata"].get("namespace"),
                    "name": pod["metadata"]["name"],
                    "requests": _share(totals, "requests", alloc),
                    "limits": limits,
                    "podAboveLimitPercent": _above(limits, alloc, pod_limit_percent),
                }
            )
        cpu_block = _block(
            {k: (v[0] * 1000, v[1]) for k, v in cpu.items()},
            alloc["cpuMillicores"],
            number,
            "Millicores",
        )
        mem_block = _block(mem, alloc["memoryBytes"], lambda q: int(round(q)), "Bytes")
        limited = [e for e in entries if _top_pods([e], "limits")]
        largest = max((_rank(e["limits"]) for e in limited), default=None)
        flags = [e["podAboveLimitPercent"] for e in limited]
        rows.append(
            {
                "name": name,
                "pods": len(by_node[name]),
                "podCapacity": alloc["pods"],
                "allocatable": {
                    "cpuMillicores": alloc["cpuMillicores"],
                    "memoryBytes": alloc["memoryBytes"],
                },
                "cpu": cpu_block,
                "memory": mem_block,
                "requestsAboveAllocatable": _any(
                    cpu_block["requestsAboveAllocatable"], mem_block["requestsAboveAllocatable"]
                ),
                "limitsAboveAllocatable": _any(
                    cpu_block["limitsAboveAllocatable"], mem_block["limitsAboveAllocatable"]
                ),
                "largestPodLimitPercent": largest,
                "podAboveLimitPercent": _any(*flags) if flags else None,
                "topPodsByLimits": _top_pods(entries, "limits"),
                "topPodsByRequests": _top_pods(entries, "requests"),
            }
        )
    rows.sort(
        key=lambda r: (
            r["limitsAboveAllocatable"] is not True,
            r["requestsAboveAllocatable"] is not True,
            r["podAboveLimitPercent"] is not True,
            r["name"],
        )
    )
    inspected = {
        "nodes": len(rows),
        "pods": sum(r["pods"] for r in rows),
        "podsUnscheduled": unscheduled,
        "podsOnOtherNodes": other,
    }
    return cap(rows, max_items), inspected
