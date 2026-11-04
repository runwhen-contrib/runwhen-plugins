"""ready: the /ready answer of each Loki pod."""

import re

from k8s_loki_health.common import MESSAGE_LIMIT, cap, truncate
from k8s_loki_health.errors import RwError
from k8s_loki_health.proxy import status_error
from k8s_loki_health.fetch import (
    Budget,
    fetch_one,
    pod_condition,
    pod_port,
    queried,
)

PATH = "/ready"
_READY = re.compile(r"\bready\b")


def ready_reply(reply, what):
    """(ready, body) from a /ready reply: a 200 whose text says ready is True; a 503 is False with
    its body, the reason Loki gives ("Ingester not ready: waiting for 15s after being ready"); a
    200 with other text (an auth page, an HTML error) or any other status is an error, and never
    reads as ready."""
    if reply.status == 503:
        return False, reply.body.strip() or None
    if reply.status != 200:
        raise status_error(reply, what)
    text = reply.body.strip()
    if _READY.search(text.lower()) and "not ready" not in text.lower():
        return True, text
    raise RwError("internal", f"{what} did not return the Loki ready text: {text[:80]!r}")


def row(pod, port, probed, ready=None, http_status=None, body=None):
    text, was_cut = truncate(body)
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_condition(pod, "Ready"),
        "port": pod_port(pod, port),
        "queried": probed,
        "ready": ready,
        "httpStatus": http_status,
        "body": text,
        "bodyTruncated": was_cut,
    }


def view(ctx, workload, pods, scheme, port, limit, budget=None):
    """The ready view. Pods that are not Running are listed and not asked. A failing pod is one
    error scoped to it (`ctx.error`), also a pod not asked because the task's deadline passed or
    three pods in a row timed out; if no asked pod answered at all, the first failure is raised.
    A workload with no (running) pod is a fact: the rows are empty or unqueried."""
    budget = budget or Budget()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows, failures = [], []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(row(pod, port, False))
            continue
        try:
            reply = fetch_one(ctx, workload, pod, scheme, port, PATH, budget)
            ready, body = ready_reply(reply, f"{PATH} on pod {name}")
            rows.append(row(pod, port, True, ready, reply.status, body))
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failures.append((name, exc))
            rows.append(row(pod, port, True))
    if asked and len(failures) == len(asked):
        name, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"pod/{name}") from exc
    for name, exc in failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"pod/{name}")
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    return {
        "workload": {
            "kind": workload["kind"],
            "name": workload["metadata"]["name"],
            "replicas": spec.get("replicas"),
            "readyReplicas": status.get("readyReplicas"),
        },
        "podCount": len(pods),
        "truncated": len(pods) > limit,
        "readyCount": sum(1 for r in rows if r["ready"] is True),
        "notReadyCount": sum(1 for r in rows if r["ready"] is False),
        "pods": rows,
    }
