"""cluster-status: the release's workloads and pods, and what its master pods say about leadership."""

from k8s_seaweedfs_health.api import cluster_view, master_summary
from k8s_seaweedfs_health.common import cap
from k8s_seaweedfs_health.discovery import pod_view, workload_view
from k8s_seaweedfs_health.fetch import Reads, get_json, healthz, load_release, load_workloads


def master_member(ctx, rel, pod, reads):
    name = pod["metadata"]["name"]
    member = {
        "pod": name,
        "queried": False,
        "notQueriedReason": None,
        "healthz": None,
        "isLeader": None,
        "leader": None,
        "peers": None,
        "peerCount": None,
        "maxVolumeId": None,
    }
    status = reads.run(
        f"master/{name}/cluster-status",
        lambda: cluster_view(
            get_json(ctx, rel, pod, "master", "cluster/status", "master /cluster/status")
        ),
    )
    member["healthz"] = reads.run(
        f"master/{name}/healthz", lambda: healthz(ctx, rel, pod, "master", "cluster/healthz")
    )
    if status is not None:
        member.update(status, queried=True)
    return member


def not_queried(pod):
    status = pod.get("status") or {}
    why = f"pod is {status.get('phase') or 'in an unknown phase'}"
    if status.get("phase") == "Running":
        why = "pod has no IP yet or is being deleted"
    return {
        "pod": pod["metadata"]["name"],
        "queried": False,
        "notQueriedReason": why,
        "healthz": None,
        "isLeader": None,
        "leader": None,
        "peers": None,
        "peerCount": None,
        "maxVolumeId": None,
    }


def run(ctx, release=""):
    rel = load_release(ctx, release)
    workloads, unread = load_workloads(ctx, rel)  # a forbidden list is on ctx.error already
    reads = Reads(ctx, have_result=True)
    ok, rest = rel.role_pods("master")
    members = [master_member(ctx, rel, p, reads) for p in ok] + [not_queried(p) for p in rest]
    reads.finish()
    if rest and not ok:
        ctx.error(
            "unreachable",
            f"no Running master pod to ask ({', '.join(p['metadata']['name'] for p in rest)} not Running)",
            scope="master",
        )
    max_items = ctx.settings.max_items
    pods = [pod_view(p) for p in sorted(rel.pods, key=lambda p: p["metadata"]["name"])]
    views = [workload_view(o, kind) for kind, o in workloads]
    views.sort(key=lambda w: (w["kind"], w["name"]))
    return {
        "release": {
            "name": rel.name,
            "namespace": rel.namespace,
            "workloads": {
                "count": len(views),
                "truncated": len(views) > max_items,
                "unread": unread,
                "items": cap(views, max_items),
            },
            "pods": {
                "count": len(pods),
                "truncated": len(pods) > max_items,
                "items": cap(pods, max_items),
            },
        },
        "master": master_summary(sorted(members, key=lambda m: m["pod"])),
    }
