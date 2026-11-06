"""VerticalPodAutoscaler recommendations against the requests of the workloads they target."""

from k8s_namespace_resources.common import bytes_of, millicores, nested, percent, trunc
from k8s_namespace_resources.containers import containers_of, effective_requests
from k8s_namespace_resources.owners import index_objects, owner_chain

# VPA targetRef kind -> kubectl resource. Any other kind is reported as unsupported, not as an error.
TARGET_KINDS = {
    "Deployment": "deployments.apps",
    "StatefulSet": "statefulsets.apps",
    "DaemonSet": "daemonsets.apps",
    "ReplicaSet": "replicasets.apps",
    "Job": "jobs.batch",
    "CronJob": "cronjobs.batch",
}

# The workload kinds whose pods hang off a ReplicaSet or Job, so those lists are needed to find them.
VIA_REPLICASET = ("Deployment", "ReplicaSet")
VIA_JOB = ("CronJob", "Job")


def _kinds(vpas):
    return {(v.get("spec", {}).get("targetRef") or {}).get("kind") for v in vpas}


def target_resources(vpas):
    """The kubectl resources to list so every supported VPA target can be found."""
    return sorted(TARGET_KINDS[k] for k in _kinds(vpas) if k in TARGET_KINDS)


def intermediate_resources(vpas):
    """The ReplicaSet / Job lists needed to tell which pods belong to the targets."""
    kinds = _kinds(vpas)
    out = []
    if kinds & set(VIA_REPLICASET):
        out.append("replicasets.apps")
    if kinds & set(VIA_JOB):
        out.append("jobs.batch")
    return out


def _pod_spec(kind, workload):
    spec = workload.get("spec") or {}
    if kind == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def _requests(pod_spec):
    """{container name: (init, {cpu, memory})}: the template's requests, a limit
    standing in for a request that is unset (the API server does the same when
    it creates the pod)."""
    return {c["name"]: (init, effective_requests(c)) for init, c in containers_of(pod_spec)}


def _live_requests(pods, replicasets, jobs, kind, name):
    """{container name: {pods, cpu, memory}} for the live pods that belong to the
    target: how many carry the container and the distinct requests they have
    (None when a pod sets none). Pods that finished are left out."""
    index = index_objects(ReplicaSet=replicasets or [], Job=jobs or [])
    out = {}
    for pod in pods:
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        if (kind, name) not in owner_chain(pod, index):
            continue
        for _, c in containers_of(pod.get("spec") or {}):
            live = out.setdefault(c["name"], {"pods": 0, "cpu": [], "memory": []})
            live["pods"] += 1
            for res, value in effective_requests(c).items():
                if value not in live[res]:
                    live[res].append(value)
    return out


def _delta(recommended, current, parse):
    if recommended is None or current is None:
        return None
    cur = parse(current)
    if not cur:
        return None
    return percent(parse(recommended) - cur, cur)


def _outside(current, bound, below):
    """Whether `current` is below (or above) `bound` for cpu or memory; None when
    neither resource has both values."""
    verdicts = []
    for res, parse in (("cpu", millicores), ("memory", bytes_of)):
        cur, b = current.get(res), (bound or {}).get(res)
        if cur is None or b is None:
            continue
        verdicts.append(parse(cur) < parse(b) if below else parse(cur) > parse(b))
    return any(verdicts) if verdicts else None


def _condition(vpa, ctype):
    for c in (vpa.get("status") or {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c.get("status") == "True"
    return None


def _conditions(vpa):
    """(the VPA's status conditions, bounded; whether any was cut)."""
    out, cut_any = [], False
    for c in (vpa.get("status") or {}).get("conditions") or []:
        message, cut = trunc(c.get("message"))
        cut_any = cut_any or cut
        out.append(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": message,
            }
        )
    out, more = nested(out)
    return out, cut_any or more


def vpa_entries(vpas, workloads, threshold, max_items, pods=None, replicasets=None, jobs=None):
    """Entries for the VPAs (by name), with the recommendation of each container
    beside the requests of the targeted workload's pod template (`current`) and,
    when the pods could be read, those of its live pods (`currentLive`).

    `workloads` is {kubectl resource: [workload]} for the kinds that could be
    listed; a supported kind missing from it was unreadable, so `found` is None.
    `differs` is True when the recommendation is at least `threshold` percent
    above or below the current request for cpu or memory, False when every
    comparable value is within it, and None when nothing is comparable (no
    request set, or no recommendation yet). `belowLowerBound` / `aboveUpperBound`
    say whether the current request is outside the recommender's bounds for cpu or
    memory (None when nothing is comparable). `pods` is None when the pod list was
    not read; `replicasets` / `jobs` resolve which pods a Deployment or CronJob owns.
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
        live = None
        if pods is not None and resource is not None and found:
            live = _live_requests(pods, replicasets, jobs, kind, name)
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
                    "currentLive": None
                    if live is None
                    else _live_entry(live.get(r["containerName"])),
                    "target": {"cpu": target.get("cpu"), "memory": target.get("memory")},
                    "lowerBound": _pair(r.get("lowerBound")),
                    "upperBound": _pair(r.get("upperBound")),
                    "cpuDifferencePercent": cpu_pct,
                    "memoryDifferencePercent": mem_pct,
                    "differs": any(abs(p) >= threshold for p in comparable) if comparable else None,
                    "belowLowerBound": _outside(cur, r.get("lowerBound"), below=True),
                    "aboveUpperBound": _outside(cur, r.get("upperBound"), below=False),
                }
            )
        containers, more = nested(containers)
        conditions, cut = _conditions(v)
        out.append(
            {
                "name": v["metadata"]["name"],
                "targetKind": kind,
                "targetName": name,
                "targetSupported": resource is not None,
                "targetFound": found,
                "updateMode": ((v.get("spec") or {}).get("updatePolicy") or {}).get("updateMode"),
                "recommendationProvided": _condition(v, "RecommendationProvided"),
                "conditions": conditions,
                "containers": containers,
                "truncated": more or cut,
            }
        )
    return out[: int(max_items)]


def _live_entry(live):
    """The live pods' requests for one container; no pod carries it -> zero pods."""
    live = live or {"pods": 0, "cpu": [], "memory": []}
    cpu, _ = nested(live["cpu"])
    memory, _ = nested(live["memory"])
    return {"pods": live["pods"], "cpu": cpu, "memory": memory}


def _pair(q):
    q = q or {}
    return {"cpu": q.get("cpu"), "memory": q.get("memory")}
