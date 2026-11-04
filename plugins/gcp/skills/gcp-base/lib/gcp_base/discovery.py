"""discover: the project and its buckets, as facts and as an rw.discovery.v1 sync.

The gcloud calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync")."""

import time

from .errors import RwError
from .gcloud import gcloud_json, target_path

PLATFORM = "gcp"
SOURCE = "gcp-base/discover"
BATCH = 500  # items per batch (rw.discovery.v1 itemsBatch)
CANONICAL_KEY = "projectId"
MAX_LABELS = 64
LABEL_LIMIT = 255


def target_project(resource):
    """The project id from the run's target; discover runs on a gcp/project only."""
    project, bucket = target_path(resource)
    if bucket is not None:
        raise RwError(
            "invalid_input", f"discover runs on a gcp/project, got {(resource or {}).get('type')!r}"
        )
    return project


def read_buckets(ctx, project, timeout):
    """The project's buckets as reported by `gcloud storage buckets list --raw` (the API's own
    bucket resources). A failure raises: with no list there is nothing to report."""
    rows = gcloud_json(
        ctx, "storage", "buckets", "list", f"--project={project}", "--raw", timeout=timeout
    )
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("name") for r in rows):
        raise RwError("internal", "gcloud returned no bucket list")
    return rows


def bucket_fact(row):
    """The `buckets` output entry of one bucket resource."""
    fact = {"name": row["name"]}
    for key, source in (
        ("location", "location"),
        ("locationType", "locationType"),
        ("storageClass", "storageClass"),
        ("created", "timeCreated"),
    ):
        value = row.get(source)
        fact[key] = value if isinstance(value, str) else None
    return fact


def read_project(ctx, project, errors, timeout):
    """The project's facts, or the identifier alone with the reason appended to `errors` when this
    identity may not read the project resource (permission_denied). Any other failure raises."""
    try:
        row = gcloud_json(ctx, "projects", "describe", project, timeout=timeout)
    except RwError as exc:
        if exc.code != "permission_denied":
            raise
        errors.append({"code": exc.code, "message": f"project: {exc.message}", "scope": "project"})
        return {
            "projectId": project,
            "projectNumber": None,
            "displayName": None,
            "lifecycleState": None,
        }
    if not isinstance(row, dict):
        raise RwError("internal", "gcloud returned no project")
    pick = lambda key: row[key] if isinstance(row.get(key), str) else None  # noqa: E731
    return {
        "projectId": project,
        "projectNumber": pick("projectNumber"),
        "displayName": pick("name"),
        "lifecycleState": pick("lifecycleState"),
    }


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def bucket_listing(buckets, limit):
    items, total, omitted = bound(buckets, limit)
    return {"items": items, "total": total, "omitted": omitted}


def bucket_partitions(path, count):
    """The one partition a full sync of a project sweeps: its buckets."""
    return [{"type": "bucket", "parentPath": path, "status": "complete", "count": count}]


def summarize(counts, partitions, errors, duration_ms, project):
    """The rw.discovery.summary.v1 value, also the commit's `summary`. The script builds it a
    second time after the push, with the push's errors and the partitions the platform accepted."""
    totals = {"complete": 0, "failed": 0, "forbidden": 0, "excluded": 0}
    for p in partitions:
        totals[p["status"]] += 1
    summary = {"counts": dict(counts), "partitions": totals, "durationMs": duration_ms}
    if errors:
        summary["errors"] = list(errors)
    summary["root"] = {"canonicalKey": {"name": CANONICAL_KEY, "value": project}}
    return summary


def labels_of(row):
    """The bucket's user labels as an item `labels` map: string keys and values, at most 64."""
    raw = row.get("labels")
    if not isinstance(raw, dict):
        return {}
    kept = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and k}
    return {k[:LABEL_LIMIT]: v[:LABEL_LIMIT] for k, v in sorted(kept.items())[:MAX_LABELS]}


def build_sync(path, project, project_facts, rows, summary):
    """The rw.discovery.v1 document for one full sync of the project at `path`."""
    root = {"type": "project", "name": project}
    root_item = {
        "identity": {"platform": PLATFORM, "chain": [root]},
        "displayName": (project_facts["displayName"] or project)[:255],
        "document": {
            k: v
            for k, v in project_facts.items()
            if v is not None and k in ("projectId", "projectNumber", "lifecycleState")
        },
    }
    if project_facts["projectNumber"]:
        root_item["providerUid"] = project_facts["projectNumber"]
    items = [root_item]
    for row in rows:
        fact = bucket_fact(row)
        item = {
            "identity": {
                "platform": PLATFORM,
                "chain": [root, {"type": "bucket", "name": row["name"]}],
            },
            "displayName": row["name"],
            "document": {k: v for k, v in fact.items() if v is not None},
        }
        labels = labels_of(row)
        if labels:
            item["labels"] = labels
        items.append(item)
    batches = [
        {"seq": i // BATCH + 1, "items": items[i : i + BATCH]} for i in range(0, len(items), BATCH)
    ]
    return {
        "apiVersion": "rw.discovery.v1",
        "open": {"source": SOURCE, "scopePath": path, "mode": "full"},
        "batches": batches,
        "commit": {"partitions": bucket_partitions(path, len(rows)), "summary": summary},
    }


def discover(ctx, max_items, timeout):
    """Run discovery on the project this task targets; returns the task's outputs and the sync
    document. A failure to list buckets raises; a project resource this identity may not read is
    a ctx.error and discovery goes on (the id is the root's canonical key either way)."""
    started = time.monotonic()
    project = target_project(ctx.resource)
    path = ctx.resource["path"]
    errors = []
    rows = read_buckets(ctx, project, timeout)  # first: a failure here fails the task
    project_facts = read_project(ctx, project, errors, timeout)
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])
    summary = summarize(
        {"project": 1, "bucket": len(rows)},
        bucket_partitions(path, len(rows)),
        errors,
        int((time.monotonic() - started) * 1000),
        project,
    )
    sync = build_sync(path, project, project_facts, rows, summary)
    buckets = [bucket_fact(r) for r in rows]
    outputs = {
        "summary": summary,
        "project": project_facts,
        "buckets": bucket_listing(buckets, max_items),
    }
    return outputs, sync
