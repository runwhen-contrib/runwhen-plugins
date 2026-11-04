"""server-status: liveness, readiness, runtime and head-block facts per Prometheus pod."""

import re

from k8s_prometheus_health.common import MESSAGE_LIMIT, age_seconds, iso, parse_time, truncate
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.fetch import Budget, endpoint, fetch_one, pod_stats, select_pods
from k8s_prometheus_health.proxy import api_json, status_error

_HEALTHY = re.compile(r"\bhealthy\b")
_READY = re.compile(r"\bready\b")


def probe_reply(reply, what, word, negative):
    """True/False from a /-/healthy or /-/ready reply: a 200 whose text says `word` is True, a 503
    is False (Prometheus answers 503 while not ready); a 200 that says anything else, or any
    other status, is an error and never True."""
    if reply.status == 503:
        return False
    if reply.status != 200:
        raise status_error(reply, what)
    text = reply.body.lower()
    if word.search(text) and negative not in text:
        return True
    raise RwError(
        "internal", f"{what} did not return the Prometheus probe text: {text.strip()[:80]!r}"
    )


def healthy_reply(reply, what):
    return probe_reply(reply, what, _HEALTHY, "unhealthy")


def ready_reply(reply, what):
    return probe_reply(reply, what, _READY, "not ready")


def runtime_view(data, now):
    start = parse_time(data.get("startTime"))
    last_config = parse_time(data.get("lastConfigTime"))
    return {
        "startTime": iso(start),
        "uptimeSeconds": age_seconds(start, now),
        "lastConfigTime": iso(last_config),
        "lastConfigAgeSeconds": age_seconds(last_config, now),
        "reloadConfigSuccess": data.get("reloadConfigSuccess"),
        "corruptionCount": data.get("corruptionCount"),
        "goroutineCount": data.get("goroutineCount"),
        "storageRetention": data.get("storageRetention"),
    }


def tsdb_view(data):
    head = data.get("headStats") or {}
    return {
        "headSeries": head.get("numSeries"),
        "headChunks": head.get("chunkCount"),
        "headLabelPairs": head.get("numLabelPairs"),
    }


CONDITIONS = ("Available", "Reconciled")


def expected_replicas(spec):
    """The pods the CR asks for: replicas (1 when unset) times shards (1 when unset)."""
    replicas, shards = spec.get("replicas"), spec.get("shards")
    return (1 if replicas is None else replicas) * (1 if shards is None else shards)


def condition_view(prom):
    """The operator's Available and Reconciled conditions of `Prometheus.status.conditions`."""
    rows = []
    for cond in (prom.get("status") or {}).get("conditions") or []:
        if cond.get("type") not in CONDITIONS:
            continue
        message, cut = truncate(cond.get("message") or None)
        rows.append(
            {
                "type": cond.get("type"),
                "status": cond.get("status"),
                "reason": cond.get("reason") or None,
                "message": message,
                "messageTruncated": cut,
                "lastTransitionTime": cond.get("lastTransitionTime"),
            }
        )
    return rows


def prometheus_view(prom):
    spec = prom.get("spec") or {}
    scheme, _, prefix = endpoint(prom, {})
    return {
        "name": prom["metadata"]["name"],
        "namespace": prom["metadata"]["namespace"],
        "replicas": spec.get("replicas"),
        "shards": spec.get("shards"),
        "expectedReplicas": expected_replicas(spec),
        "conditions": condition_view(prom),
        "version": spec.get("version"),
        "scheme": scheme,
        "portName": spec.get("portName") or "web",
        "routePrefix": prefix or "/",
    }


def pod_condition(pod, kind):
    for cond in (pod.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status") == "True"
    return None


def pod_row(pod, prom, probes, runtime, tsdb):
    """One pod: its own phase and Ready condition, then what the Prometheus answered (None for
    an answer that was not obtained)."""
    _, port, _ = endpoint(prom, pod)
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_condition(pod, "Ready"),
        "port": port,
        "queried": probes is not None,
        "healthy": (probes or {}).get("healthy"),
        "ready": (probes or {}).get("ready"),
        "runtime": runtime,
        "tsdb": tsdb,
    }


def collect(ctx, prom, pods, now, limit, budget=None):
    """The server view. The first failure of a pod (not forbidden) ends that pod's probing, so one
    dead pod is one error, not four. If no running pod answered a single probe the first failure
    is raised (nothing to report); otherwise the failures are recorded with ctx.error. A CR that
    expects pods (replicas times shards) of which none is queryable is `unreachable` scoped
    `pods`, so the result is partial, not a clean empty one."""
    budget = budget or Budget()
    asked = {p["metadata"]["name"] for p in select_pods(pods)[0]}
    rows, failures, queried = [], [], 0
    for pod in pods[:limit]:
        if pod["metadata"]["name"] not in asked:
            rows.append(pod_row(pod, prom, None, None, None))
            continue
        queried += 1
        probes, runtime, tsdb = {}, None, None
        try:
            probes["healthy"] = fetch_one(
                ctx, prom, pod, "/-/healthy", healthy_reply, "/-/healthy", budget
            )
            probes["ready"] = fetch_one(ctx, prom, pod, "/-/ready", ready_reply, "/-/ready", budget)
            runtime = runtime_view(
                fetch_one(
                    ctx,
                    prom,
                    pod,
                    "/api/v1/status/runtimeinfo",
                    api_json,
                    "runtimeinfo",
                    budget,
                ),
                now,
            )
            tsdb = tsdb_view(
                fetch_one(ctx, prom, pod, "/api/v1/status/tsdb", api_json, "tsdb status", budget)
            )
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failures.append((pod["metadata"]["name"], exc))
        rows.append(pod_row(pod, prom, probes, runtime, tsdb))
    if queried and len(failures) == queried and not any(r["healthy"] is not None for r in rows):
        name, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"pod/{name}") from exc
    for name, exc in failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"pod/{name}")
    expected = expected_replicas(prom.get("spec") or {})
    if not queried and expected > 0:
        ctx.error(
            "unreachable",
            f"Prometheus {prom['metadata']['name']} expects {expected} replica(s) and "
            f"{len(pods)} exist, none Running: nothing to query",
            scope="pods",
        )
    return {
        "prometheus": prometheus_view(prom),
        **pod_stats(pods),
        "truncated": len(pods) > limit,
        "pods": rows,
    }
