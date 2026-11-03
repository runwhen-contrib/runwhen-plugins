from k8s_namespace_resources.common import cap
from k8s_namespace_resources.containers import is_running
from k8s_namespace_resources.fetch import (
    check_settings,
    list_degradable,
    list_optional,
    load_namespace,
)
from k8s_namespace_resources.settings import (
    count_containers,
    flagged_pod_containers,
    needs_kinds,
    pod_entries,
    workload_entries,
)

WORKLOADS = (
    ("deployments", "Deployment"),
    ("statefulsets", "StatefulSet"),
    ("daemonsets", "DaemonSet"),
)


def main(ctx):
    check_settings(ctx, "max_items")
    ns = load_namespace(ctx, "resource-settings")
    lists = list_degradable(
        ctx, ns, ["pods", "deployments.apps", "statefulsets.apps", "daemonsets.apps"]
    )
    pods = lists["pods"]

    pod_findings = workload_findings = None
    if pods is not None:
        flagged = flagged_pod_containers(pods)
        kinds = needs_kinds(flagged)
        # Owners are resolved through the ReplicaSet / Job lists, read only when a flagged pod needs them.
        replicasets = (
            list_optional(ctx, ns, "replicasets", "replicasets.apps")
            if "ReplicaSet" in kinds
            else None
        )
        jobs = list_optional(ctx, ns, "jobs", "jobs.batch") if "Job" in kinds else None
        pod_findings = pod_entries(flagged, replicasets, jobs)

    read_kinds = [(plural, kind) for plural, kind in WORKLOADS if lists[plural] is not None]
    if read_kinds:
        workload_findings = [
            e for plural, kind in read_kinds for e in workload_entries(kind, lists[plural])
        ]
        workload_findings.sort(
            key=lambda e: (e["kind"], e["name"], e["init"] is False, e["container"])
        )

    return {
        "pods": None if pod_findings is None else cap(pod_findings, ctx.settings.max_items),
        "workloads": None
        if workload_findings is None
        else cap(workload_findings, ctx.settings.max_items),
        "inspected": {
            "pods": None if pods is None else len(pods),
            "runningPods": None if pods is None else sum(1 for p in pods if is_running(p)),
            "containers": None if pods is None else count_containers(pods),
            "workloads": sum(len(lists[plural]) for plural, _ in read_kinds)
            if read_kinds
            else None,
            "podEntries": None if pod_findings is None else len(pod_findings),
            "workloadEntries": None if workload_findings is None else len(workload_findings),
        },
    }
