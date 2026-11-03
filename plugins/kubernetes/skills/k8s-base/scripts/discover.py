import json
import os
from pathlib import Path

from k8s_base import discovery_doc as dd
from k8s_base.discovery import discover
from k8s_base.errors import RwError
from k8s_base.settings import parse_duration
from k8s_base.sync import SyncClient, load_credential

ARTIFACT = "sync"
DEADLINE_RANGE = (1, 570)  # seconds; the task's own timeout is 600s and the push comes after


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


def _deadline(settings):
    seconds = parse_duration(settings.deadline, "deadline")
    low, high = DEADLINE_RANGE
    if not low <= seconds <= high:
        raise RwError("invalid_input", f"setting deadline must be from {low}s to {high}s")
    return seconds


def main(ctx):
    max_items = _bounded(ctx.settings, "max_items", 1, 100000)
    max_string = _bounded(ctx.settings, "max_string_bytes", 64, 1048576)
    namespaces = _namespaces(ctx.settings)
    deadline = _deadline(ctx.settings)
    scope, cluster = _cluster(ctx.resource)

    run, uid, duration_ms = discover(
        ctx, scope, cluster, namespaces, max_items, max_string, deadline
    )
    partitions = run.partitions
    summary = dd.summarize(run.counts, partitions, ctx.errors, duration_ms, uid)
    doc = dd.document(scope, run.items, partitions, summary)

    # streamed: one json.dump chunk at a time, never the whole document as one string
    with open(Path(os.environ["RW_OUT"]) / ARTIFACT, "w") as artifact:
        json.dump(doc, artifact, separators=(",", ":"))

    credential = os.environ.get("RW_SYNC_CREDENTIAL")
    if credential:
        client = SyncClient(load_credential(credential))
        commit, rejected = client.push(doc)
        if rejected:
            ctx.error(
                "internal",
                f"the platform rejected {len(rejected)} items: {sorted({r.get('code') for r in rejected})}",
                scope="sync",
            )
        if commit.get("status") == "held":
            ctx.error(
                "internal",
                f"the platform held the commit: {commit.get('heldReason') or 'no reason given'}",
                scope="sync",
            )
        partitions = client.sent_partitions or partitions
        # after the push, so what it reported is part of the summary
        summary = dd.summarize(run.counts, partitions, ctx.errors, duration_ms, uid)
    return {"summary": summary}
