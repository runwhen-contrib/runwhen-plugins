"""VerticalPodAutoscaler recommendations against the requests of the workloads they target."""

from k8s_namespace_resources.common import bytes_of, millicores, nested, percent
from k8s_namespace_resources.containers import containers_of

# VPA targetRef kind -> kubectl resource. Any other kind is reported as unsupported, not as an error.
TARGET_KINDS = {
    "Deployment": "deployments.apps",
    "StatefulSet": "statefulsets.apps",
    "DaemonSet": "daemonsets.apps",
    "ReplicaSet": "replicasets.apps",
    "Job": "jobs.batch",
    "CronJob": "cronjobs.batch",
}


def target_resources(vpas):
    """The kubectl resources to list so every supported VPA target can be found."""
    kinds = {(v.get("spec", {}).get("targetRef") or {}).get("kind") for v in vpas}
    return sorted(TARGET_KINDS[k] for k in kinds if k in TARGET_KINDS)


def _pod_spec(kind, workload):
    spec = workload.get("spec") or {}
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def _requests(pod_spec):
    """{container name: (init, {cpu, memory})} from a pod spec's requests."""
    out = {}
    for init, c in containers_of(pod_spec):
        requests = (c.get("resources") or {}).get("requests") or {}
        out[c["name"]] = (init, {"cpu": requests.get("cpu"), "memory": requests.get("memory")})
    return out


def _delta(recommended, current, parse):
    if recommended is None or current is None:
        return None
    cur = parse(current)
    if not cur:
        return None
    return percent(parse(recommended) - cur, cur)


def _condition(vpa, ctype):
    for c in (vpa.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c.get("status") == "True"
    return None


def vpa_entries(vpas, workloads, threshold, max_items):
    """Entries for the VPAs (by name), with the recommendation of each container
    beside the current requests of the targeted workload.

    `workloads` is {kubectl resource: [workload]} for the kinds that could be
    listed; a supported kind missing from it was unreadable, so `found` is None.
    `differs` is True when the recommendation is at least `threshold` percent
    above or below the current request for cpu or memory, False when every
    comparable value is within it, and None when nothing is comparable (no
    request set, or no recommendation yet).
    """
    out = []
    for v in sorted(vpas, key=lambda v: v["metadata"]["name"]):
        ref = (v.get("spec") or {}).get("targetRef") or {}
        kind, name = ref.get("kind"), ref.get("name")
        resource = TARGET_KINDS.get(kind)
        found, current = None, {}
        if resource is not None and resource in workloads:
            match = next((w for w in workloads[resource] if w["metadata"]["name"] == name), None)
            found = match is not None
            if match is not None:
                current = _requests(_pod_spec(kind, match))
        recs = ((v.get("status") or {}).get("recommendation") or {}).get(
            "containerRecommendations"
        ) or []
        containers = []
        for r in recs:
            init, cur = current.get(r["containerName"], (False, {"cpu": None, "memory": None}))
            target = r.get("target") or {}
            cpu_pct = _delta(target.get("cpu"), cur["cpu"], millicores)
            mem_pct = _delta(target.get("memory"), cur["memory"], bytes_of)
            comparable = [p for p in (cpu_pct, mem_pct) if p is not None]
            containers.append(
                {
                    "container": r["containerName"],
                    "init": init,
                    "current": cur,
                    "target": {"cpu": target.get("cpu"), "memory": target.get("memory")},
                    "lowerBound": _pair(r.get("lowerBound")),
                    "upperBound": _pair(r.get("upperBound")),
                    "cpuDifferencePercent": cpu_pct,
                    "memoryDifferencePercent": mem_pct,
                    "differs": any(abs(p) >= threshold for p in comparable) if comparable else None,
                }
            )
        containers, more = nested(containers)
        out.append(
            {
                "name": v["metadata"]["name"],
                "targetKind": kind,
                "targetName": name,
                "targetSupported": resource is not None,
                "targetFound": found,
                "updateMode": ((v.get("spec") or {}).get("updatePolicy") or {}).get("updateMode"),
                "recommendationProvided": _condition(v, "RecommendationProvided"),
                "containers": containers,
                "truncated": more,
            }
        )
    return out[: int(max_items)]


def _pair(q):
    q = q or {}
    return {"cpu": q.get("cpu"), "memory": q.get("memory")}
