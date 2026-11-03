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

    Effective rule: the larger of the app containers plus all native sidecars
    (init containers with restartPolicy Always), and the peak while the init
    containers run (each regular init container plus the sidecars started
    before it), plus spec.overhead. Containers that set nothing add 0 and are
    counted so a caller knows the sum understates.
    """
    spec = pod.get("spec") or {}
    zero = Fraction(0)
    missing = 0
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
    overhead = parse_quantity(((spec.get("overhead") or {}).get(resource)))
    return total + (overhead or zero), missing


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


def overcommit(nodes, pods, max_items):
    """Per node: summed requests and limits of its non-terminated pods against allocatable.

    Returns (rows capped at max_items, inspected). Pods with no node yet
    (Pending, unscheduled) and Succeeded/Failed pods are not counted; pods on
    a node that is not in `nodes` are counted in inspected.podsOnOtherNodes.
    """
    by_node = {n["metadata"]["name"]: [] for n in nodes}
    unscheduled = other = terminated = 0
    for pod in pods:
        if not _is_active(pod):
            terminated += 1
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
        cpu = {"requests": [Fraction(0), 0], "limits": [Fraction(0), 0]}
        mem = {"requests": [Fraction(0), 0], "limits": [Fraction(0), 0]}
        for pod in by_node[name]:
            for totals, resource in ((cpu, "cpu"), (mem, "memory")):
                for section in ("requests", "limits"):
                    t, missing = effective(pod, section, resource)
                    totals[section][0] += t
                    totals[section][1] += missing
        cpu_block = _block(
            {k: (v[0] * 1000, v[1]) for k, v in cpu.items()},
            alloc["cpuMillicores"],
            number,
            "Millicores",
        )
        mem_block = _block(mem, alloc["memoryBytes"], lambda q: int(round(q)), "Bytes")
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
            }
        )
    rows.sort(key=lambda r: (r["limitsAboveAllocatable"] is not True, r["name"]))
    inspected = {
        "nodes": len(rows),
        "pods": sum(r["pods"] for r in rows),
        "podsUnscheduled": unscheduled,
        "podsTerminated": terminated,
        "podsOnOtherNodes": other,
    }
    return cap(rows, max_items), inspected
