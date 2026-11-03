"""http-status: whether each Jenkins pod serves /login, and the X-Jenkins version header."""

import re

from k8s_jenkins_health.common import cap
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.fetch import Budget, ask, pod_port, pod_ready, queried
from k8s_jenkins_health.tally import Tally

PATH = "/login"
_JENKINS = re.compile(r"jenkins", re.IGNORECASE)


def login_row(pod, port, probed, reply=None):
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_ready(pod),
        "port": pod_port(pod, port),
        "queried": probed,
        "httpStatus": reply.status if reply else None,
        "ok": None,
        "authRequired": bool(reply and reply.status in (401, 403)),
        "version": None,
        "headersExposed": None if reply is None else reply.headers_exposed,
    }


def read_login(pod, port, reply, what):
    """The row of a /login reply. 200 from something that is Jenkins (an X-Jenkins header, else
    the word in the page) is ok true; 503 is ok false; 404 is ok false too (nothing serves /login
    there); a 200 that is neither is an `internal` error, never ok."""
    row = login_row(pod, port, True, reply)
    row["version"] = reply.version
    if reply.status == 200:
        if reply.version is None and not _JENKINS.search(reply.body):
            raise RwError("internal", f"{what} answered 200 but is not a Jenkins login page")
        row["ok"] = True
    elif reply.status in (404, 503):
        row["ok"] = False
    return row


def view(ctx, workload, pods, scheme, port, limit, budget=None, prefix=""):
    """The http-status view. Pods that are not Running are listed and not asked. A failing pod is
    one error scoped to it; if no asked pod answered at all the first failure is raised, and if
    every answer wanted credentials the task is skipped."""
    budget, tally = budget or Budget(), Tally()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows = []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(login_row(pod, port, False))
            continue
        what = f"{prefix + PATH} on pod {name}"
        try:
            reply = ask(
                ctx, workload, pod, scheme, pod_port(pod, port), prefix + PATH, budget, headers=True
            )
            rows.append(read_login(pod, port, reply, what))
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            tally.fail(f"pod/{name}", exc)
            rows.append(login_row(pod, port, True))
            continue
        tally.answer(reply, f"pod/{name}", what)
    tally.finish(ctx, asked)
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
        "okCount": sum(1 for r in rows if r["ok"] is True),
        "notOkCount": sum(1 for r in rows if r["ok"] is False),
        "pods": rows,
    }
