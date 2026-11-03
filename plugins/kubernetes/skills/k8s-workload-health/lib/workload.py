"""Pure functions behind the k8s-workload-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

import re
from datetime import datetime, timedelta, timezone

DEPLOYMENT = "Deployment"
STATEFULSET = "StatefulSet"
DAEMONSET = "DaemonSet"
KINDS = (DEPLOYMENT, STATEFULSET, DAEMONSET)

_DURATION = re.compile(r"(\d+)\s*([smhdw])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# ---------------------------------------------------------------- time ----


def parse_duration(text):
    """'90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise ValueError(f"not a duration: {text!r}")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))


def parse_time(value):
    """RFC 3339 string (as the Kubernetes API writes it) -> aware datetime, or None."""
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _within(dt, since, now):
    return dt is not None and dt >= now - parse_duration(since)


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


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
            raise ValueError(f"unknown selector operator {op!r}")
    if not terms:
        raise ValueError("workload has an empty selector")
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
    not this workload's pod."""
    selector = workload["spec"]["selector"]
    if kind == DEPLOYMENT:
        owners = {r["metadata"]["uid"] for r in owned_revisions(kind, workload, replicasets_or_revisions)}
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
    return [
        {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": c.get("message"),
            "lastTransitionTime": c.get("lastTransitionTime"),
        }
        for c in obj.get("status", {}).get("conditions") or []
    ]


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
        "generationObserved": observed is not None and generation is not None and observed >= generation,
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
        "progressDeadlineSeconds": spec.get("progressDeadlineSeconds") if kind == DEPLOYMENT else None,
        "progressDeadlineExceeded": any(
            c["type"] == "Progressing" and c["reason"] == "ProgressDeadlineExceeded" for c in conditions
        ),
        "conditions": conditions,
    }


# ------------------------------------------------------------- revisions ----


def _revision_view(kind, item):
    """One ReplicaSet or ControllerRevision as (revision number, pod spec, ...)."""
    meta = item["metadata"]
    if kind == DEPLOYMENT:
        number = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        pod_spec = item.get("spec", {}).get("template", {}).get("spec", {})
        replicas = item.get("spec", {}).get("replicas")
        ready = item.get("status", {}).get("readyReplicas", 0)
    else:
        number = item.get("revision")
        pod_spec = ((item.get("data") or {}).get("spec") or {}).get("template", {}).get("spec", {})
        replicas = None
        ready = None
    return {
        "name": meta["name"],
        "revision": int(number) if number is not None else None,
        "created": meta.get("creationTimestamp"),
        "podSpec": pod_spec,
        "replicas": replicas,
        "ready": ready,
    }


def _images(pod_spec):
    return {c["name"]: c.get("image") for c in pod_spec.get("containers") or []}


def revision_history(kind, revisions):
    """Revisions of the workload, newest first: number, creation time, images,
    and for a Deployment the replicas each ReplicaSet still runs."""
    views = [_revision_view(kind, r) for r in revisions]
    views.sort(key=lambda v: (v["revision"] is not None, v["revision"] or 0), reverse=True)
    return [
        {
            "name": v["name"],
            "revision": v["revision"],
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
    """Per pod: phase, readiness, scheduling, and why containers are waiting."""
    out = []
    for pod in pods:
        status = pod.get("status", {})
        scheduled = _condition(pod, "PodScheduled")
        ready = _condition(pod, "Ready")
        waiting, terminated, total, ready_count = [], [], 0, 0
        for _init, s in _container_statuses(pod):
            if _init:
                continue
            total += 1
            ready_count += 1 if s.get("ready") else 0
            name, state = _state(s.get("state"))
            if name == "waiting":
                waiting.append(
                    {"container": s["name"], "reason": state.get("reason"), "message": state.get("message")}
                )
            elif name == "terminated":
                terminated.append(
                    {"container": s["name"], "reason": state.get("reason"), "exitCode": state.get("exitCode")}
                )
        unscheduled = scheduled is not None and scheduled.get("status") == "False"
        out.append(
            {
                "name": pod["metadata"]["name"],
                "phase": status.get("phase"),
                "reason": status.get("reason"),
                "message": status.get("message"),
                "ready": ready is not None and ready.get("status") == "True",
                "containersReady": ready_count,
                "containersTotal": total or len(pod.get("spec", {}).get("containers") or []),
                "node": pod.get("spec", {}).get("nodeName"),
                "scheduled": None if scheduled is None else scheduled.get("status") == "True",
                "unschedulable": (
                    {"reason": scheduled.get("reason"), "message": scheduled.get("message")}
                    if unscheduled
                    else None
                ),
                "waiting": waiting,
                "terminated": terminated,
                "startTime": status.get("startTime"),
                "deletionTimestamp": pod["metadata"].get("deletionTimestamp"),
            }
        )
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


def warning_events(events, names_by_kind, since, now, max_items):
    """Warning events about the named objects whose last occurrence falls in
    the window, grouped by object and reason (newest first, capped).

    `count` is the sum of the events' own occurrence counters, which are
    lifetime counts: an event last seen in the window may have started earlier.
    """
    groups = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        obj = ev.get("involvedObject") or {}
        if obj.get("name") not in names_by_kind.get(obj.get("kind"), ()):
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
                "reportingComponent": ev.get("reportingComponent") or (ev.get("source") or {}).get("component"),
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
    return cap(rows, max_items)


# -------------------------------------------------------------- probes ----

_PROBES = (("liveness", "livenessProbe"), ("readiness", "readinessProbe"), ("startup", "startupProbe"))


def probe_config(pod_template):
    """Every container x {liveness, readiness, startup}: present or not, the
    handler, its port and whether that port is declared in the container's
    `ports` (by number or by name), and the timings."""
    out = []
    for c in pod_template.get("spec", {}).get("containers") or []:
        ports = c.get("ports") or []
        by_number = {p.get("containerPort") for p in ports}
        by_name = {p["name"]: p.get("containerPort") for p in ports if p.get("name")}
        for label, field in _PROBES:
            probe = c.get(field)
            entry = {"container": c["name"], "probe": label, "present": probe is not None}
            if probe is None:
                out.append(entry)
                continue
            handler = next((h for h in ("httpGet", "tcpSocket", "grpc", "exec") if h in probe), None)
            port = (probe.get(handler) or {}).get("port") if handler in ("httpGet", "tcpSocket", "grpc") else None
            declared = resolved = None
            if port is not None:
                if isinstance(port, int):
                    declared, resolved = port in by_number, port
                else:
                    declared, resolved = port in by_name, by_name.get(port)
            entry.update(
                {
                    "handler": handler,
                    "port": port,
                    "portDeclared": declared,
                    "resolvedPort": resolved,
                    "path": (probe.get("httpGet") or {}).get("path"),
                    "command": (probe.get("exec") or {}).get("command"),
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


def _env_names(container):
    return {e["name"] for e in container.get("env") or []}


def _diff_specs(prev, cur):
    """What changed between two pod specs, container by container."""
    prev_c = {c["name"]: c for c in prev.get("containers") or []}
    cur_c = {c["name"]: c for c in cur.get("containers") or []}
    diff = {
        "imageChanges": [],
        "envAdded": [],
        "envRemoved": [],
        "resourceChanges": [],
        "containersAdded": sorted(set(cur_c) - set(prev_c)),
        "containersRemoved": sorted(set(prev_c) - set(cur_c)),
    }
    for name in sorted(set(prev_c) & set(cur_c)):
        a, b = prev_c[name], cur_c[name]
        if a.get("image") != b.get("image"):
            diff["imageChanges"].append({"container": name, "from": a.get("image"), "to": b.get("image")})
        for env in sorted(_env_names(b) - _env_names(a)):
            diff["envAdded"].append({"container": name, "name": env})
        for env in sorted(_env_names(a) - _env_names(b)):
            diff["envRemoved"].append({"container": name, "name": env})
        if (a.get("resources") or {}) != (b.get("resources") or {}):
            diff["resourceChanges"].append(
                {"container": name, "from": a.get("resources") or {}, "to": b.get("resources") or {}}
            )
    return diff


def config_changes(kind, workload, revisions, since, now):
    """Revisions created within `since`, each compared with the revision
    before it: images, env var names added or removed, resources, containers.

    A rollback re-uses an older ReplicaSet, whose creation time is old, so it
    shows only when that revision's creation time is in the window.
    """
    views = [_revision_view(kind, r) for r in revisions]
    views = [v for v in views if v["revision"] is not None]
    views.sort(key=lambda v: v["revision"])
    out = []
    for i, v in enumerate(views):
        if not _within(parse_time(v["created"]), since, now):
            continue
        prev = views[i - 1] if i else None
        entry = {
            "revision": v["revision"],
            "name": v["name"],
            "created": v["created"],
            "previousRevision": prev["revision"] if prev else None,
            "images": _images(v["podSpec"]),
        }
        entry.update(_diff_specs(prev["podSpec"], v["podSpec"]) if prev else _diff_specs({}, {}))
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
    kind = m.get("type")
    body = m.get(kind[0].lower() + kind[1:]) or {}
    name = body.get("name") or (body.get("metric") or {}).get("name")
    return kind, name, body


def hpa_status(hpas, kind, name, pod_template):
    """HPAs whose scaleTargetRef is this workload: bounds, current and desired
    replicas, conditions, metrics, and whether a Utilization metric targets a
    container that has no resource request (the HPA cannot compute it then)."""
    containers = pod_template.get("spec", {}).get("containers") or []
    out = []
    for h in hpas:
        ref = h.get("spec", {}).get("scaleTargetRef") or {}
        if ref.get("kind") != kind or ref.get("name") != name:
            continue
        spec, status = h["spec"], h.get("status", {})
        current = {}
        for m in status.get("currentMetrics") or []:
            k, n, body = _metric_identity(m)
            current[(k, n)] = (body.get("current") or {})
        metrics, missing_any = [], False
        for m in spec.get("metrics") or []:
            k, n, body = _metric_identity(m)
            _, target_value = _target_value(body.get("target") or {})
            _, cur_value = _target_value(current.get((k, n), {}))
            missing = []
            if k in ("Resource", "ContainerResource") and (body.get("target") or {}).get("type") == "Utilization":
                scope = [c for c in containers if k == "Resource" or c["name"] == body.get("container")]
                missing = [
                    c["name"] for c in scope if n not in ((c.get("resources") or {}).get("requests") or {})
                ]
            missing_any = missing_any or bool(missing)
            metrics.append(
                {
                    "type": k,
                    "name": n,
                    "targetType": body.get("target", {}).get("type"),
                    "target": target_value,
                    "current": cur_value,
                    "containersWithoutRequests": missing,
                }
            )
        min_r, max_r = spec.get("minReplicas", 1), spec.get("maxReplicas")
        cur_r, des_r = status.get("currentReplicas", 0), status.get("desiredReplicas")
        out.append(
            {
                "name": h["metadata"]["name"],
                "minReplicas": min_r,
                "maxReplicas": max_r,
                "currentReplicas": cur_r,
                "desiredReplicas": des_r,
                "atMax": None if des_r is None or max_r is None else des_r >= max_r,
                "atMin": None if des_r is None else des_r <= min_r,
                "lastScaleTime": status.get("lastScaleTime"),
                "conditions": _conditions(h),
                "metrics": metrics,
                "resourceMetricWithoutRequests": missing_any,
            }
        )
    return out
