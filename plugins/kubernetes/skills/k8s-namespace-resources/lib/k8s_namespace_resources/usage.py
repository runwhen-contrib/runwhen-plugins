"""Container usage from metrics.k8s.io PodMetrics against the pod's requests and limits."""

from k8s_namespace_resources.common import bytes_of, millicores, percent
from k8s_namespace_resources.containers import containers_of, is_running

RESOURCES = (
    # (resource, unit, parse)
    ("cpu", "millicores", millicores),
    ("memory", "bytes", bytes_of),
)


def _sort_key(entry):
    # Highest use of the limit first; containers with no limit last, by use of the request.
    pl, pr = entry["percentOfLimit"], entry["percentOfRequest"]
    return (
        pl is None,
        -(pl or 0),
        pr is None,
        -(pr or 0),
        entry["pod"],
        entry["container"],
        entry["resource"],
    )


def usage_entries(metrics, pods, threshold):
    """(entries, counts) for PodMetrics items against the pods that report them.

    One entry per container and resource: usage, the request and limit of the
    container (numbers in `unit`, None when unset or when the pods could not be
    read) and usage as a percentage of each. `atOrAbove` compares the percentage
    of the limit with `threshold`; it is None without a limit, because usage
    above a request alone is normal bursting. `pods` is None when the pod list
    was forbidden, so no request or limit is known.
    """
    by_name = None if pods is None else {p["metadata"]["name"]: p for p in pods}
    entries, with_metrics, without_pod = [], set(), 0
    for item in metrics:
        name = item["metadata"]["name"]
        pod = None if by_name is None else by_name.get(name)
        if by_name is not None and pod is None:
            without_pod += 1  # the pod went away between the two reads
            continue
        with_metrics.add(name)
        specs = {}
        if pod is not None:
            specs = {c["name"]: (init, c) for init, c in containers_of(pod.get("spec") or {})}
        for mc in item.get("containers") or []:
            init, spec = specs.get(mc["name"], (False, {}))
            resources = spec.get("resources") or {}
            for resource, unit, parse in RESOURCES:
                used = parse((mc.get("usage") or {}).get(resource))
                request = parse((resources.get("requests") or {}).get(resource))
                limit = parse((resources.get("limits") or {}).get(resource))
                of_limit = percent(used, limit)
                entries.append(
                    {
                        "pod": name,
                        "container": mc["name"],
                        "init": init,
                        "resource": resource,
                        "unit": unit,
                        "usage": used,
                        "request": request,
                        "limit": limit,
                        "percentOfLimit": of_limit,
                        "percentOfRequest": percent(used, request),
                        "atOrAbove": None if of_limit is None else of_limit >= threshold,
                    }
                )
    entries.sort(key=_sort_key)
    running = None if by_name is None else [n for n, p in by_name.items() if is_running(p)]
    return entries, {
        "pods": None if by_name is None else len(by_name),
        "podsWithMetrics": len(with_metrics),
        "podsWithoutMetrics": None
        if running is None
        else sum(1 for n in running if n not in with_metrics),
    }
