"""What the two tasks do per pod: run redis-cli calls (through fetch.py) and shape the facts."""

from k8s_redis_health.errors import RwError
from k8s_redis_health.fetch import error_entry, redis_cli, redis_info
from k8s_redis_health.redis import AUTH_HINT, counts, instance_facts, role_of
from k8s_redis_health.workload import choose_container, limit_text, pod_ready

TTL_SECONDS = 60
KEY_PREFIX = "rw:healthcheck:"


def _report_failures(ctx, failures):
    """Record the per-pod failures with ctx.error (-> partial)."""
    for name, exc in failures:
        entry = error_entry(exc)
        ctx.error(
            entry["code"],
            f"pod {name}: {entry['message']}",
            scope=f"pod/{name}",
            hint=AUTH_HINT if entry["code"] == "permission_denied" else None,
        )


def _raise_if_nothing_looked(attempted, succeeded, failures):
    """A failure on every pod that was tried leaves nothing to report: it fails the task."""
    if attempted and not succeeded and failures:
        raise failures[0][1]


# ----------------------------------------------------------------- ping ----


def ping_instances(ctx, pods, requested, memory_threshold):
    """One entry per pod. Ready pods are asked PING and INFO; a pod that is not ready is listed and
    not queried. Returns (instances, counts)."""
    instances, failures = [], []
    for pod in pods:
        name = pod["metadata"]["name"]
        entry = {
            "pod": name,
            "ready": pod_ready(pod),
            "container": choose_container(pod, requested),
            "queried": False,
            "ping": None,
            "info": None,
            "error": None,
        }
        instances.append(entry)
        if not entry["ready"]:
            continue
        try:
            reply, latency = redis_cli(ctx, pod, entry["container"], "PING")
            text, truncated = limit_text(reply)
            entry["queried"] = True
            entry["ping"] = {
                "ok": reply == "PONG",
                "response": text,
                "truncated": truncated,
                "latencyMs": latency,
            }
            fields, _ = redis_info(ctx, pod, entry["container"])
            entry["info"] = instance_facts(fields, memory_threshold)
        except RwError as exc:
            entry["error"] = error_entry(exc)
            failures.append((name, exc))
    attempted = [i for i in instances if i["ready"]]
    _raise_if_nothing_looked(attempted, [i for i in attempted if i["queried"]], failures)
    _report_failures(ctx, failures)
    return instances, counts(instances)


# ------------------------------------------------------ read-write check ----


def find_primaries(ctx, pods, requested):
    """(candidates, primary pods): the role of every ready pod. A pod whose role could not be read
    is listed with role null and an error; if no pod's role could be read the task fails."""
    candidates, failures, primaries = [], [], []
    for pod in pods:
        name = pod["metadata"]["name"]
        entry = {
            "pod": name,
            "ready": pod_ready(pod),
            "container": choose_container(pod, requested),
            "role": None,
            "error": None,
        }
        candidates.append(entry)
        if not entry["ready"]:
            continue
        try:
            fields, _ = redis_info(ctx, pod, entry["container"], "replication")
            entry["role"] = role_of(fields)
        except RwError as exc:
            entry["error"] = error_entry(exc)
            failures.append((name, exc))
            continue
        if entry["role"] == "master":
            primaries.append(pod)
    attempted = [c for c in candidates if c["ready"]]
    _raise_if_nothing_looked(attempted, [c for c in attempted if c["error"] is None], failures)
    _report_failures(ctx, failures)
    return candidates, primaries


def _step(name, performed, ok=None, latency=None, reply=None, code=None):
    return {
        "step": name,
        "performed": performed,
        "ok": ok,
        "latencyMs": latency,
        "reply": reply,
        "errorCode": code,
    }


def _attempt(ctx, pod, container, name, args, expected):
    """One redis-cli step: (step entry, reply or None). A step that could not run is a failed step
    with its error code, recorded with ctx.error; a reply that is not the expected one is a failed
    step too (a fact about the server, no error)."""
    try:
        reply, latency = redis_cli(ctx, pod, container, *args)
    except RwError as exc:
        entry = error_entry(exc)
        ctx.error(
            entry["code"],
            f"pod {pod['metadata']['name']}: {name} failed: {entry['message']}",
            scope=f"pod/{pod['metadata']['name']}",
        )
        return _step(name, True, ok=False, code=entry["code"]), None
    text, _ = limit_text(reply, 256)
    return _step(name, True, ok=reply == expected, latency=latency, reply=text), reply


def write_check_on(ctx, pod, container, key, value, dry):
    """SET key value EX 60, GET it back, DEL it, on one primary."""
    entry = {
        "pod": pod["metadata"]["name"],
        "container": container,
        "steps": [],
        "allOk": None,
        "valueMatches": None,
        "keyRemoved": None,
    }
    if dry:
        entry["steps"] = [_step(n, False) for n in ("set", "get", "del")]
        return entry
    steps = entry["steps"]
    set_step, set_reply = _attempt(
        ctx, pod, container, "set", ["SET", key, value, "EX", str(TTL_SECONDS)], "OK"
    )
    steps.append(set_step)
    # a server that refused the SET (READONLY, MISCONF) wrote nothing; one that could not be reached
    # may have: the key has a TTL, and a DEL is tried
    wrote = set_step["ok"] or set_reply is None
    if set_step["ok"]:
        get_step, _ = _attempt(ctx, pod, container, "get", ["GET", key], value)
        steps.append(get_step)
        entry["valueMatches"] = get_step["ok"]
    else:
        steps.append(_step("get", False))
    if wrote:
        del_step, _ = _attempt(ctx, pod, container, "del", ["DEL", key], "1")
        steps.append(del_step)
        entry["keyRemoved"] = del_step["ok"]
    else:
        steps.append(_step("del", False))
    entry["allOk"] = all(s["performed"] and s["ok"] for s in steps)
    return entry


def read_write_check(ctx, pods, requested, now, dry):
    """(candidates, check): the write sequence on every primary (role master), or on none."""
    suffix = now.strftime("%Y%m%dT%H%M%S%f")
    key = f"{KEY_PREFIX}{suffix}"
    candidates, primaries = find_primaries(ctx, pods, requested)
    reason = None
    if not any(c["ready"] for c in candidates):
        reason = "no_ready_pods"
    elif not primaries:
        reason = "no_primary"
    elif dry:
        reason = "dry_run"
    results = [
        write_check_on(ctx, p, choose_container(p, requested), key, suffix, dry) for p in primaries
    ]
    check = {
        "key": key,
        "ttlSeconds": TTL_SECONDS,
        "dryRun": dry,
        "ran": bool(results) and not dry,
        "reason": reason,
        "primaries": results,
    }
    return candidates, check
