"""queue: the build queue and the executors of each Jenkins pod, from the anonymous JSON API."""

import json
from datetime import datetime, timezone

from k8s_jenkins_health.common import NESTED_LIMIT, cap, cap_text, truncate
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.fetch import Budget, ask, no_pod_error, pod_endpoint, pod_ready, queried
from k8s_jenkins_health.tally import Tally

QUEUE = "/queue/api/json"
COMPUTERS = "/computer/api/json"
# only the fields read: the remote API answers with far more
QUEUE_TREE = (
    "?tree=discoverableItems[id],items[id,blocked,buildable,stuck,inQueueSince,why,task[name,url]]"
)
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


def discoverable_count(doc, what):
    """How many queue items the anonymous caller can see exist but not read (`discoverableItems`),
    None when the document has no such list."""
    found = doc.get("discoverableItems") if isinstance(doc, dict) else None
    if found is None:
        return None
    if not isinstance(found, list):
        raise RwError("internal", f"{what} has a discoverableItems that is not a list")
    return len(found)


def read_queue(items, now, stuck_after, limit, discoverable=None):
    """The queue facts: every item's age against `now`. Listed are the items Jenkins flags stuck,
    whatever their age, and the blocked or buildable items that have waited longer than
    `stuck_after` (seconds): flagged ones first, then oldest first, cut at `limit`, each with its
    `why` text cut at 1024 and its task's name and url. Also how many items Jenkins flags stuck or
    blocked. An `inQueueSince` that is not a number is an `internal` error."""
    listed, unknown, stuck_flag, blocked_flag = [], 0, 0, 0
    now_ms = now.timestamp() * 1000
    for item in items:
        stuck = item.get("stuck") is True
        stuck_flag += stuck
        blocked_flag += item.get("blocked") is True
        since, age = item.get("inQueueSince"), None
        if since is None:
            unknown += 1
        elif isinstance(since, bool) or not isinstance(since, (int, float)):
            raise RwError("internal", f"queue item {item.get('id')} has a bad inQueueSince")
        else:
            age = (now_ms - since) / 1000
        waiting = age is not None and age > stuck_after
        if stuck or (waiting and (item.get("blocked") is True or item.get("buildable") is True)):
            listed.append((age, item, since))
    listed.sort(key=lambda t: (t[1].get("stuck") is not True, t[0] is None, -(t[0] or 0)))
    rows = []
    for age, item, since in cap(listed, limit):
        why, was_cut = truncate(item.get("why"))
        task = item.get("task")
        task = task if isinstance(task, dict) else {}
        rows.append(
            {
                "id": cap_text(item.get("id")),
                "task": cap_text(task.get("name")),
                "taskUrl": cap_text(task.get("url")),
                "inQueueSince": iso(since),
                "ageSeconds": round(age, 1) if age is not None else None,
                "stuck": item.get("stuck"),
                "blocked": item.get("blocked"),
                "buildable": item.get("buildable"),
                "why": why,
                "whyTruncated": was_cut,
            }
        )
    return {
        "itemCount": len(items),
        "discoverableCount": discoverable,
        "flaggedStuckCount": stuck_flag,
        "flaggedBlockedCount": blocked_flag,
        "ageUnknownCount": unknown,
        "stuckAfterCount": len(listed),
        "stuckAfterItems": rows,
        "stuckAfterTruncated": len(listed) > limit,
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
                "name": cap_text(node.get("displayName")),
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


def pod_row(pod, port, probed, queue=None, computers=None, scheme=None, prefix=None):
    endpoint = pod_endpoint(pod, port, scheme, prefix)
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_ready(pod),
        "port": endpoint.port,
        "scheme": endpoint.scheme,
        "urlPrefix": endpoint.prefix,
        "queried": probed,
        "queue": queue,
        "computers": computers,
    }


def view(ctx, workload, pods, scheme, port, stuck_after, now, limit, budget=None, prefix=None):
    """The queue view. `stuck_after` is seconds, `now` the run's reference time; `scheme`, `port`
    and `prefix` are inputs, None meaning the pod's readiness probe decides. Pods that are not
    Running are listed and not asked. A failing endpoint is one error scoped
    `pod/<name>/<queue|computers>` and a null section; if nothing answered at all the first
    failure is raised, and if everything that answered wanted credentials the task is skipped."""
    budget, tally = budget or Budget(), Tally()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows = []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(pod_row(pod, port, False, scheme=scheme, prefix=prefix))
            continue
        endpoint = pod_endpoint(pod, port, scheme, prefix)
        sections = {}
        for key, path, tree in (
            ("queue", QUEUE, QUEUE_TREE),
            ("computers", COMPUTERS, COMPUTER_TREE),
        ):
            what = f"{endpoint.prefix + path} on pod {name}"
            scope = f"pod/{name}/{key}"
            try:
                reply = ask(ctx, workload, pod, endpoint, endpoint.prefix + path + tree, budget)
                if reply.status == 200:
                    doc = parse_json(reply, what)
                    sections[key] = (
                        read_queue(
                            queue_items(doc, what),
                            now,
                            stuck_after,
                            limit,
                            discoverable_count(doc, what),
                        )
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
        rows.append(
            pod_row(pod, port, True, sections["queue"], sections["computers"], scheme, prefix)
        )
    tally.finish(ctx, asked)
    no_pod_error(ctx, workload, asked)
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
