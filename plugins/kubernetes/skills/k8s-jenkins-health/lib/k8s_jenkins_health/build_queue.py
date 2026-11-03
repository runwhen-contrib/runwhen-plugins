"""queue: the build queue and the executors of each Jenkins pod, from the anonymous JSON API."""

import json
from datetime import datetime, timezone

from k8s_jenkins_health.common import NESTED_LIMIT, cap, truncate
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.fetch import Budget, ask, pod_port, pod_ready, queried
from k8s_jenkins_health.tally import Tally

QUEUE = "/queue/api/json"
COMPUTERS = "/computer/api/json"
# only the fields read: the remote API answers with far more
QUEUE_TREE = "?tree=items[id,blocked,buildable,stuck,inQueueSince,why,task[name]]"
COMPUTER_TREE = (
    "?tree=busyExecutors,totalExecutors,"
    "computer[displayName,offline,temporarilyOffline,numExecutors,offlineCauseReason]"
)


def iso(millis):
    return (
        datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if millis is not None
        else None
    )


def parse_json(reply, what):
    try:
        return json.loads(reply.body)
    except ValueError:
        raise RwError("internal", f"{what} did not return JSON: {reply.body[:80]!r}") from None


def queue_items(doc, what):
    """The queue's items, each a dict; anything else (an HTML page, another product) is an
    `internal` error, never an empty queue."""
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise RwError("internal", f"{what} did not return a Jenkins queue document")
    return items


def read_queue(items, now, stuck_after, limit):
    """The queue facts: every item's age against `now`; the items older than `stuck_after`
    (seconds), oldest first and cut at `limit`, with their `why` text cut at 1024; and how many
    items Jenkins itself flags stuck or blocked. An `inQueueSince` that is not a number is an
    `internal` error."""
    aged, unknown, stuck_flag, blocked_flag = [], 0, 0, 0
    now_ms = now.timestamp() * 1000
    for item in items:
        stuck_flag += item.get("stuck") is True
        blocked_flag += item.get("blocked") is True
        since = item.get("inQueueSince")
        if since is None:
            unknown += 1
            continue
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            raise RwError("internal", f"queue item {item.get('id')} has a bad inQueueSince")
        age = (now_ms - since) / 1000
        if age > stuck_after:
            aged.append((age, item, since))
    aged.sort(key=lambda t: -t[0])
    rows = []
    for age, item, since in cap(aged, limit):
        why, was_cut = truncate(item.get("why"))
        task = item.get("task")
        rows.append(
            {
                "id": item.get("id"),
                "task": task.get("name") if isinstance(task, dict) else None,
                "inQueueSince": iso(since),
                "ageSeconds": round(age, 1),
                "stuck": item.get("stuck"),
                "blocked": item.get("blocked"),
                "buildable": item.get("buildable"),
                "why": why,
                "whyTruncated": was_cut,
            }
        )
    return {
        "itemCount": len(items),
        "flaggedStuckCount": stuck_flag,
        "flaggedBlockedCount": blocked_flag,
        "ageUnknownCount": unknown,
        "stuckAfterCount": len(aged),
        "stuckAfterItems": rows,
        "stuckAfterTruncated": len(aged) > limit,
    }


def read_computers(doc, what):
    """The executors: every offline computer, with its cause text cut at 1024. A document without
    a `computer` list of dicts is an `internal` error."""
    nodes = doc.get("computer") if isinstance(doc, dict) else None
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        raise RwError("internal", f"{what} did not return a Jenkins computer document")
    offline = []
    for node in nodes:
        if node.get("offline") is not True:
            continue
        cause, was_cut = truncate(node.get("offlineCauseReason"))
        offline.append(
            {
                "name": node.get("displayName"),
                "temporarilyOffline": node.get("temporarilyOffline"),
                "numExecutors": node.get("numExecutors"),
                "cause": cause or None,
                "causeTruncated": was_cut,
            }
        )
    return {
        "computerCount": len(nodes),
        "offlineCount": len(offline),
        "totalExecutors": doc.get("totalExecutors"),
        "busyExecutors": doc.get("busyExecutors"),
        "offline": cap(offline, NESTED_LIMIT),
        "offlineTruncated": len(offline) > NESTED_LIMIT,
    }


def pod_row(pod, port, probed, queue=None, computers=None):
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_ready(pod),
        "port": pod_port(pod, port),
        "queried": probed,
        "queue": queue,
        "computers": computers,
    }


def view(ctx, workload, pods, scheme, port, stuck_after, now, limit, budget=None, prefix=""):
    """The queue view. `stuck_after` is seconds, `now` the run's reference time. Pods that are not
    Running are listed and not asked. A failing endpoint is one error scoped
    `pod/<name>/<queue|computers>` and a null section; if nothing answered at all the first
    failure is raised, and if everything that answered wanted credentials the task is skipped."""
    budget, tally = budget or Budget(), Tally()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows = []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(pod_row(pod, port, False))
            continue
        sections = {}
        for key, path, tree in (
            ("queue", QUEUE, QUEUE_TREE),
            ("computers", COMPUTERS, COMPUTER_TREE),
        ):
            what = f"{prefix + path} on pod {name}"
            scope = f"pod/{name}/{key}"
            try:
                reply = ask(
                    ctx, workload, pod, scheme, pod_port(pod, port), prefix + path + tree, budget
                )
                if reply.status == 200:
                    doc = parse_json(reply, what)
                    sections[key] = (
                        read_queue(queue_items(doc, what), now, stuck_after, limit)
                        if key == "queue"
                        else read_computers(doc, what)
                    )
                else:
                    sections[key] = None
                    if reply.status not in (401, 403):
                        raise RwError("dependency", f"{what} answered HTTP {reply.status}")
            except RwError as exc:
                if exc.code == "permission_denied":
                    raise
                tally.fail(scope, exc)
                sections[key] = None
                continue
            tally.answer(reply, scope, what)
        rows.append(pod_row(pod, port, True, sections["queue"], sections["computers"]))
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
        "stuckAfterSeconds": stuck_after,
        "stuckAfterCount": sum((r["queue"] or {}).get("stuckAfterCount", 0) for r in rows),
        "offlineCount": sum((r["computers"] or {}).get("offlineCount", 0) for r in rows),
        "pods": rows,
    }
