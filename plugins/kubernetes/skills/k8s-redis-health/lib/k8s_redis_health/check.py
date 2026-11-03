"""What the two tasks do per pod: run redis-cli calls (through fetch.py) and shape the facts."""

from k8s_redis_health.errors import RwError
from k8s_redis_health.fetch import error_entry, redis_cli, redis_info
from k8s_redis_health.redis import (
    counts,
    instance_facts,
    refuse_unsupported_mode,
    require_server,
    role_of,
)
from k8s_redis_health.workload import choose_container, limit_text, pod_ready

TTL_SECONDS = 60
KEY_PREFIX = "rw:healthcheck:"
# a SET that did not answer in time or could not be reached may still have written the key
MAYBE_WROTE = ("timeout", "unreachable")


def _report_failures(ctx, failures):
    """Record the per-pod failures with ctx.error (-> partial)."""
    for name, exc in failures:
        entry = error_entry(exc)
        ctx.error(
            entry["code"],
            f"pod {name}: {entry['message']}",
            scope=f"pod/{name}",
            hint=getattr(exc, "hint", None),
        )


def _raise_if_nothing_looked(attempted, succeeded, failures):
    """A failure on every pod that was tried leaves nothing to report: it fails the task."""
    if attempted and not succeeded and failures:
        raise failures[0][1]


# ----------------------------------------------------------------- ping ----


def ping_instances(ctx, pods, requested, memory_threshold, conn):
    """One entry per pod. Ready pods are asked PING and INFO; a pod that is not ready is listed and
    not queried, and so is one reached after the task's time budget was spent (timeout error).
    Returns (instances, counts)."""
    instances, failures = [], []
    for pod in pods:
        name = pod["metadata"]["name"]
        entry = {
            "pod": name,
            "ready": pod_ready(pod),
            "container": choose_container(pod, requested),
            "mode": None,
            "queried": False,
            "ping": None,
            "info": None,
            "error": None,
        }
        instances.append(entry)
        if not entry["ready"]:
            continue
        try:
            reply, latency = redis_cli(ctx, pod, entry["container"], conn, "PING")
            text, truncated = limit_text(reply)
            entry["queried"] = True
            entry["ping"] = {
                "ok": reply == "PONG",
                "response": text,
                "truncated": truncated,
                "latencyMs": latency,
            }
            fields, _ = redis_info(ctx, pod, entry["container"], conn)
            entry["mode"] = limit_text(fields.get("redis_mode"))[0]
            entry["info"] = instance_facts(require_server(fields), memory_threshold)
        except RwError as exc:
            entry["error"] = error_entry(exc)
            failures.append((name, exc))
    attempted = [i for i in instances if i["ready"]]
    # a PONG without INFO facts is not a look at the server (a sentinel answers PING too)
    _raise_if_nothing_looked(attempted, [i for i in attempted if i["info"] is not None], failures)
    _report_failures(ctx, failures)
    return instances, counts(instances)


# ------------------------------------------------------ read-write check ----


def _fail(entry, failures, exc):
    entry["error"] = error_entry(exc)
    failures.append((entry["pod"], exc))


def find_primaries(ctx, pods, requested, conn):
    """(candidates, primary pods): the role and mode of every ready pod. A pod whose role could not
    be read is listed with role null and an error; if no pod's role could be read the task fails. A
    pod that runs in cluster or sentinel mode refuses the whole check (invalid_input)."""
    candidates, failures, primaries = [], [], []
    for pod in pods:
        entry = {
            "pod": pod["metadata"]["name"],
            "ready": pod_ready(pod),
            "container": choose_container(pod, requested),
            "mode": None,
            "role": None,
            "error": None,
        }
        candidates.append(entry)
        if not entry["ready"]:
            continue
        try:
            fields, _ = redis_info(ctx, pod, entry["container"], conn)
        except RwError as exc:
            _fail(entry, failures, exc)
            continue
        entry["mode"] = limit_text(fields.get("redis_mode"))[0]
        refuse_unsupported_mode(fields)
        try:
            entry["role"] = role_of(require_server(fields))
        except RwError as exc:
            _fail(entry, failures, exc)
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


def _attempt(ctx, pod, container, conn, name, args, expected):
    """One redis-cli step: (step entry, reply or None). A step that could not run is a failed step
    with its error code, recorded with ctx.error; a reply that is not the expected one is a failed
    step too (a fact about the server, no error)."""
    try:
        reply, latency = redis_cli(ctx, pod, container, conn, *args)
    except RwError as exc:
        entry = error_entry(exc)
        ctx.error(
            entry["code"],
            f"pod {pod['metadata']['name']}: {name} failed: {entry['message']}",
            scope=f"pod/{pod['metadata']['name']}",
        )
        return _step(name, True, ok=False, code=entry["code"]), None
    text, _ = limit_text(reply or "(nil)", 256)  # redis-cli prints a nil reply as nothing
    return _step(name, True, ok=reply == expected, latency=latency, reply=text), reply


def write_check_on(ctx, pod, container, conn, key, value, dry):
    """SET key value NX EX 60, GET it back, DEL it, on one primary."""
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
    set_step, _ = _attempt(
        ctx,
        pod,
        container,
        conn,
        "set",
        ["SET", key, value, "NX", "EX", str(TTL_SECONDS)],
        "OK",
    )
    steps.append(set_step)
    # A SET that was refused (READONLY, MISCONF), answered nil (the key exists: NX did not set it)
    # or was not allowed wrote nothing, and nothing is deleted. One that succeeded, or that timed
    # out or could not be reached, may have written the key, so it is deleted (it has a TTL anyway).
    wrote = set_step["ok"] or set_step["errorCode"] in MAYBE_WROTE
    if set_step["ok"]:
        get_step, _ = _attempt(ctx, pod, container, conn, "get", ["GET", key], value)
        steps.append(get_step)
        entry["valueMatches"] = get_step["ok"]
    else:
        steps.append(_step("get", False))
    if wrote:
        del_step, _ = _attempt(ctx, pod, container, conn, "del", ["DEL", key], "1")
        steps.append(del_step)
        entry["keyRemoved"] = del_step["ok"]
    else:
        steps.append(_step("del", False))
    entry["allOk"] = all(s["performed"] and s["ok"] for s in steps)
    return entry


BITNAMI_HINT = (
    "every ready pod is a replica; a Bitnami chart runs the master and the replicas as separate "
    "StatefulSets: target the master StatefulSet"
)


def read_write_check(ctx, pods, requested, now, dry, conn):
    """(candidates, check): the write sequence on ONE primary, the first by pod name, or a failure
    when there is nothing to write to (no ready pod, no primary). Other pods with role master are
    reported as otherPrimaries: several masters without replication is itself a fact."""
    key = f"{KEY_PREFIX}{now.strftime('%Y%m%dT%H%M%S%f')}"
    value = now.strftime("%Y%m%dT%H%M%S%f")
    if not any(pod_ready(p) for p in pods):
        raise RwError(
            "unreachable",
            f"no ready Redis pod in {ctx.resource.name}",
            hint="see kubernetes:k8s-workload-health/pod-status for why the pods are not ready",
        )
    candidates, primaries = find_primaries(ctx, pods, requested, conn)
    if not primaries:
        unread = [c for c in candidates if c["error"] is not None]
        if unread:  # a pod that could not be asked may be the primary: not a proof of none
            raise RwError(unread[0]["error"]["code"], unread[0]["error"]["message"])
        raise RwError(
            "not_found", "no Redis primary: every ready pod is a replica", hint=BITNAMI_HINT
        )
    primaries = sorted(primaries, key=lambda p: p["metadata"]["name"])
    primary, others = primaries[0], primaries[1:]
    result = write_check_on(
        ctx, primary, choose_container(primary, requested), conn, key, value, dry
    )
    check = {
        "key": key,
        "ttlSeconds": TTL_SECONDS,
        "dryRun": dry,
        "ran": not dry,
        "reason": "dry_run" if dry else None,
        "primaries": [result],
        "otherPrimaries": [p["metadata"]["name"] for p in others],
    }
    return candidates, check
