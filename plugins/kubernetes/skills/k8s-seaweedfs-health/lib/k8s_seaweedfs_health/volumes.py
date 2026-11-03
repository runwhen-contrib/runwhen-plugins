"""volumes: slots, writable layouts, read-only volumes and disk usage from the master and volume servers."""

from k8s_seaweedfs_health.api import disk_view, layouts_view, slots_view, volumes_view
from k8s_seaweedfs_health.fetch import Reads, first_master_json, get_json, load_release


def disk_of(ctx, rel, pod, reads):
    name = pod["metadata"]["name"]
    doc = reads.run(
        f"volume/{name}/status",
        lambda: get_json(ctx, rel, pod, "volume", "status", "volume server /status"),
    )
    if doc is None:
        return None
    view = reads.run(
        f"volume/{name}/status",
        lambda: disk_view(doc, ctx.settings.min_free_disk_percent, ctx.settings.max_items),
    )
    return None if view is None else {"pod": name, **view}


def run(ctx, release=""):
    rel = load_release(ctx, release)
    reads = Reads(ctx)
    max_items = ctx.settings.max_items
    dir_status = reads.run(
        "master/dir-status",
        lambda: first_master_json(ctx, rel, "dir/status", "master /dir/status", reads),
    )
    vol_status = reads.run(
        "master/vol-status",
        lambda: first_master_json(ctx, rel, "vol/status", "master /vol/status", reads),
    )
    slots = layouts = volumes = None
    if dir_status is not None:
        slots = reads.run(
            "master/dir-status",
            lambda: slots_view(dir_status, ctx.settings.min_free_volume_slots, max_items),
        )
        layouts = reads.run("master/dir-status", lambda: layouts_view(dir_status, max_items))
    if vol_status is not None:
        volumes = reads.run(
            "master/vol-status",
            lambda: volumes_view(vol_status, ctx.settings.garbage_threshold, max_items),
        )
    ok, rest = rel.role_pods("volume")
    asked = ok[: ctx.settings.max_pods]
    items = [d for d in (disk_of(ctx, rel, p, reads) for p in asked) if d is not None]
    reads.finish()
    if rest and not ok:
        ctx.error(
            "unreachable",
            f"no Running volume pod to ask ({', '.join(p['metadata']['name'] for p in rest)} not Running)",
            scope="volume",
        )
    return {
        "slots": slots,
        "layouts": layouts,
        "volumes": volumes,
        "disk": {
            "podCount": len(ok) + len(rest),
            "queriedCount": len(items),
            "truncated": len(ok) > len(asked),
            "notRunning": [p["metadata"]["name"] for p in rest][:50],
            "items": items,
        },
    }
