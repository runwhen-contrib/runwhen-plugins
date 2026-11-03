"""filer-status: the filer pods answering, and whether the volume servers are registered with the master."""

from k8s_seaweedfs_health.api import check_filer_root, data_node_hosts
from k8s_seaweedfs_health.common import nested
from k8s_seaweedfs_health.fetch import Reads, first_master_json, get_json, healthz, load_release


def filer_of(ctx, rel, pod, reads):
    name = pod["metadata"]["name"]
    health = reads.run(f"filer/{name}/healthz", lambda: healthz(ctx, rel, pod, "filer", "healthz"))

    def root():
        check_filer_root(
            get_json(ctx, rel, pod, "filer", "?metadata=true", "filer /?metadata=true")
        )
        return True

    return {
        "pod": name,
        "healthz": health,
        "rootReadable": bool(reads.run(f"filer/{name}/root", root)),
    }


def volume_servers(rel, topology):
    """Each volume-role pod against the data nodes the master lists (matched on pod IP), and the data
    nodes that no volume pod of this release explains."""
    hosts = data_node_hosts(topology)
    ok, rest = rel.role_pods("volume")
    items, matched = [], set()
    for pod in ok + rest:
        ip = (pod.get("status") or {}).get("podIP")
        node = hosts.get(ip)
        if node:
            matched.add(ip)
        items.append(
            {
                "pod": pod["metadata"]["name"],
                "podIP": ip,
                "running": pod in ok,
                "registered": node is not None,
                "dataNodeUrl": node["url"] if node else None,
                "volumes": node["volumes"] if node else None,
                "maxVolumes": node["max"] if node else None,
            }
        )
    unmatched = sorted(h["url"] for ip, h in hosts.items() if ip not in matched)
    return {
        "podCount": len(items),
        "registeredCount": sum(1 for i in items if i["registered"]),
        "dataNodeCount": len(hosts),
        "items": nested(items),
        "unmatchedDataNodes": unmatched[:50],
    }


def run(ctx, release=""):
    rel = load_release(ctx, release)
    reads = Reads(ctx)
    ok, rest = rel.role_pods("filer")
    filers = [f for f in (filer_of(ctx, rel, p, reads) for p in ok[: ctx.settings.max_pods]) if f]
    topology = reads.run(
        "master/dir-status",
        lambda: first_master_json(ctx, rel, "dir/status", "master /dir/status", reads),
    )
    servers = (
        None
        if topology is None
        else reads.run("master/dir-status", lambda: volume_servers(rel, topology))
    )
    reads.finish()
    if rest and not ok:
        ctx.error(
            "unreachable",
            f"no Running filer pod to ask ({', '.join(p['metadata']['name'] for p in rest)} not Running)",
            scope="filer",
        )
    return {
        "filer": {
            "podCount": len(ok) + len(rest),
            "notRunning": [p["metadata"]["name"] for p in rest][:50],
            "items": filers,
        },
        "volumeServers": servers,
    }
