"""Views of SeaweedFS's HTTP answers (pure: parsed JSON in, outputs out).

Every payload is checked: a document missing a field this code reads, or holding one of the
wrong type, is an `internal` RwError, never an empty or healthy-looking result.
"""

from k8s_seaweedfs_health.common import NESTED_LIMIT, bad, cap, maybe, nested, need, number

TTL_UNITS = {0: "", 1: "m", 2: "h", 3: "d", 4: "w", 5: "M", 6: "y"}
NUM = (int, float)


def replication_string(placement):
    """'XYZ' from a ReplicaPlacement object (DiffDataCenter, DiffRack, SameRack counts; missing is 0)."""
    if not isinstance(placement, dict):
        raise bad("volume", "ReplicaPlacement is not an object")
    counts = [
        placement.get(k) or 0 for k in ("DiffDataCenterCount", "DiffRackCount", "SameRackCount")
    ]
    if not all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
        raise bad("volume", "ReplicaPlacement counts are not integers")
    return "".join(str(c) for c in counts)


def ttl_string(ttl):
    """'' for no TTL, else like '3d', from a Ttl {Count, Unit} object."""
    if not isinstance(ttl, dict):
        raise bad("volume", "Ttl is not an object")
    count, unit = ttl.get("Count") or 0, ttl.get("Unit") or 0
    if not count:
        return ""
    return f"{count}{TTL_UNITS.get(unit, f'unit{unit}')}"


# ------------------------------------------------------------- master ----


def cluster_view(doc):
    """Master GET /cluster/status: leadership and raft peers (Peers is absent on a single master)."""
    what = "master /cluster/status"
    is_leader = need(doc, "IsLeader", bool, what)
    leader = maybe(doc, "Leader", str, what)
    peers = maybe(doc, "Peers", list, what)
    if peers is not None and not all(isinstance(p, str) for p in peers):
        raise bad(what, "Peers holds something other than strings")
    return {
        "isLeader": is_leader,
        "leader": leader or None,
        "peers": nested(peers) if peers is not None else None,
        "peerCount": len(peers) if peers is not None else None,
        "maxVolumeId": maybe(doc, "MaxVolumeId", int, what),
    }


def _topology(doc, what):
    topo = need(doc, "Topology", dict, what)
    need(topo, "Max", int, what)
    need(topo, "Free", int, what)
    return topo


def _data_nodes(topo, what):
    for dc in topo.get("DataCenters") or []:
        for rack in need(dc, "Racks", list, what):
            for node in need(rack, "DataNodes", list, what):
                yield dc.get("Id"), rack.get("Id"), node


def slots_view(doc, min_free, max_items):
    """Volume slots from master /dir/status: total, free, per data node, against `min_free`."""
    what = "master /dir/status"
    topo = _topology(doc, what)
    total, free = topo["Max"], topo["Free"]
    nodes = []
    for dc, rack, node in _data_nodes(topo, what):
        url = need(node, "Url", str, what)
        volumes, mx = need(node, "Volumes", int, what), need(node, "Max", int, what)
        nodes.append(
            {
                "url": url,
                "dataCenter": dc,
                "rack": rack,
                "volumes": volumes,
                "ecShards": maybe(node, "EcShards", int, what) or 0,
                "max": mx,
                "full": volumes >= mx,
            }
        )
    return {
        "version": maybe(doc, "Version", str, what),
        "topologyId": maybe(doc, "TopologyId", str, what),
        "max": total,
        "free": free,
        "used": total - free,
        "usedPercent": number(100.0 * (total - free) / total) if total else None,
        "minFreeSlots": min_free,
        "belowMinFree": free < min_free,
        "dataNodeCount": len(nodes),
        "dataNodesTruncated": len(nodes) > max_items,
        "dataNodes": cap(nodes, max_items),
    }


def layouts_view(doc, max_items):
    """Writable volumes per collection / replication / TTL / disk type from master /dir/status."""
    what = "master /dir/status"
    topo = _topology(doc, what)
    if "Layouts" not in topo:
        raise bad(what, "missing 'Layouts'")
    items = []
    for lay in topo["Layouts"] or []:
        writables = maybe(lay, "writables", list, what) or []
        if not all(isinstance(w, int) and not isinstance(w, bool) for w in writables):
            raise bad(what, "writables holds something other than volume ids")
        items.append(
            {
                "collection": need(lay, "collection", str, what),
                "replication": need(lay, "replication", str, what),
                "ttl": need(lay, "ttl", str, what),
                "diskType": maybe(lay, "diskType", str, what),
                "writableCount": len(writables),
                "writableVolumeIds": writables[:NESTED_LIMIT],
            }
        )
    items.sort(key=lambda x: (x["collection"], x["replication"], x["ttl"], x["diskType"] or ""))
    return {
        "count": len(items),
        "withoutWritable": sum(1 for i in items if i["writableCount"] == 0),
        "truncated": len(items) > max_items,
        "items": cap(items, max_items),
    }


def _volume_rows(doc):
    """(node, volume) for every replica in master /vol/status."""
    what = "master /vol/status"
    vols = need(doc, "Volumes", dict, what)
    for dc in (need(vols, "DataCenters", dict, what)).values():
        for rack in dc.values():
            for node, volumes in rack.items():
                if not isinstance(volumes, list):
                    raise bad(what, f"volumes of {node!r} is not a list")
                for v in volumes:
                    yield node, v


def _volume(v):
    what = "master /vol/status"
    return {
        "id": need(v, "Id", int, what),
        "collection": need(v, "Collection", str, what),
        "size": need(v, "Size", int, what),
        "fileCount": need(v, "FileCount", int, what),
        "deletedBytes": need(v, "DeletedByteCount", int, what),
        "readOnly": need(v, "ReadOnly", bool, what),
        "replication": replication_string(v.get("ReplicaPlacement") or {}),
        "ttl": ttl_string(v.get("Ttl") or {}),
    }


def volumes_view(doc, garbage_threshold, max_items):
    """Per collection totals, read-only volumes and compaction candidates from master /vol/status."""
    replicas = [(node, _volume(v)) for node, v in _volume_rows(doc)]
    first = {}
    for _, v in replicas:
        first.setdefault(v["id"], v)
    read_only_ids = {v["id"] for _, v in replicas if v["readOnly"]}
    by_col = {}
    for vid, v in first.items():
        c = by_col.setdefault(
            v["collection"],
            {"collection": v["collection"], "volumeCount": 0, "replicaCount": 0, "readOnlyVolumeCount": 0,
             "logicalBytes": 0, "physicalBytes": 0, "fileCount": 0, "deletedBytes": 0},
        )  # fmt: skip
        c["volumeCount"] += 1
        c["readOnlyVolumeCount"] += vid in read_only_ids
        c["logicalBytes"] += v["size"]
        c["fileCount"] += v["fileCount"]
        c["deletedBytes"] += v["deletedBytes"]
    for _, v in replicas:
        c = by_col[v["collection"]]
        c["replicaCount"] += 1
        c["physicalBytes"] += v["size"]
    for c in by_col.values():
        c["garbageRatio"] = (
            number(c["deletedBytes"] / c["logicalBytes"]) if c["logicalBytes"] else None
        )
    collections = sorted(by_col.values(), key=lambda c: c["collection"])
    read_only = sorted(
        (
            {
                "id": v["id"],
                "collection": v["collection"],
                "node": node,
                "sizeBytes": v["size"],
                "fileCount": v["fileCount"],
                "replication": v["replication"],
                "ttl": v["ttl"],
            }
            for node, v in replicas
            if v["readOnly"]
        ),
        key=lambda r: (r["id"], r["node"]),
    )
    candidates = sorted(
        (
            {
                "id": v["id"],
                "collection": v["collection"],
                "garbageRatio": number(v["deletedBytes"] / v["size"]),
                "sizeBytes": v["size"],
                "deletedBytes": v["deletedBytes"],
            }
            for v in first.values()
            if v["size"] and v["deletedBytes"] / v["size"] >= garbage_threshold
        ),
        key=lambda c: (-c["garbageRatio"], c["id"]),
    )
    return {
        "volumeCount": len(first),
        "replicaCount": len(replicas),
        "readOnlyVolumeCount": len(read_only_ids),
        "readOnlyReplicaCount": len(read_only),
        "readOnlyTruncated": len(read_only) > max_items,
        "readOnly": cap(read_only, max_items),
        "collectionCount": len(collections),
        "collectionsTruncated": len(collections) > max_items,
        "collections": cap(collections, max_items),
        "garbageThreshold": garbage_threshold,
        "compactionCandidateCount": len(candidates),
        "compactionCandidates": nested(candidates),
    }


def collection_names(doc):
    """Sorted non-empty collection names (a bucket's volumes are its collection) from /vol/status."""
    return sorted(
        {
            v["collection"]
            for _, v in ((n, _volume(x)) for n, x in _volume_rows(doc))
            if v["collection"]
        }
    )


def collection_bytes(doc):
    """{collection: logical bytes} (one replica per volume id) from /vol/status."""
    first = {}
    for _, v in ((n, _volume(x)) for n, x in _volume_rows(doc)):
        first.setdefault(v["id"], v)
    out = {}
    for v in first.values():
        out[v["collection"]] = out.get(v["collection"], 0) + v["size"]
    return out


def data_node_hosts(doc):
    """{host: {url, volumes, max}} of the data nodes in master /dir/status, keyed by the host part of
    their Url (the pod IP for a pod-hosted volume server)."""
    what = "master /dir/status"
    out = {}
    for _, _, node in _data_nodes(_topology(doc, what), what):
        url = need(node, "Url", str, what)
        out[url.rsplit(":", 1)[0]] = {
            "url": url,
            "volumes": need(node, "Volumes", int, what),
            "max": need(node, "Max", int, what),
        }
    return out


# ------------------------------------------------------------- volume server ----


def disk_view(doc, min_free_percent, max_items):
    """Disk usage and volume counts from a volume server's GET /status (DiskStatuses)."""
    what = "volume server /status"
    disks = []
    for d in need(doc, "DiskStatuses", list, what):
        percent_free = need(d, "percent_free", NUM, what)
        disks.append(
            {
                "dir": need(d, "dir", str, what),
                "allBytes": need(d, "all", int, what),
                "usedBytes": need(d, "used", int, what),
                "freeBytes": need(d, "free", int, what),
                "percentFree": number(percent_free),
                "percentUsed": number(need(d, "percent_used", NUM, what)),
                "belowMinFree": percent_free < min_free_percent,
            }
        )
    volumes = need(doc, "Volumes", list, what) if doc.get("Volumes") is not None else []
    return {
        "version": maybe(doc, "Version", str, what),
        "disks": cap(disks, max_items),
        "minFreePercent": min_free_percent,
        "volumeCount": len(volumes),
        "readOnlyVolumeCount": sum(1 for v in volumes if need(v, "ReadOnly", bool, what)),
    }


# ------------------------------------------------------------- filer ----


def check_filer_root(doc):
    """The filer's `/?metadata=true` answer must be the root entry; any other document is not a filer."""
    what = "filer /?metadata=true"
    if need(doc, "FullPath", str, what) != "/":
        raise bad(what, f"FullPath is {doc['FullPath']!r}, not '/'")


def bucket_entry(doc, name):
    """The filer entry of /buckets/<name>: quota and the NAMES of its extended attributes (never values)."""
    what = f"filer /buckets/{name}?metadata=true"
    if need(doc, "FullPath", str, what) != f"/buckets/{name}":
        raise bad(what, f"FullPath is {doc['FullPath']!r}")
    quota = need(doc, "Quota", int, what)
    extended = maybe(doc, "Extended", dict, what) or {}
    return {
        "quotaBytes": abs(quota) or None,
        "quotaState": "enabled" if quota > 0 else ("disabled" if quota < 0 else "none"),
        "extendedKeys": sorted(extended)[:NESTED_LIMIT],
    }


def master_summary(members):
    """Leadership across the master pods: how many claim it, whom they name, whether they agree."""
    queried = [m for m in members if m["isLeader"] is not None]
    leaders = sorted({m["leader"] for m in queried if m["leader"]})
    return {
        "podCount": len(members),
        "queriedCount": len(queried),
        "leaderCount": sum(1 for m in queried if m["isLeader"]),
        "leaders": leaders[:NESTED_LIMIT],
        "leaderReported": bool(leaders),
        "leaderAgreement": (len(leaders) == 1 and all(m["leader"] for m in queried))
        if queried
        else None,
        "members": nested(members),
    }
