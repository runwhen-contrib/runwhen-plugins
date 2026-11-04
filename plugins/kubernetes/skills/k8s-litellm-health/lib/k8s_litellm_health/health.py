"""liveness and readiness: what each LiteLLM pod answers on its unauthenticated health endpoints."""

import json

from k8s_litellm_health.common import MESSAGE_LIMIT, NESTED_LIMIT, cap, truncate
from k8s_litellm_health.errors import RwError
from k8s_litellm_health.fetch import (
    MAX_QUERIED_PODS,
    Budget,
    fetch_one,
    pod_condition,
    pod_port,
    queried,
    running,
)

LIVENESS_PATHS = ("/health/liveliness", "/health/liveness")  # LiteLLM's own spelling first
READINESS_PATH = "/health/readiness"
# Why a readiness 503 happened, from what liveness answered at the same time. Read from LiteLLM
# 1.102.1 (proxy/health_endpoints/_health_endpoints.py): /health/liveliness answers 503 only once
# a graceful shutdown has begun; /health/readiness answers 503 then too, and also when a
# configured database is not connected (unless allow_requests_on_db_unavailable is set) or when
# building the detailed payload raised. So readiness 503 with liveness 200 is a database problem
# in all but the rare error case, and 503 on both is a shutdown.
CAUSE_DB = "db_unreachable_likely"
CAUSE_DRAINING = "draining"
CAUSE_UNDETERMINED = "undetermined"
CAUSE_BASIS = {
    CAUSE_DB: (
        "readiness answered 503 while liveness answered 200: LiteLLM 503s readiness for a "
        "configured database it cannot reach and liveness only during shutdown"
    ),
    CAUSE_DRAINING: (
        "readiness and liveness both answered 503: LiteLLM answers 503 on both once a graceful "
        "shutdown has begun"
    ),
    CAUSE_UNDETERMINED: "liveness could not be read to tell a database problem from a shutdown",
}


def alive_reply(reply, what):
    """(alive, body) of a liveness reply: 200 whose JSON string says alive is True; 503 is False
    (LiteLLM answers 503 once a graceful shutdown has begun; the proxy does not pass the body on);
    a 200 with other content (an auth page, an HTML error) is an error, never alive. A 404 is the
    caller's `served: false`."""
    if reply.status == 503:
        return False, None
    if reply.status != 200:
        raise RwError("dependency", f"{what} answered HTTP {reply.status}")
    try:
        doc = json.loads(reply.body)
    except ValueError:
        doc = None
    if isinstance(doc, str) and "alive" in doc.lower():
        return True, doc
    raise RwError(
        "internal", f"{what} did not return LiteLLM's liveness text: {reply.body.strip()[:80]!r}"
    )


def readiness_doc(reply, what):
    """The parsed readiness JSON of a 200 reply: an object with a string `status`. Anything else is
    an error, never ready. The detailed payload (cache, litellm_version, success_callbacks) is only
    there when the proxy exposes it publicly (`allow_public_health_readiness_details`); the public
    payload is `status` and `db`."""
    try:
        doc = json.loads(reply.body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("status"), str):
        raise RwError(
            "internal",
            f"{what} did not return LiteLLM readiness JSON: {reply.body.strip()[:80]!r}",
        )
    for field in ("db", "litellm_version"):
        if doc.get(field) is not None and not isinstance(doc[field], str):
            raise RwError("internal", f"{what} returned a {field} that is not text")
    callbacks = doc.get("success_callbacks")
    if callbacks is not None and not isinstance(callbacks, list):
        raise RwError("internal", f"{what} returned success_callbacks that is not a list")
    return doc


def cache_name(cache):
    """The cache type as text: LiteLLM answers the type name, or {type, index_info} for a semantic
    cache; null when no cache is configured."""
    if isinstance(cache, dict):
        cache = cache.get("type")
    return None if cache is None else str(cache)


def pod_base(pod, port, probed):
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_condition(pod, "Ready"),
        "port": pod_port(pod, port),
        "queried": probed,
    }


def workload_summary(workload):
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    return {
        "kind": workload["kind"],
        "name": workload["metadata"]["name"],
        "replicas": spec.get("replicas"),
        "readyReplicas": status.get("readyReplicas"),
    }


def liveness_row(ctx, workload, pod, scheme, port, budget):
    """The row of one pod: each path asked on its own. A path that fails (not a 404/503 reply)
    leaves that path's `alive` null and raises after the rest were asked."""
    paths, failure = [], None
    for path in LIVENESS_PATHS:
        entry = {"path": path, "served": None, "httpStatus": None, "alive": None, "body": None}
        try:
            reply = fetch_one(ctx, workload, pod, scheme, port, path, budget)
            if reply.status == 404:
                entry.update(served=False, httpStatus=404)
            else:
                alive, body = alive_reply(reply, f"{path} on pod {pod['metadata']['name']}")
                entry.update(served=True, httpStatus=reply.status, alive=alive, body=body)
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failure = failure or exc
        paths.append(entry)
    served = [p for p in paths if p["served"]]
    if failure is None and not served:
        failure = RwError(
            "dependency",
            f"pod {pod['metadata']['name']} serves neither {LIVENESS_PATHS[0]} nor "
            f"{LIVENESS_PATHS[1]}: is this a LiteLLM proxy?",
        )
    verdicts = [p["alive"] for p in served]
    if failure is not None:
        alive = False if False in verdicts else None
    else:
        alive = False not in verdicts and True in verdicts
    row = {**pod_base(pod, port, True), "alive": alive, "paths": paths}
    return row, failure


def readiness_cause(ctx, workload, pod, scheme, port, budget):
    """(cause, liveness HTTP status, failure) for a pod whose readiness answered 503: liveness is
    asked on the spelling the pod serves. 200 is a database problem (likely), 503 a shutdown; a
    pod that serves neither path, or whose liveness cannot be read, is undetermined (the failure,
    if any, is the one to report)."""
    name = pod["metadata"]["name"]
    for path in LIVENESS_PATHS:
        try:
            reply = fetch_one(ctx, workload, pod, scheme, port, path, budget)
            if reply.status == 404:
                continue
            alive, _ = alive_reply(reply, f"{path} on pod {name}")
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            return CAUSE_UNDETERMINED, None, exc
        return (CAUSE_DB if alive else CAUSE_DRAINING), reply.status, None
    return CAUSE_UNDETERMINED, None, None


def readiness_row(ctx, workload, pod, scheme, port, budget):
    """(row, failure) of one pod: failure is set when a 503's cause could not be read. Raises
    RwError for a reply that is not the readiness JSON."""
    name = pod["metadata"]["name"]
    what = f"{READINESS_PATH} on pod {name}"
    reply = fetch_one(ctx, workload, pod, scheme, port, READINESS_PATH, budget)
    row = {
        **pod_base(pod, port, True),
        "ready": None,
        "httpStatus": reply.status,
        "status": None,
        "db": None,
        "cache": None,
        "litellmVersion": None,
        "successCallbacks": None,
        "successCallbacksTruncated": False,
        "detailed": False,
        "truncated": False,
        "cause": None,
        "causeBasis": None,
        "livenessHttpStatus": None,
    }
    if reply.status == 503:
        row["ready"] = False  # the proxy answered not ready; its body does not come through
        cause, live_status, failure = readiness_cause(ctx, workload, pod, scheme, port, budget)
        row.update(cause=cause, causeBasis=CAUSE_BASIS[cause], livenessHttpStatus=live_status)
        return row, failure
    if reply.status != 200:
        raise RwError("dependency", f"{what} answered HTTP {reply.status}")
    doc = readiness_doc(reply, what)
    status, cut_status = truncate(doc["status"])
    db, cut_db = truncate(doc.get("db"))
    cache, cut_cache = truncate(cache_name(doc.get("cache")))
    version, cut_version = truncate(doc.get("litellm_version"))
    callbacks = doc.get("success_callbacks")
    row.update(
        ready=doc["status"] == "healthy",
        status=status,
        db=db,
        cache=cache,
        litellmVersion=version,
        detailed="litellm_version" in doc,
        truncated=cut_status or cut_db or cut_cache or cut_version,
    )
    if callbacks is not None:
        # entries are coerced to text: the proxy names a callback by its name or its repr
        shown = [truncate(str(c)) for c in callbacks[:NESTED_LIMIT]]
        row["successCallbacks"] = [text for text, _ in shown]
        row["successCallbacksTruncated"] = len(callbacks) > NESTED_LIMIT
        row["truncated"] = row["truncated"] or any(cut for _, cut in shown)
    return row, None


def unasked(pod, port, extra):
    return {**pod_base(pod, port, False), **extra}


LIVENESS_UNASKED = {"alive": None, "paths": []}
READINESS_UNASKED = {
    "ready": None,
    "httpStatus": None,
    "status": None,
    "db": None,
    "cache": None,
    "litellmVersion": None,
    "successCallbacks": None,
    "successCallbacksTruncated": False,
    "detailed": False,
    "truncated": False,
    "cause": None,
    "causeBasis": None,
    "livenessHttpStatus": None,
}


def answered(kind, row):
    """Whether a pod's row holds an answer from the proxy (a served liveness path, or a readiness
    verdict), as opposed to nothing but failures."""
    if kind == "liveness":
        return any(p["served"] for p in row["paths"])
    return row["ready"] is not None


def names_text(names):
    shown = ", ".join(names[:10])
    return shown + (f" and {len(names) - 10} more" if len(names) > 10 else "")


def report_unasked(ctx, pods, asked):
    """One scoped error for the pods that were not asked: those not Running, and the Running ones
    past the cap of MAX_QUERIED_PODS asks (or `maxItems`), so a result that covers fewer pods than
    the workload has is `partial`, never silently complete."""
    idle = sorted(p["metadata"]["name"] for p in pods if not running(p))
    skipped = sorted(
        p["metadata"]["name"] for p in pods if running(p) and p["metadata"]["name"] not in asked
    )
    if idle:
        ctx.error(
            "unreachable",
            f"{len(idle)} pod(s) not Running, not asked: {names_text(idle)}"[:MESSAGE_LIMIT],
            scope="pods/not-running",
        )
    if skipped:
        ctx.error(
            "unreachable",
            (
                f"{len(skipped)} Running pod(s) not asked: at most {MAX_QUERIED_PODS} pods are "
                f"asked per run, and no more than maxItems are listed: {names_text(skipped)}"
            )[:MESSAGE_LIMIT],
            scope="pods/not-asked",
        )


def view(ctx, kind, workload, pods, scheme, port, limit, budget=None):
    """The liveness or readiness view. Pods that are not Running are listed and not asked. A pod
    that fails is one error scoped to it (`ctx.error`), also a pod not asked because the task's
    deadline passed or three asks in a row timed out; if no asked pod answered at all, the first
    failure is raised. A workload with no (running) pod is a fact: the rows are empty or
    unasked."""
    budget = budget or Budget()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows, failures = [], []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(
                unasked(pod, port, LIVENESS_UNASKED if kind == "liveness" else READINESS_UNASKED)
            )
            continue
        try:
            if kind == "liveness":
                row, failure = liveness_row(ctx, workload, pod, scheme, port, budget)
                if failure is not None:
                    failures.append((name, failure))
            else:
                row, failure = readiness_row(ctx, workload, pod, scheme, port, budget)
                if failure is not None:
                    failures.append((name, failure))
            rows.append(row)
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failures.append((name, exc))
            rows.append(
                unasked(pod, port, LIVENESS_UNASKED if kind == "liveness" else READINESS_UNASKED)
                | {"queried": True}
            )
    if asked and len(failures) == len(asked) and not any(answered(kind, r) for r in rows):
        name, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"pod/{name}") from exc
    for name, exc in failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"pod/{name}")
    report_unasked(ctx, pods, {r["name"] for r in rows if r["queried"]})
    good = "alive" if kind == "liveness" else "ready"
    asked_rows = sum(1 for r in rows if r["queried"])
    return {
        "workload": workload_summary(workload),
        "podCount": len(pods),
        "queriedCount": asked_rows,
        "unaskedCount": len(pods) - asked_rows,
        "truncated": len(pods) > limit,
        f"{good}Count": sum(1 for r in rows if r[good] is True),
        f"not{good.capitalize()}Count": sum(1 for r in rows if r[good] is False),
        "pods": rows,
    }
