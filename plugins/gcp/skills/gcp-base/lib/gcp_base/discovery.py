"""discover: the project and its buckets, as facts and as an rw.discovery.v1 sync.

The gcloud calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync")."""

import time

from .errors import RwError
from .fnlist import generation, list_functions, parse_name, state
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


PARTIAL_PROJECT = ("permission_denied", "dependency", "rate_limited", "not_found")
NO_FACTS = {"projectNumber": None, "displayName": None, "lifecycleState": None}


def project_facts(row):
    """(projectNumber, displayName, lifecycleState) from the project resource, or None for a shape
    that is neither Resource Manager's v1 (`projectNumber`, `name`, `lifecycleState`, what
    `gcloud projects describe` prints today) nor v3 (`name` = `projects/<number>`, `displayName`,
    `state`)."""
    pick = lambda key: row[key] if isinstance(row.get(key), str) else None  # noqa: E731
    if "lifecycleState" in row:  # v1
        return pick("projectNumber"), pick("name"), pick("lifecycleState")
    if "state" in row or "displayName" in row:  # v3
        name = pick("name") or ""
        number = name.split("/", 1)[1] if name.startswith("projects/") else None
        return number or None, pick("displayName"), pick("state")
    return None


def read_project(ctx, project, errors, timeout):
    """The project's facts, or the identifier alone with the reason appended to `errors` when the
    project resource cannot be had for a reason that leaves the rest useful: this identity may not
    read it, the API is disabled, the call was throttled, or the project is gone (the bucket list
    already worked). A response of an unknown shape is an error scoped `project` too, never
    silent nulls. Any other failure raises."""

    def alone(code, message):
        errors.append({"code": code, "message": f"project: {message}", "scope": "project"})
        return {"projectId": project, **NO_FACTS}

    try:
        row = gcloud_json(ctx, "projects", "describe", project, timeout=timeout)
    except RwError as exc:
        if exc.code not in PARTIAL_PROJECT:
            raise
        return alone(exc.code, exc.message)
    facts = project_facts(row) if isinstance(row, dict) else None
    if facts is None:
        return alone("internal", "gcloud projects describe returned a resource of an unknown shape")
    number, name, state = facts
    return {
        "projectId": project,
        "projectNumber": number,
        "displayName": name,
        "lifecycleState": state,
    }


def function_fact(row):
    """The `functions` output entry of one function row (either generation's format)."""
    _, region, name = parse_name(row)
    gen = generation(row)
    config = row.get("buildConfig") if gen == 2 else row
    runtime = config.get("runtime") if isinstance(config, dict) else None
    update = row.get("updateTime")
    return {
        "name": name,
        "region": region,
        "generation": gen,
        "state": state(row),
        "runtime": runtime if isinstance(runtime, str) else None,
        "updateTime": update if isinstance(update, str) else None,
    }


def split_ambiguous(rows):
    """(rows whose id is unique in the project, the ids found in more than one region). A function
    is named by its id alone, so an id in two regions cannot be two resources; neither is synced."""
    regions = {}
    for row in rows:
        _, region, name = parse_name(row)
        regions.setdefault(name, set()).add(region)
    ambiguous = sorted(n for n, r in regions.items() if len(r) > 1)
    return [r for r in rows if parse_name(r)[2] not in ambiguous], ambiguous


def read_functions(ctx, project, errors, timeout):
    """(facts, rows, status): the project's functions, the rows to sync and the partition status.
    The buckets were listed already, so a functions list that fails (denied, the Cloud Functions
    API off, throttled ...) is an error scoped `functions` and the partition is `forbidden` /
    `failed`, never an empty list: (None, [], status). A list that read only part of the project
    (gcloud could not reach some regions) or found one id in several regions is returned with an
    error and a `failed` partition: nothing is swept for it."""
    try:
        rows, unreachable = list_functions(ctx, project, timeout=timeout)
    except RwError as exc:
        errors.append(
            {"code": exc.code, "message": f"functions: {exc.message}", "scope": "functions"}
        )
        return None, [], "forbidden" if exc.code == "permission_denied" else "failed"
    complete = True
    if unreachable:
        complete = False
        errors.append({
            "code": "unreachable",
            "message": "functions: gcloud could not reach " + ", ".join(unreachable)
            + ": their functions are not in the list",
            "scope": "functions",
        })  # fmt: skip
    synced, ambiguous = split_ambiguous(rows)
    if ambiguous:
        complete = False
        errors.append({
            "code": "internal",
            "message": "functions: the same function id exists in several regions and cannot be "
            "named by its id alone: " + ", ".join(ambiguous[:20]),
            "scope": "functions",
        })  # fmt: skip
    facts = {
        "items": [function_fact(r) for r in rows],
        "unreachableRegions": unreachable,
        "ambiguousIds": ambiguous[:50],
    }
    return facts, synced, "complete" if complete else "failed"


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def bucket_listing(buckets, limit):
    items, total, omitted = bound(buckets, limit)
    return {"items": items, "total": total, "omitted": omitted}


def function_listing(functions, limit):
    """The `functions` output: `read` is False (items empty, counts null) when the list failed."""
    if functions is None:
        return {"read": False, "items": [], "total": None, "omitted": None,
                "unreachableRegions": [], "ambiguousIds": []}  # fmt: skip
    items, total, omitted = bound(functions["items"], limit)
    return {"read": True, "items": items, "total": total, "omitted": omitted,
            "unreachableRegions": functions["unreachableRegions"],
            "ambiguousIds": functions["ambiguousIds"]}  # fmt: skip


def bucket_partitions(path, count):
    """The partition a full sync of a project sweeps for its buckets."""
    return [{"type": "bucket", "parentPath": path, "status": "complete", "count": count}]


def partitions_of(path, bucket_count, function_status, function_count):
    """The partitions of a full sync: the buckets and the cloud functions of the project."""
    return [
        *bucket_partitions(path, bucket_count),
        {"type": "cloud-function", "parentPath": path, "status": function_status,
         "count": function_count},
    ]  # fmt: skip


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


def build_sync(path, project, project_facts, rows, summary, functions=(), partitions=None):
    """The rw.discovery.v1 document for one full sync of the project at `path`: the project, its
    buckets and the function rows to sync."""
    partitions = partitions or bucket_partitions(path, len(rows))
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
    for row in functions:
        fact = function_fact(row)
        item = {
            "identity": {
                "platform": PLATFORM,
                "chain": [root, {"type": "cloud-function", "name": fact["name"]}],
            },
            "displayName": fact["name"],
            "document": {k: v for k, v in fact.items() if k != "name" and v is not None},
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
        "commit": {"partitions": partitions, "summary": summary},
    }


def discover(ctx, max_items, timeout):
    """Run discovery on the project this task targets; returns the task's outputs and the sync
    document. A failure to list buckets raises; a project resource this identity may not read, or
    a functions list that fails, is a ctx.error and discovery goes on (the id is the root's
    canonical key either way)."""
    started = time.monotonic()
    project = target_project(ctx.resource)
    path = ctx.resource["path"]
    errors = []
    rows = read_buckets(ctx, project, timeout)  # first: a failure here fails the task
    project_facts = read_project(ctx, project, errors, timeout)
    functions, fn_rows, fn_status = read_functions(ctx, project, errors, timeout)
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])
    counts = {"project": 1, "bucket": len(rows)}
    if functions is not None:
        counts["cloud-function"] = len(fn_rows)
    partitions = partitions_of(path, len(rows), fn_status, len(fn_rows))
    summary = summarize(
        counts, partitions, errors, int((time.monotonic() - started) * 1000), project
    )
    sync = build_sync(path, project, project_facts, rows, summary, fn_rows, partitions)
    buckets = [bucket_fact(r) for r in rows]
    outputs = {
        "summary": summary,
        "project": project_facts,
        "buckets": bucket_listing(buckets, max_items),
        "functions": function_listing(functions, max_items),
    }
    return outputs, sync
