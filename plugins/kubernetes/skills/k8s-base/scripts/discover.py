import json
import os
from pathlib import Path

from k8s_base import discovery_doc as dd
from k8s_base.discovery import discover
from k8s_base.errors import RwError
from k8s_base.sync import SyncClient, load_credential

ARTIFACT = "sync"


def _bounded(settings, name, low, high):
    value = getattr(settings, name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RwError("invalid_input", f"setting {name} must be an integer from {low} to {high}")
    return value


def _namespaces(settings):
    value = list(settings.namespaces)
    if not all(isinstance(n, str) and n for n in value):
        raise RwError("invalid_input", "setting namespaces must be a list of namespace names")
    return sorted(set(value))


def _cluster(resource):
    """(scope path, cluster name) of the target, which must be the cluster root."""
    path = str(resource.path).strip("/")
    parts = path.split("/")
    if len(parts) != 3 or parts[:2] != ["kubernetes", "clusters"]:
        raise RwError(
            "invalid_input",
            f"discover targets a cluster (kubernetes/clusters/<name>), got {path!r}",
        )
    return path, parts[2]


def main(ctx):
    max_items = _bounded(ctx.settings, "max_items", 1, 100000)
    max_string = _bounded(ctx.settings, "max_string_bytes", 64, 1048576)
    namespaces = _namespaces(ctx.settings)
    scope, cluster = _cluster(ctx.resource)

    run, uid, duration_ms = discover(ctx, scope, cluster, namespaces, max_items, max_string)
    summary = dd.summarize(run.counts, run.partitions, ctx.errors, duration_ms, uid)
    doc = dd.document(scope, run.items, run.partitions, summary)

    out = Path(os.environ["RW_OUT"]) / ARTIFACT
    out.write_text(json.dumps(doc, separators=(",", ":")))

    credential = os.environ.get("RW_SYNC_CREDENTIAL")
    if credential:
        _, rejected = SyncClient(load_credential(credential)).push(doc)
        if rejected:
            ctx.error(
                "internal",
                f"the platform rejected {len(rejected)} items: {sorted({r.get('code') for r in rejected})}",
                scope="sync",
            )
    return {"summary": summary}
