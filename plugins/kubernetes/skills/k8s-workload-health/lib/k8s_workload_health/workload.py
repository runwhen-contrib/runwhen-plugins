"""Pure functions behind the k8s-workload-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

import json
import re
from datetime import UTC, datetime, timedelta

from k8s_workload_health.k8s import RwError

DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"
DAEMONSET = "DaemonSet"
KINDS = (DEPLOYMENT, STATEFULSET, DAEMONSET)

TEXT_MAX = 1024  # longest free-text field returned
NESTED_MAX = 50  # longest nested list returned inside an entry
DEFAULT_HPA_CPU_TARGET = 80  # what the autoscaler controller uses when spec.metrics is empty

_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_DURATION_FULL = re.compile(r"(?:\d+(?:ms|s|m|h|d))+")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


# ---------------------------------------------------------------- time ----


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' (contract pattern) -> timedelta.
    Anything else raises RwError(invalid_input)."""
    if isinstance(text, timedelta):
        return text
    s = str(text)
    if not _DURATION_FULL.fullmatch(s):
        raise RwError("invalid_input", f"not a duration: {text!r} (use e.g. 90s, 30m, 1h, 2d)")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION.findall(s)))


def parse_time(value):
    """RFC 3339 string (as the Kubernetes API writes it) -> aware datetime, or None."""
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def fmt_time(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _within(dt, since, now):
    return dt is not None and dt >= now - parse_duration(since)


# -------------------------------------------------------------- bounds ----


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


def _text(entry, key, value):
    """entry[key] = value cut to TEXT_MAX chars; entry["truncated"] = True if cut."""
    if isinstance(value, str) and len(value) > TEXT_MAX:
        value = value[:TEXT_MAX]
        entry["truncated"] = True
    entry[key] = value


def _nested(entry, key, items):
    """entry[key] = items bounded to NESTED_MAX; entry["truncated"] = True if cut."""
    items = list(items)
    if len(items) > NESTED_MAX:
        entry["truncated"] = True
    entry[key] = items[:NESTED_MAX]


# ----------------------------------------------------------- selectors ----


def selector_string(selector):
    """Label selector (matchLabels + matchExpressions) -> kubectl -l string.

    An empty selector would select every pod in the namespace, so it raises.
    """
    selector = selector or {}
    terms = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], ",".join(expr.get("values") or [])
        if op == "In":
            terms.append(f"{key} in ({values})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({values})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            raise RwError("invalid_input", f"unknown selector operator {op!r}")
    if not terms:
        raise RwError(
            "invalid_input", "workload has an empty selector; refusing to select every pod"
        )
    return ",".join(terms)


def matches_selector(labels, selector):
    labels = labels or {}
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


# ------------------------------------------------------------ ownership ----


def _controller_uids(obj):
    return {
        ref.get("uid")
        for ref in (obj.get("metadata", {}).get("ownerReferences") or [])
        if ref.get("controller")
    }


def owned_revisions(kind, workload, items):
    """ReplicaSets (Deployment) or ControllerRevisions (StatefulSet/DaemonSet)
    whose controller is this workload."""
    uid = workload["metadata"]["uid"]
    return [i for i in items if uid in _controller_uids(i)]


def owned_pods(kind, workload, replicasets_or_revisions, pods):
    """Pods that match the workload's selector AND hang off it through the
    ownerReferences chain (Deployment -> ReplicaSet -> Pod; StatefulSet or
    DaemonSet -> Pod). A pod with the right labels but another controller is
    not this workload's pod. The second argument matters only for a Deployment."""
    selector = workload["spec"]["selector"]
    if kind == DEPLOYMENT:
        owners = {
            r["metadata"]["uid"] for r in owned_revisions(kind, workload, replicasets_or_revisions)
        }
    else:
        owners = {workload["metadata"]["uid"]}
    return [
        p
        for p in pods
        if matches_selector(p["metadata"].get("labels"), selector) and _controller_uids(p) & owners
    ]


def pvc_names(pods):
    """Names of the PersistentVolumeClaims mounted by these pods."""
    names = set()
    for p in pods:
        for v in p.get("spec", {}).get("volumes") or []:
            claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                names.add(claim)
    return names


def pod_uids(pods):
    """{pod name: uid} of the current pods."""
    return {p["metadata"]["name"]: p["metadata"].get("uid") for p in pods}


def related_names(kind, workload, revisions, pods):
    """{involvedObject kind: names} for the workload, its ReplicaSets or
    ControllerRevisions, its pods and the PVCs those pods mount."""
    revision_kind = "ReplicaSet" if kind == DEPLOYMENT else "ControllerRevision"
    return {
        kind: {workload["metadata"]["name"]},
        revision_kind: {r["metadata"]["name"] for r in revisions},
        "Pod": {p["metadata"]["name"] for p in pods},
        "PersistentVolumeClaim": pvc_names(pods),
    }


# -------------------------------------------------------- rollout status ----


def _conditions(obj):
    out = []
    for c in obj.get("status", {}).get("conditions") or []:
        entry = {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "lastTransitionTime": c.get("lastTransitionTime"),
        }
        _text(entry, "message", c.get("message"))
        out.append(entry)
    return out


def rollout_status(kind, obj):
    """Replica counts, rollout conditions and strategy of the workload."""
    spec, status = obj.get("spec", {}), obj.get("status", {})
    meta = obj["metadata"]
    if kind == DAEMONSET:
        desired = status.get("desiredNumberScheduled", 0)
        replicas = status.get("currentNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        updated = status.get("updatedNumberScheduled", 0)
        available = status.get("numberAvailable", 0)
        unavailable = status.get("numberUnavailable", 0)
        strategy = (spec.get("updateStrategy") or {}).get("type")
        partition = None
    else:
        desired = spec.get("replicas", 1)
        replicas = status.get("replicas", 0)
        ready = status.get("readyReplicas", 0)
        updated = status.get("updatedReplicas", 0)
        available = status.get("availableReplicas", 0)
        if kind == DEPLOYMENT:
            unavailable = status.get("unavailableReplicas", 0)
            strategy = (spec.get("strategy") or {}).get("type")
            partition = None
        else:
            unavailable = max(desired - available, 0)
            update = spec.get("updateStrategy") or {}
            strategy = update.get("type")
            partition = (update.get("rollingUpdate") or {}).get("partition")
    conditions = _conditions(obj)
    generation = meta.get("generation")
    observed = status.get("observedGeneration")
    return {
        "kind": kind,
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "generation": generation,
        "observedGeneration": observed,
        "generationObserved": observed is not None
        and generation is not None
        and observed >= generation,
        "paused": bool(spec.get("paused", False)),
        "desired": desired,
        "replicas": replicas,
        "ready": ready,
        "updated": updated,
        "available": available,
        "unavailable": unavailable,
        "misscheduled": status.get("numberMisscheduled", 0) if kind == DAEMONSET else None,
        "updateStrategy": strategy,
        "partition": partition,
        "currentRevision": status.get("currentRevision") if kind == STATEFULSET else None,
        "updateRevision": status.get("updateRevision") if kind == STATEFULSET else None,
        "progressDeadlineSeconds": spec.get("progressDeadlineSeconds")
        if kind == DEPLOYMENT
        else None,
        "progressDeadlineExceeded": any(
            c["type"] == "Progressing" and c["reason"] == "ProgressDeadlineExceeded"
            for c in conditions
        ),
        "conditions": conditions,
    }


# ------------------------------------------------------------- revisions ----

_REVISION = "deployment.kubernetes.io/revision"
_REVISION_HISTORY = "deployment.kubernetes.io/revision-history"


def _revision_view(kind, item):
    """One ReplicaSet or ControllerRevision: revision number, pod template, ..."""
    meta = item["metadata"]
    annotations = meta.get("annotations") or {}
    earlier = []
    if kind == DEPLOYMENT:
        number = annotations.get(_REVISION)
        template = item.get("spec", {}).get("template", {})
        replicas = item.get("spec", {}).get("replicas")
        ready = item.get("status", {}).get("readyReplicas", 0)
        earlier = [int(n) for n in annotations.get(_REVISION_HISTORY, "").split(",") if n.strip()]
    else:
        number = item.get("revision")
        template = ((item.get("data") or {}).get("spec") or {}).get("template", {})
        replicas = None
        ready = None
    return {
        "name": meta["name"],
        "revision": int(number) if number is not None else None,
        "created": meta.get("creationTimestamp"),
        "template": template,
        "podSpec": template.get("spec", {}),
        "replicas": replicas,
        "ready": ready,
        "earlier": earlier,
    }


def _images(pod_spec):
    return {c["name"]: c.get("image") for c in pod_spec.get("containers") or []}


def _newest(views):
    numbers = [v["revision"] for v in views if v["revision"] is not None]
    return max(numbers) if numbers else None


def revision_history(kind, revisions):
    """Revisions of the workload, newest first: number, creation time, images,
    whether it is the current (highest) revision, whether a Deployment's
    ReplicaSet was re-used by a rollback (`reused`, with the revision numbers it
    held before), and the replicas each ReplicaSet still runs."""
    views = [_revision_view(kind, r) for r in revisions]
    views.sort(key=lambda v: (v["revision"] is not None, v["revision"] or 0), reverse=True)
    newest = _newest(views)
    return [
        {
            "name": v["name"],
            "revision": v["revision"],
            "current": v["revision"] is not None and v["revision"] == newest,
            "reused": bool(v["earlier"]),
            "earlierRevisions": v["earlier"],
            "created": v["created"],
            "images": _images(v["podSpec"]),
            "replicas": v["replicas"],
            "ready": v["ready"],
        }
        for v in views
    ]


# -------------------------------------------------------------- restarts ----


def _container_statuses(pod):
    status = pod.get("status", {})
    for s in status.get("initContainerStatuses") or []:
        yield True, s
    for s in status.get("containerStatuses") or []:
        yield False, s


def _state(state):
    for name in ("running", "waiting", "terminated"):
        if name in (state or {}):
            return name, state[name]
    return None, {}


def pod_restarts(pods, since, now, threshold):
    """Containers that restarted at least once: count, why the last run ended,
    and whether it ended within `since`. `exceedsThreshold` is restartCount >=
    threshold (the restartThreshold setting)."""
    out = []
    for pod in pods:
        for is_init, s in _container_statuses(pod):
            count = s.get("restartCount", 0)
            if count < 1:
                continue
            last = (s.get("lastState") or {}).get("terminated")
            finished = parse_time(last.get("finishedAt")) if last else None
            state_name, state = _state(s.get("state"))
            out.append(
                {
                    "pod": pod["metadata"]["name"],
                    "container": s["name"],
                    "init": is_init,
                    "restartCount": count,
                    "ready": bool(s.get("ready", False)),
                    "state": state_name,
                    "waitingReason": state.get("reason") if state_name == "waiting" else None,
                    "lastTerminated": (
                        {
                            "reason": last.get("reason"),
                            "exitCode": last.get("exitCode"),
                            "signal": last.get("signal"),
                            "startedAt": last.get("startedAt"),
                            "finishedAt": last.get("finishedAt"),
                        }
                        if last
                        else None
                    ),
                    "recent": _within(finished, since, now),
                    "exceedsThreshold": count >= int(threshold),
                }
            )
    out.sort(key=lambda r: (-r["restartCount"], r["pod"], r["container"]))
    return out


# ---------------------------------------------------------------- pods ----


def _condition(pod, ctype):
    for c in pod.get("status", {}).get("conditions") or []:
        if c.get("type") == ctype:
            return c
    return None


def pod_status(pods):
    """Per pod: phase, readiness, scheduling, and why containers (init ones
    included, flagged `init`) are waiting or terminated."""
    out = []
    for pod in pods:
        status = pod.get("status", {})
        scheduled = _condition(pod, "PodScheduled")
        ready = _condition(pod, "Ready")
        waiting, terminated = [], []
        counts = {True: [0, 0], False: [0, 0]}  # init -> [ready, total]
        for is_init, s in _container_statuses(pod):
            counts[is_init][1] += 1
            counts[is_init][0] += 1 if s.get("ready") else 0
            name, state = _state(s.get("state"))
            if name == "waiting":
                entry = {"container": s["name"], "reason": state.get("reason"), "init": is_init}
                _text(entry, "message", state.get("message"))
                waiting.append(entry)
            elif name == "terminated":
                terminated.append(
                    {
                        "container": s["name"],
                        "reason": state.get("reason"),
                        "exitCode": state.get("exitCode"),
                        "init": is_init,
                    }
                )
        waiting.sort(key=lambda e: not e["init"])  # init containers first (stable)
        terminated.sort(key=lambda e: not e["init"])
        unscheduled = scheduled is not None and scheduled.get("status") == "False"
        entry = {
            "name": pod["metadata"]["name"],
            "phase": status.get("phase"),
            "reason": status.get("reason"),
            "ready": ready is not None and ready.get("status") == "True",
            "containersReady": counts[False][0],
            "containersTotal": counts[False][1] or len(pod.get("spec", {}).get("containers") or []),
            "initContainersReady": counts[True][0],
            "initContainersTotal": counts[True][1],
            "node": pod.get("spec", {}).get("nodeName"),
            "scheduled": None if scheduled is None else scheduled.get("status") == "True",
            "unschedulable": None,
            "waiting": waiting,
            "terminated": terminated,
            "startTime": status.get("startTime"),
            "deletionTimestamp": pod["metadata"].get("deletionTimestamp"),
        }
        _text(entry, "message", status.get("message"))
        if unscheduled:
            entry["unschedulable"] = {"reason": scheduled.get("reason")}
            _text(entry["unschedulable"], "message", scheduled.get("message"))
        out.append(entry)
    out.sort(key=lambda p: (p["ready"], p["name"]))
    return out


def pod_counts(pods):
    phases = {}
    ready = 0
    for p in pods:
        phase = p.get("status", {}).get("phase") or "Unknown"
        phases[phase] = phases.get(phase, 0) + 1
        cond = _condition(p, "Ready")
        ready += 1 if cond is not None and cond.get("status") == "True" else 0
    return {"total": len(pods), "ready": ready, "notReady": len(pods) - ready, "byPhase": phases}


# -------------------------------------------------------------- events ----


def _event_times(ev):
    series = ev.get("series") or {}
    meta = ev.get("metadata", {})
    last = (
        parse_time(series.get("lastObservedTime"))
        or parse_time(ev.get("lastTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(ev.get("firstTimestamp"))
        or parse_time(meta.get("creationTimestamp"))
    )
    first = (
        parse_time(ev.get("firstTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(meta.get("creationTimestamp"))
        or last
    )
    return first, last


def warning_events(events, names_by_kind, since, now, max_items, pod_uids=None):
    """Warning events about the named objects whose last occurrence falls in
    the window, grouped by object and reason (newest first, capped).

    `count` is the sum of the events' own occurrence counters, which are
    lifetime counts: an event last seen in the window may have started earlier.
    When `pod_uids` ({name: uid}) is given, an event about a Pod carrying a
    different uid (an earlier incarnation of the same name) is ignored.
    """
    groups = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        obj = ev.get("involvedObject") or {}
        if obj.get("name") not in names_by_kind.get(obj.get("kind"), ()):
            continue
        if pod_uids is not None and obj.get("kind") == "Pod":
            wanted = pod_uids.get(obj.get("name"))
            if obj.get("uid") and wanted and obj["uid"] != wanted:
                continue
        first, last = _event_times(ev)
        if not _within(last, since, now):
            continue
        series = ev.get("series") or {}
        count = series.get("count") or ev.get("count") or 1
        key = (obj.get("kind"), obj.get("name"), ev.get("reason"))
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "kind": obj.get("kind"),
                "name": obj.get("name"),
                "reason": ev.get("reason"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "reportingComponent": ev.get("reportingComponent")
                or (ev.get("source") or {}).get("component"),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last and (g["lastSeen"] is None or last >= g["lastSeen"]):
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["name"]), reverse=True)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        _text(g, "message", g["message"])
    return cap(rows, max_items)


# -------------------------------------------------------------- probes ----

_PROBES = (
    ("liveness", "livenessProbe"),
    ("readiness", "readinessProbe"),
    ("startup", "startupProbe"),
)


def probe_config(pod_template):
    """Every container x {liveness, readiness, startup}: present or not, the
    handler, its port and whether that port is declared in the container's
    `ports` (by number or by name), and the timings. Native sidecars (init
    containers with restartPolicy Always, the only init containers that can
    have probes) are included with `init: true`. An exec probe reports only its
    program (`command[0]`) and `argCount`, never the arguments."""
    spec = pod_template.get("spec", {})
    containers = [(False, c) for c in spec.get("containers") or []]
    containers += [
        (True, c) for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"
    ]
    out = []
    for is_init, c in containers:
        ports = c.get("ports") or []
        by_number = {p.get("containerPort") for p in ports}
        by_name = {p["name"]: p.get("containerPort") for p in ports if p.get("name")}
        for label, field in _PROBES:
            probe = c.get(field)
            entry = {"container": c["name"], "init": is_init, "probe": label}
            entry["present"] = probe is not None
            if probe is None:
                out.append(entry)
                continue
            handler = next(
                (h for h in ("httpGet", "tcpSocket", "grpc", "exec") if h in probe), None
            )
            port = None
            if handler in ("httpGet", "tcpSocket", "grpc"):
                port = (probe.get(handler) or {}).get("port")
            declared = resolved = None
            if port is not None:
                if isinstance(port, int):
                    declared, resolved = port in by_number, port
                else:
                    declared, resolved = port in by_name, by_name.get(port)
            argv = (probe.get("exec") or {}).get("command") if handler == "exec" else None
            entry.update(
                {
                    "handler": handler,
                    "port": port,
                    "portDeclared": declared,
                    "resolvedPort": resolved,
                    "path": (probe.get("httpGet") or {}).get("path"),
                    "command": argv[0] if argv else None,
                    "argCount": len(argv) - 1 if argv else None,
                    "initialDelaySeconds": probe.get("initialDelaySeconds", 0),
                    "periodSeconds": probe.get("periodSeconds", 10),
                    "timeoutSeconds": probe.get("timeoutSeconds", 1),
                    "successThreshold": probe.get("successThreshold", 1),
                    "failureThreshold": probe.get("failureThreshold", 3),
                }
            )
            out.append(entry)
    return out


# ------------------------------------------------------ config changes ----

_COMPARED_CONTAINER_KEYS = {"image", "env", "resources", "name"}


def _env(container):
    return {e["name"]: e for e in container.get("env") or []}


def _diff_templates(prev, cur):
    """What changed between two pod templates, by name only (never a value)."""
    prev_spec, cur_spec = prev.get("spec", {}), cur.get("spec", {})
    prev_c = {c["name"]: c for c in prev_spec.get("containers") or []}
    cur_c = {c["name"]: c for c in cur_spec.get("containers") or []}
    diff = {
        "imageChanges": [],
        "envAdded": [],
        "envRemoved": [],
        "envChanged": [],
        "resourceChanges": [],
        "containersAdded": sorted(set(cur_c) - set(prev_c)),
        "containersRemoved": sorted(set(prev_c) - set(cur_c)),
    }
    other = []
    for name in sorted(set(prev_c) & set(cur_c)):
        a, b = prev_c[name], cur_c[name]
        if a.get("image") != b.get("image"):
            diff["imageChanges"].append(
                {"container": name, "from": a.get("image"), "to": b.get("image")}
            )
        ea, eb = _env(a), _env(b)
        for env in sorted(set(eb) - set(ea)):
            diff["envAdded"].append({"container": name, "name": env})
        for env in sorted(set(ea) - set(eb)):
            diff["envRemoved"].append({"container": name, "name": env})
        for env in sorted(set(ea) & set(eb)):
            if ea[env] != eb[env]:
                diff["envChanged"].append({"container": name, "name": env})
        if (a.get("resources") or {}) != (b.get("resources") or {}):
            diff["resourceChanges"].append(
                {
                    "container": name,
                    "from": a.get("resources") or {},
                    "to": b.get("resources") or {},
                }
            )
        for key in sorted((set(a) | set(b)) - _COMPARED_CONTAINER_KEYS):
            if a.get(key) != b.get(key):
                other.append(f"containers.{name}.{key}")
    for key in sorted((set(prev_spec) | set(cur_spec)) - {"containers"}):
        if prev_spec.get(key) != cur_spec.get(key):
            other.append(key)
    pa = (prev.get("metadata") or {}).get("annotations") or {}
    ca = (cur.get("metadata") or {}).get("annotations") or {}
    for key in sorted(k for k in set(pa) | set(ca) if pa.get(k) != ca.get(k)):
        other.append(f"annotations.{key}")
    diff["otherPodSpecChanged"] = sorted(other)
    return diff


def _progressing_update(workload):
    for c in (workload or {}).get("status", {}).get("conditions") or []:
        if c.get("type") == "Progressing":
            return parse_time(c.get("lastUpdateTime"))
    return None


def config_changes(kind, workload, revisions, since, now):
    """Revisions in the window, each compared with the revision before it
    (the next lower revision number): images, env var names added, removed or
    changed (never values), resources, containers, and the names of any other
    pod-spec field or template annotation that differs (for example
    `annotations.kubectl.kubernetes.io/restartedAt`).

    A revision is in the window when it was created in it (`windowBasis:
    created`). A Deployment rollback re-uses an old ReplicaSet, whose creation
    time stays old but which carries the new revision number and a
    `revision-history` annotation: that current revision is also included when
    the Deployment's Progressing condition was updated in the window
    (`windowBasis: progressing`). ControllerRevisions have no such timestamp, so
    a StatefulSet or DaemonSet rollback to an existing revision is not visible.
    """
    views = [_revision_view(kind, r) for r in revisions]
    views = [v for v in views if v["revision"] is not None]
    views.sort(key=lambda v: v["revision"])
    newest = _newest(views)
    progressing = _progressing_update(workload) if kind == DEPLOYMENT else None
    out = []
    for i, v in enumerate(views):
        basis = None
        if _within(parse_time(v["created"]), since, now):
            basis = "created"
        elif v["earlier"] and v["revision"] == newest and _within(progressing, since, now):
            basis = "progressing"
        if basis is None:
            continue
        prev = views[i - 1] if i else None
        entry = {
            "revision": v["revision"],
            "name": v["name"],
            "current": v["revision"] == newest,
            "reused": bool(v["earlier"]),
            "windowBasis": basis,
            "created": v["created"],
            "previousRevision": prev["revision"] if prev else None,
            "images": _images(v["podSpec"]),
        }
        diff = _diff_templates(prev["template"], v["template"]) if prev else _diff_templates({}, {})
        for key, value in diff.items():
            if key in ("envAdded", "envRemoved", "envChanged", "otherPodSpecChanged"):
                _nested(entry, key, value)
            else:
                entry[key] = value
        out.append(entry)
    out.sort(key=lambda e: e["revision"], reverse=True)
    return out


# ----------------------------------------------------------------- HPA ----


def _target_value(target):
    for key in ("averageUtilization", "averageValue", "value"):
        if key in target:
            return key, target[key]
    return None, None


def _metric_identity(m):
    """(type, name, container, selector/describedObject key, body) of a metric spec or status."""
    kind = m.get("type")
    body = m.get(kind[0].lower() + kind[1:]) or {}
    metric = body.get("metric") or {}
    name = body.get("name") or metric.get("name")
    described = body.get("describedObject") or {}
    discriminator = json.dumps(
        [metric.get("selector"), [described.get("kind"), described.get("name")]], sort_keys=True
    )
    return kind, name, body.get("container"), discriminator, body


def hpa_status(hpas, kind, name, pod_template):
    """HPAs whose scaleTargetRef is this workload: bounds, current and desired
    replicas, conditions, metrics, and whether a Utilization metric targets a
    container that has no resource request (the HPA cannot compute it then; a
    LimitRange default request makes this a false positive). With no metrics the
    controller falls back to `defaultCpuTarget` percent CPU utilization."""
    containers = pod_template.get("spec", {}).get("containers") or []

    def without_request(resource, container=None):
        scope = [c for c in containers if container is None or c["name"] == container]
        return [
            c["name"]
            for c in scope
            if resource not in ((c.get("resources") or {}).get("requests") or {})
        ]

    out = []
    for h in hpas:
        ref = h.get("spec", {}).get("scaleTargetRef") or {}
        if ref.get("kind") != kind or ref.get("name") != name:
            continue
        spec, status = h["spec"], h.get("status", {})
        current = {}
        for m in status.get("currentMetrics") or []:
            ident = _metric_identity(m)
            current[ident[:4]] = ident[4].get("current") or {}
        metrics, missing_any = [], False
        for m in spec.get("metrics") or []:
            k, n, container, discr, body = _metric_identity(m)
            _, target_value = _target_value(body.get("target") or {})
            _, cur_value = _target_value(current.get((k, n, container, discr), {}))
            missing = []
            if (
                k in ("Resource", "ContainerResource")
                and (body.get("target") or {}).get("type") == "Utilization"
            ):
                missing = without_request(n, container if k == "ContainerResource" else None)
            missing_any = missing_any or bool(missing)
            metrics.append(
                {
                    "type": k,
                    "name": n,
                    "container": container,
                    "targetType": body.get("target", {}).get("type"),
                    "target": target_value,
                    "current": cur_value,
                    "containersWithoutRequests": missing,
                }
            )
        default_target = None
        if not spec.get("metrics"):
            default_target = DEFAULT_HPA_CPU_TARGET
            missing_any = bool(without_request("cpu"))
        min_r, max_r = spec.get("minReplicas", 1), spec.get("maxReplicas")
        cur_r, des_r = status.get("currentReplicas", 0), status.get("desiredReplicas")
        entry = {
            "name": h["metadata"]["name"],
            "minReplicas": min_r,
            "maxReplicas": max_r,
            "currentReplicas": cur_r,
            "desiredReplicas": des_r,
            "atMax": None if des_r is None or max_r is None else des_r >= max_r,
            "atMin": None if des_r is None else des_r <= min_r,
            "lastScaleTime": status.get("lastScaleTime"),
            "conditions": _conditions(h),
            "defaultCpuTarget": default_target,
            "resourceMetricWithoutRequests": missing_any,
        }
        _nested(entry, "metrics", metrics)
        out.append(entry)
    return out
