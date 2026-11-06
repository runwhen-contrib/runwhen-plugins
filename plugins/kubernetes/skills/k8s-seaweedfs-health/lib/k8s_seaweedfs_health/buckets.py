"""buckets: per bucket (collection) usage, quota and extended attribute names, and what an anonymous caller may do."""

import xml.etree.ElementTree as ET
from urllib.parse import quote

from k8s_seaweedfs_health.api import bucket_entry, collection_bytes, collection_names
from k8s_seaweedfs_health.common import bad, number
from k8s_seaweedfs_health.errors import RwError
from k8s_seaweedfs_health.fetch import Reads, first_master_json, get_json, get_text, load_release
from k8s_seaweedfs_health.k8s import ProxyError

XML_LIMIT = 1_000_000


def root_tag(text, what):
    """Local name of an S3 XML answer's root element. Anything that is not XML is an error."""
    if len(text) > XML_LIMIT:
        raise bad(what, "answer is larger than 1 MB")
    try:
        tag = ET.fromstring(text).tag
    except ET.ParseError:
        raise bad(what, f"not XML (first bytes: {text[:80]!r})") from None
    return tag.rsplit("}", 1)[-1]


def anonymous_get(ctx, rel, pod, path, expect, what):
    """'allowed' when an unauthenticated GET gets a 2xx whose XML root is `expect`, 'denied' when the
    gateway answers 401/403, 'no_such_bucket' on 404. Anything else (unreachable pod, RBAC, 5xx,
    other XML) raises."""
    try:
        text = get_text(ctx, rel, pod, "s3", path)
    except ProxyError as exc:
        if exc.kind == "upstream_denied":
            return "denied"
        if exc.kind == "upstream_missing":
            return "no_such_bucket"
        raise
    tag = root_tag(text, what)
    if tag != expect:
        raise bad(what, f"root element is {tag!r}, expected {expect!r}")
    return "allowed"


def bucket_of(ctx, rel, name, used, filer, gateway, reads):
    entry = None
    if filer is not None:

        def read():
            try:
                doc = get_json(
                    ctx,
                    rel,
                    filer,
                    "filer",
                    f"buckets/{quote(name)}?metadata=true",
                    f"filer entry of bucket {name}",
                )
            except ProxyError as exc:
                if exc.kind == "upstream_missing":
                    return False
                raise
            return bucket_entry(doc, name)

        entry = reads.run(f"bucket/{name}/entry", read)
    anonymous = None
    if gateway is not None:
        anonymous = reads.run(
            f"bucket/{name}/anonymous",
            lambda: anonymous_get(
                ctx,
                rel,
                gateway,
                f"{quote(name)}/?list-type=2&max-keys=1",
                "ListBucketResult",
                f"S3 list of bucket {name}",
            ),
        )
    found = entry is not False and entry is not None
    quota = entry["quotaBytes"] if found else None
    return {
        "name": name,
        "usedBytes": used,
        "entryFound": None if entry is None else found,
        "quotaState": entry["quotaState"] if found else None,
        "quotaBytes": quota,
        "quotaUsedPercent": number(100.0 * used / quota)
        if quota and entry["quotaState"] == "enabled"
        else None,
        "extendedKeys": entry["extendedKeys"] if found else None,
        "anonymousList": anonymous,
    }


def run(ctx, release=""):
    rel = load_release(ctx, release)
    reads = Reads(ctx)
    max_items, max_buckets = ctx.settings.max_items, ctx.settings.max_buckets
    vol_status = reads.run(
        "master/vol-status",
        lambda: first_master_json(ctx, rel, "vol/status", "master /vol/status", reads),
    )
    names = used = None
    if vol_status is not None:
        names = reads.run("master/vol-status", lambda: collection_names(vol_status))
        used = reads.run("master/vol-status", lambda: collection_bytes(vol_status))
    filers, f_rest = rel.role_pods("filer")
    gateways, g_rest = rel.role_pods("s3")
    for role, ok, rest in (("filer", filers, f_rest), ("s3", gateways, g_rest)):
        if rest and not ok:
            names_down = ", ".join(p["metadata"]["name"] for p in rest)
            reads.failures.append(
                (
                    role,
                    RwError(
                        "unreachable", f"no Running {role} pod to ask: {names_down} not Running"
                    ),
                )
            )
    filer, gateway = (filers or [None])[0], (gateways or [None])[0]
    gateway_anonymous = None
    if gateway is not None:
        gateway_anonymous = reads.run(
            "gateway/anonymous",
            lambda: anonymous_get(
                ctx, rel, gateway, "", "ListAllMyBucketsResult", "S3 list of buckets"
            ),
        )
    buckets = []
    if names is not None and used is not None:
        for name in names[:max_buckets]:
            buckets.append(bucket_of(ctx, rel, name, used.get(name, 0), filer, gateway, reads))
    reads.finish()
    return {
        "buckets": {
            "count": None if names is None else len(names),
            "inspected": len(buckets),
            "truncated": names is not None and len(names) > len(buckets),
            "items": buckets[:max_items],
        },
        "gateway": {
            "podCount": len(gateways) + len(g_rest),
            "anonymousListBuckets": gateway_anonymous,
        },
    }
