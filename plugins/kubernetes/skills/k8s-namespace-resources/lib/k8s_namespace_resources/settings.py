"""Containers without requests or limits, in running pods and in workload templates."""

from k8s_namespace_resources.containers import (
    containers_of,
    is_running,
    missing_settings,
    resource_settings,
)
from k8s_namespace_resources.owners import controller_ref, index_objects, resolve_owner


def _entry(init, container):
    settings = resource_settings(container)
    return {"container": container["name"], "init": init, **settings}, missing_settings(settings)


def flagged_pod_containers(pods):
    """[(pod, entry)] for every container of a Running pod with at least one of
    cpu/memory request/limit unset. The owner is filled in by `with_owner`."""
    out = []
    for pod in pods:
        if not is_running(pod):
            continue
        for init, container in containers_of(pod.get("spec") or {}):
            entry, missing = _entry(init, container)
            if missing:
                out.append((pod, {**entry, "missing": missing}))
    return out


def needs_kinds(flagged):
    """The intermediate owner kinds (ReplicaSet, Job) the flagged pods hang off."""
    kinds = set()
    for pod, _ in flagged:
        ref = controller_ref(pod)
        if ref:
            kinds.add(ref["kind"])
    return kinds


def pod_entries(flagged, replicasets, jobs):
    """Output entries for the flagged pod containers, owner resolved through the
    ReplicaSet or Job lists (None when a list could not be read: the immediate
    owner is then returned unresolved)."""
    index = index_objects(ReplicaSet=replicasets or [], Job=jobs or [])
    out = []
    for pod, entry in flagged:
        out.append(
            {
                "pod": pod["metadata"]["name"],
                "owner": resolve_owner(pod, index),
                **entry,
            }
        )
    out.sort(key=lambda e: (e["pod"], e["init"] is False, e["container"]))
    return out


def workload_entries(kind, workloads):
    """Entries for the containers of each workload's pod template that leave a
    cpu/memory request or limit unset."""
    out = []
    for w in workloads:
        template = (w.get("spec") or {}).get("template") or {}
        for init, container in containers_of(template.get("spec") or {}):
            entry, missing = _entry(init, container)
            if missing:
                out.append(
                    {
                        "kind": kind,
                        "name": w["metadata"]["name"],
                        **entry,
                        "missing": missing,
                    }
                )
    out.sort(key=lambda e: (e["kind"], e["name"], e["init"] is False, e["container"]))
    return out


def count_containers(pods):
    return sum(len(list(containers_of(p.get("spec") or {}))) for p in pods if is_running(p))
