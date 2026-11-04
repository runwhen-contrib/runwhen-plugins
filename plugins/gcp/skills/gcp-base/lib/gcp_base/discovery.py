"""discover: the project, its buckets and its Cloud Functions, as facts and as an rw.discovery.v1 sync.

Each type is read and partitioned on its own: a bucket list that fails does not stop the functions
from being listed, and the other way round. Only when both fail does the task fail (with the bucket
list's error); otherwise the failed type is an error scoped to it, a partition that is not
`complete` (so nothing is swept for it) and an output with `read: false`.

The gcloud calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync"). A function is
project -> region -> cloud-function (integration/types.yaml): the sync has an item for each region
that holds a function or was unreachable, and one cloud-function partition per region."""

import time

from .errors import RwError
from .fnlist import UNKNOWN_REGION, generation, list_functions, parse_name, state
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
    bucket resources). A failure raises (discover decides whether that fails the task)."""
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


class Read:
    """What one type's read came to: `rows` (None: it failed), `exc` (the RwError, or None) and,
    for functions, the regions gcloud could not reach."""

    def __init__(self, rows=None, exc=None, unreachable=()):
        self.rows, self.exc, self.unreachable = rows, exc, list(unreachable)

    @property
    def ok(self):
        return self.exc is None


def attempt(read, *args):
    """A Read of `read(*args)`: an RwError is the read's failure, not the task's."""
    try:
        out = read(*args)
    except RwError as exc:
        return Read(exc=exc)
    if isinstance(out, tuple):
        return Read(rows=out[0], unreachable=out[1])
    return Read(rows=out)


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


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def listing(read, facts, limit, **extra):
    """A type's output: `read` is False (items empty, counts null) when its list failed."""
    if not read.ok:
        return {"read": False, "items": [], "total": None, "omitted": None, **extra}
    items, total, omitted = bound(facts, limit)
    return {"read": True, "items": items, "total": total, "omitted": omitted, **extra}


def regions_of(functions):
    """The regions of the sync: those with a function, and those gcloud could not reach (named by
    their own name; the `unknown` placeholder is no region)."""
    names = {parse_name(r)[1] for r in functions.rows or []}
    names |= {r for r in functions.unreachable if r != UNKNOWN_REGION}
    return sorted(names)


def failure(read, what):
    """(status, reason) of a partition whose read failed."""
    status = "forbidden" if read.exc.code == "permission_denied" else "failed"
    return status, f"{what}: {read.exc.message}"[:4096]


def partitions_of(path, buckets, functions):
    """The partitions of a full sync: the buckets, the regions, and the cloud-functions of each
    region. A type whose read failed has its partition `forbidden` / `failed` with the reason (and
    no count), so nothing is swept for it; a region gcloud could not reach, or any region when it
    could not say which, is `failed`."""
    out = []
    if buckets.ok:
        out.append(
            {"type": "bucket", "parentPath": path, "status": "complete", "count": len(buckets.rows)}
        )
    else:
        status, reason = failure(buckets, "bucket list")
        out.append({"type": "bucket", "parentPath": path, "status": status, "reason": reason})
    if not functions.ok:
        status, reason = failure(functions, "functions list")
        out.append({"type": "region", "parentPath": path, "status": status, "reason": reason})
        return out
    regions = regions_of(functions)
    unknown = UNKNOWN_REGION in functions.unreachable
    if unknown:
        out.append({"type": "region", "parentPath": path, "status": "failed",
                    "reason": "gcloud could not reach some regions and did not name them"})  # fmt: skip
    else:
        out.append(
            {"type": "region", "parentPath": path, "status": "complete", "count": len(regions)}
        )
    for region in regions:
        parent = f"{path}/regions/{region}"
        count = sum(1 for r in functions.rows if parse_name(r)[1] == region)
        if region in functions.unreachable or unknown:
            reason = (
                f"gcloud could not reach {region}"
                if region in functions.unreachable
                else "gcloud could not reach some regions and did not name them"
            )
            out.append(
                {
                    "type": "cloud-function",
                    "parentPath": parent,
                    "status": "failed",
                    "reason": reason,
                }
            )
        else:
            out.append(
                {
                    "type": "cloud-function",
                    "parentPath": parent,
                    "status": "complete",
                    "count": count,
                }
            )
    return out


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


def build_sync(path, project, project_facts, buckets, functions, summary, partitions):
    """The rw.discovery.v1 document for one full sync of the project at `path`: the project, its
    buckets, the regions that hold functions (or were unreachable) and the functions. A type whose
    read failed has no items."""
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
    for row in buckets.rows or []:
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
    if functions.ok:
        for region in regions_of(functions):
            items.append(
                {
                    "identity": {
                        "platform": PLATFORM,
                        "chain": [root, {"type": "region", "name": region}],
                    },
                    "displayName": region,
                    "document": {"name": region},
                }
            )
        for row in functions.rows:
            fact = function_fact(row)
            item = {
                "identity": {
                    "platform": PLATFORM,
                    "chain": [
                        root,
                        {"type": "region", "name": fact["region"]},
                        {"type": "cloud-function", "name": fact["name"]},
                    ],
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


def discover(ctx, max_items, timeout, deadline=None):
    """Run discovery on the project this task targets; returns the task's outputs and the sync
    document. The buckets, the project resource and the functions are read independently: a
    failure of one is an error scoped to it (`buckets`, `project`, `functions`) and discovery goes
    on with the others (the project id is the root's canonical key either way). If both the
    bucket list and the functions list fail, the task fails with the bucket list's error."""
    started = time.monotonic()
    project = target_project(ctx.resource)
    path = ctx.resource["path"]
    errors = []
    buckets = attempt(read_buckets, ctx, project, timeout)
    project_facts = read_project(ctx, project, errors, timeout)
    functions = attempt(lambda: list_functions(ctx, project, timeout=timeout, deadline=deadline))
    if not buckets.ok and not functions.ok:
        raise buckets.exc
    if not buckets.ok:
        errors.append(
            {
                "code": buckets.exc.code,
                "message": f"buckets: {buckets.exc.message}",
                "scope": "buckets",
            }
        )
    if not functions.ok:
        errors.append(
            {
                "code": functions.exc.code,
                "message": f"functions: {functions.exc.message}",
                "scope": "functions",
            }
        )
    if functions.unreachable:
        named = [r for r in functions.unreachable if r != UNKNOWN_REGION]
        errors.append({
            "code": "unreachable",
            "message": "functions: gcloud could not reach "
            + (", ".join(named) if named else "some regions")
            + ": their functions are not in the list",
            "scope": "functions",
        })  # fmt: skip
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])
    counts = {"project": 1}
    if buckets.ok:
        counts["bucket"] = len(buckets.rows)
    if functions.ok:
        counts["region"] = len(regions_of(functions))
        counts["cloud-function"] = len(functions.rows)
    partitions = partitions_of(path, buckets, functions)
    summary = summarize(
        counts, partitions, errors, int((time.monotonic() - started) * 1000), project
    )
    sync = build_sync(path, project, project_facts, buckets, functions, summary, partitions)
    outputs = {
        "summary": summary,
        "project": project_facts,
        "buckets": listing(buckets, [bucket_fact(r) for r in buckets.rows or []], max_items),
        "functions": listing(
            functions,
            [function_fact(r) for r in functions.rows or []],
            max_items,
            unreachableRegions=functions.unreachable,
        ),
    }
    return outputs, sync
