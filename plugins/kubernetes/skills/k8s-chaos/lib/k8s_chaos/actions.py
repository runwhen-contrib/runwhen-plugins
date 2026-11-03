"""The four chaos actions. Each one picks its victims, acts, observes what happened
and returns facts. Anything that changes the cluster and must be undone is undone in a
`finally` path and the undo is verified."""

import hashlib
import json
import re

from k8s_chaos import clock
from k8s_chaos.common import (
    MIB,
    RESOURCE,
    allocation_bytes,
    cap,
    check_container_name,
    check_int,
    container_status,
    first_container,
    is_running,
    limit_text,
    matches_selector,
    memory_limit,
    parse_duration,
    pick,
    pod_view,
    ready_condition,
    restart_count,
    resolve_target,
    seed_of,
    selector_string,
    target_view,
)
from k8s_chaos.errors import RwError
from k8s_chaos.fetch import get_pod, list_pods, load_target
from k8s_chaos.k8s import (
    MESSAGE_LIMIT,
    exec_failure,
    failure,
    kubectl_json,
    run_checked,
    run_unchecked,
)
from k8s_chaos.service import (
    MAX_SERVICES,
    MODES,
    is_restored,
    matching_services,
    mutation_plan,
    original_spec,
    ready_endpoints,
    restore_failure_message,
    restore_patch,
    selects_workload,
)

POLL_SECONDS = 2.0
EXEC_SECONDS = 120
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")


def _target(ctx):
    res = ctx.resource
    return resolve_target(
        getattr(res, "type", None),
        getattr(res, "path", None),
        getattr(res, "namespace", None),
        res.name,
    )


def _running_pods(ctx, target, workload):
    """(all pods of the target, the running ones); a target with no running pod raises not_found."""
    pods = list_pods(ctx, target, workload)
    running = [p for p in pods if is_running(p)]
    if not running:
        where = (
            "the namespace"
            if target["scope"] == "namespace"
            else f"{target['kind']} {target['name']}"
        )
        raise RwError(
            "not_found",
            f"no running pod in {where} to act on ({len(pods)} pods in any state); nothing was changed",
        )
    return pods, running


def _seed(ctx, seed):
    return seed_of(seed, ctx.now())


def _seed_view(seed, source):
    return {"value": seed, "source": source}


def _ms(seconds):
    return int(round(seconds * 1000))


def _by_name(pods):
    return {p["metadata"]["name"]: p for p in pods}


# ---------------------------------------------------------------- delete ----


def _deleted_names(output):
    names = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("pod/"):
            names.append(line.split("/", 1)[1].split()[0])
    return names


def _await_recovery(ctx, target, workload, deleted_uids, seconds):
    """Poll until the workload has its desired ready replicas again, none of them a
    pod this task deleted: {waited, recovered, recoveryMs, waitedMs, ...}."""
    ns, kind, name = target["namespace"], target["kind"], target["name"]
    selector = selector_string(workload["spec"]["selector"])
    desired = workload["spec"].get("replicas")
    desired = 1 if desired is None else desired
    result = {
        "waited": True,
        "recovered": False,
        "recoveryMs": None,
        "waitedMs": 0,
        "desiredReplicas": desired,
        "readyReplicasBefore": workload.get("status", {}).get("readyReplicas") or 0,
        "readyReplicasAfter": None,
    }
    started = clock.monotonic()
    try:
        while True:
            current = kubectl_json(ctx, "-n", ns, "get", RESOURCE[kind], name)
            pods = kubectl_json(ctx, "-n", ns, "get", "pods", "-l", selector)["items"]
            elapsed = clock.monotonic() - started
            status_ready = current.get("status", {}).get("readyReplicas") or 0
            result["readyReplicasAfter"] = status_ready
            fresh_ready = sum(
                1
                for p in pods
                if is_running(p)
                and ready_condition(p)
                and p["metadata"]["uid"] not in deleted_uids
                and matches_selector(p["metadata"].get("labels"), workload["spec"]["selector"])
            )
            if desired > 0 and status_ready >= desired and fresh_ready >= desired:
                result["recovered"] = True
                result["recoveryMs"] = _ms(elapsed)
                break
            if elapsed >= seconds:
                break
            clock.sleep(min(POLL_SECONDS, max(seconds - elapsed, 0.1)))
    except RwError as exc:
        # the pods are already deleted: report what was done, and why we stopped looking
        ctx.error(
            exc.code,
            f"could not watch the workload recover: {exc.message}"[:MESSAGE_LIMIT],
            scope="recovery",
        )
        result["recovered"] = None
    result["waitedMs"] = _ms(clock.monotonic() - started)
    return result


def delete_pods(ctx, count=1, wait_for_recovery=True, timeout="120s", seed=None):
    count = check_int(count, "count", 1, 10)
    seconds = parse_duration(timeout, "timeout", 1, 300)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload)
    by_name = _by_name(running)
    chosen = pick(list(by_name), count, seed_value)
    dry = bool(ctx.dry_run)

    argv = ["kubectl", "-n", ns, "delete", "pod", *chosen, "--wait=false", "--ignore-not-found"]
    argv += ["-o", "name"]
    if dry:
        argv.append("--dry-run=server")
    # check=False: a partly failed delete still printed the pods it deleted; they are reported
    result = run_unchecked(ctx, argv)
    confirmed = _deleted_names(result.stdout)
    if result.returncode:
        failed = failure(argv, result)
        if not confirmed:
            raise failed
        ctx.error(
            failed.code,
            f"deleted {len(confirmed)} of {len(chosen)} pods: {failed.message}"[:MESSAGE_LIMIT],
            scope="delete",
        )
    gone = [n for n in chosen if n not in confirmed]
    recovery = None
    if workload is not None and wait_for_recovery and not dry and confirmed:
        uids = {by_name[n]["metadata"]["uid"] for n in confirmed}
        recovery = _await_recovery(ctx, target, workload, uids, seconds)
    ready_names = {n for n, p in by_name.items() if ready_condition(p)}
    return {
        "target": target_view(target, workload),
        "deletion": {
            "applied": bool(confirmed) and not dry,
            "seed": _seed_view(seed_value, seed_source),
            "requestedCount": count,
            "candidateCount": len(running),
            "selectedCount": len(chosen),
            "deletedCount": 0 if dry else len(confirmed),
            "wouldDeleteCount": len(confirmed) if dry else 0,
            "alreadyGoneCount": len(gone),
            "deletesAllReady": bool(ready_names) and ready_names <= set(confirmed),
            "deleted": [] if dry else cap(confirmed, ctx.settings.max_items),
            "wouldDelete": cap(confirmed, ctx.settings.max_items) if dry else [],
            "alreadyGone": cap(gone, ctx.settings.max_items),
            "pods": [pod_view(by_name[n]) for n in chosen][:50],
            "recovery": recovery,
        },
    }


# ---------------------------------------------------------------- exec ----


def _exec(ctx, ns, pod, container, script, *args, seconds=EXEC_SECONDS):
    """`kubectl exec` of `sh -c script sh args...`: the finished process.

    Raises RwError when kubectl could not run the command (forbidden, pod gone, no
    sh or tool in the image -> dependency). A command that ran and exited non-zero
    returns normally: its exit code is a fact for the caller.
    """
    argv = [
        "kubectl",
        "-n",
        ns,
        "exec",
        pod,
        "-c",
        container,
        "--",
        "sh",
        "-c",
        script,
        "sh",
        *args,
    ]
    result = run_unchecked(ctx, argv, timeout=seconds)
    problem = exec_failure(argv, result)
    if problem is not None:
        raise problem
    return result


def _exit_code(result):
    return int(result.returncode)


def _restart_view(before, pod, container):
    after = restart_count(pod, container) if pod is not None else None
    return before, after, None if after is None else after - before


def _last_state(pod, container):
    """The container's lastState.terminated as facts, or None."""
    status = container_status(pod, container) if pod is not None else None
    term = ((status or {}).get("lastState") or {}).get("terminated")
    if not term:
        return None
    reason, _ = limit_text(term.get("reason"))
    return {
        "reason": reason,
        "exitCode": term.get("exitCode"),
        "finishedAt": term.get("finishedAt"),
    }


# ---------------------------------------------------------------- oomkill ----


def _oom_candidates(running, container):
    """[(pod, container name, memory limit bytes)] for running pods that have a container
    with a memory limit."""
    out = []
    for pod in running:
        found = memory_limit(pod, container)
        if found:
            out.append((pod, found[0], found[1]))
    return out


def oomkill_pod(ctx, container=None, timeout="30s", seed=None):
    container = check_container_name(container)
    seconds = parse_duration(timeout, "timeout", 1, 120)
    max_mi = int(ctx.settings.max_memory_limit_mi)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload)
    candidates = _oom_candidates(running, container)
    if not candidates:
        what = f"container {container!r}" if container else "any container"
        raise RwError(
            "invalid_input",
            f"none of the {len(running)} running pods has {what} with a memory limit; "
            "without a limit the container cannot be OOM-killed by this task, so nothing was run",
        )
    too_big = [c for c in candidates if c[2] > max_mi * MIB]
    candidates = [c for c in candidates if c[2] <= max_mi * MIB]
    if not candidates:
        raise RwError(
            "invalid_input",
            f"every memory limit found is above the maxMemoryLimitMi setting ({max_mi}Mi; "
            f"{len(too_big)} pods): allocating that much could exhaust the node; nothing was run",
        )
    by_name = {c[0]["metadata"]["name"]: c for c in candidates}
    name = pick(list(by_name), 1, seed_value)[0]
    pod, cname, limit = by_name[name]
    alloc = allocation_bytes(limit)
    before = restart_count(pod, cname)
    prev = _last_state(pod, cname)
    out = {
        "applied": False,
        "seed": _seed_view(seed_value, seed_source),
        "candidateCount": len(candidates),
        "pod": pod_view(pod),
        "container": cname,
        "memoryLimitBytes": limit,
        "allocateBytes": alloc,
        "command": {"binary": "sh", "argCount": 4},
        "exitCode": None,
        "restartCountBefore": before,
        "restartCountAfter": None,
        "restartDelta": None,
        "lastState": None,
        "oomKilled": None,
        "observedMs": None,
        "podPresent": None,
    }
    if ctx.dry_run:
        return {"target": target_view(target, workload), "oomkill": out}

    # one process allocates `alloc` bytes in one buffer and touches every page of it
    script = 'dd if=/dev/zero of=/dev/null bs="$1" count=1'
    started = clock.monotonic()
    exec_error = None
    try:
        result = _exec(ctx, ns, name, cname, script, str(alloc))
        out["exitCode"] = _exit_code(result)
    except RwError as exc:
        # a stream cut by the kill itself looks like an internal kubectl error; anything
        # else (forbidden, no sh or dd, pod gone) means nothing ran
        if exc.code != "internal":
            raise
        exec_error = exc
    out["applied"] = True
    current, deadline = None, started + seconds
    while True:
        try:
            current = get_pod(ctx, ns, name)
        except RwError as exc:
            if exc.code != "not_found":
                ctx.error(
                    exc.code,
                    f"could not read the pod afterwards: {exc.message}"[:MESSAGE_LIMIT],
                    scope="observe",
                )
                current = None
                out["podPresent"] = None
                break
            current = None
            out["podPresent"] = False
            break
        out["podPresent"] = True
        status = container_status(current, cname)
        restarted = restart_count(current, cname) > before
        if restarted and status and (status.get("lastState") or {}).get("terminated"):
            break
        if clock.monotonic() >= deadline:
            break
        clock.sleep(1.0)
    out["observedMs"] = _ms(clock.monotonic() - started)
    if current is not None:
        _, after, delta = _restart_view(before, current, cname)
        last = _last_state(current, cname)
        out["restartCountAfter"], out["restartDelta"], out["lastState"] = after, delta, last
        out["oomKilled"] = bool(
            delta
            and last
            and last["reason"] == "OOMKilled"
            and (prev is None or last["finishedAt"] != prev["finishedAt"])
        )
    elif out["podPresent"] is False:
        out["oomKilled"] = False
    if exec_error is not None and not out["oomKilled"]:
        raise exec_error
    return {"target": target_view(target, workload), "oomkill": out}


# ---------------------------------------------------------------- fill tmp ----

_WRITE = 'dd if=/dev/zero of="$1" bs=1M count="$2"; rc=$?; wc -c < "$1"; exit $rc'
_CLEAN = 'rm -f -- "$1"; test ! -e "$1"'


def _cleanup(ctx, ns, pod, container, path, pod_state):
    """Remove the file and check it is gone: {attempted, succeeded, reason}."""
    if pod_state in ("evicted", "gone"):
        return {"attempted": False, "succeeded": False, "reason": f"pod_{pod_state}"}
    try:
        result = _exec(ctx, ns, pod, container, _CLEAN, path, seconds=60)
    except RwError as exc:
        if exc.code == "not_found":
            return {"attempted": True, "succeeded": False, "reason": "pod_gone"}
        ctx.error(
            exc.code,
            f"could not remove {path} from {ns}/{pod} ({container}): {exc.message}"[:MESSAGE_LIMIT],
            scope="cleanup",
            hint=f"remove the file by hand: kubectl -n {ns} exec {pod} -c {container} -- rm -f {path}",
        )
        return {"attempted": True, "succeeded": False, "reason": exc.code}
    if result.returncode:
        ctx.error(
            "internal",
            f"{path} is still in {ns}/{pod} ({container}) after rm (exit {result.returncode})",
            scope="cleanup",
            hint=f"remove the file by hand: kubectl -n {ns} exec {pod} -c {container} -- rm -f {path}",
        )
        return {"attempted": True, "succeeded": False, "reason": "still_present"}
    return {"attempted": True, "succeeded": True, "reason": None}


def _pod_state(pod):
    """'ok', 'evicted' (Failed/Evicted) or 'gone' (the pod no longer exists)."""
    if pod is None:
        return "gone"
    status = pod.get("status", {})
    if status.get("reason") == "Evicted" or status.get("phase") == "Failed":
        return "evicted"
    if pod["metadata"].get("deletionTimestamp") is not None:
        return "gone"
    return "ok"


def _refresh_pod(ctx, ns, name, last):
    """(pod, state) read again; a read that fails other than not_found is reported and the
    last known pod kept."""
    try:
        pod = get_pod(ctx, ns, name)
    except RwError as exc:
        if exc.code == "not_found":
            return None, "gone"
        ctx.error(
            exc.code,
            f"could not read the pod before the cleanup: {exc.message}"[:MESSAGE_LIMIT],
            scope="observe",
        )
        return last, "ok"
    return pod, _pod_state(pod)


def _watch_pod(ctx, ns, name, container, before, seconds):
    """Watch the pod for `seconds` (stopping early if it is evicted, restarted or gone):
    (pod document or None, state)."""
    deadline = clock.monotonic() + seconds
    pod = None
    while True:
        try:
            pod = get_pod(ctx, ns, name)
        except RwError as exc:
            if exc.code == "not_found":
                return None, "gone"
            raise
        state = _pod_state(pod)
        if state != "ok" or restart_count(pod, container) > before:
            return pod, state
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return pod, state
        clock.sleep(min(POLL_SECONDS, remaining))


def fill_tmp(ctx, size_mi=100, container=None, observe="10s", seed=None):
    size_mi = check_int(size_mi, "sizeMi", 1, 1024)
    container = check_container_name(container)
    seconds = parse_duration(observe, "observe", 0, 120)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload)
    usable = [(p, first_container(p, container)) for p in running]
    usable = [(p, c) for p, c in usable if c]
    if not usable:
        raise RwError(
            "invalid_input", f"no running pod has a container named {container!r}; nothing was run"
        )
    by_name = {p["metadata"]["name"]: (p, c) for p, c in usable}
    name = pick(list(by_name), 1, seed_value)[0]
    pod, cname = by_name[name]
    path = "/tmp/rw-chaos-" + hashlib.sha256(f"{seed_value}\0{name}".encode()).hexdigest()[:10]
    before = restart_count(pod, cname)
    out = {
        "applied": False,
        "seed": _seed_view(seed_value, seed_source),
        "candidateCount": len(usable),
        "pod": pod_view(pod),
        "container": cname,
        "path": path,
        "bytesRequested": size_mi * MIB,
        "bytesWritten": None,
        "writeExitCode": None,
        "writeError": None,
        "writeErrorTruncated": False,
        "command": {"binary": "sh", "argCount": 5},
        "restartCountBefore": before,
        "restartCountAfter": None,
        "restarted": None,
        "evicted": None,
        "podPresent": None,
        "podPhase": None,
        "cleanup": {"attempted": False, "succeeded": None, "reason": None},
    }
    if ctx.dry_run:
        return {"target": target_view(target, workload), "filltmp": out}

    # assume the write ran until kubectl says it never started; a timeout or crash leaves it unknown
    write_started, pod_state, current = True, "ok", None
    try:
        try:
            result = _exec(ctx, ns, name, cname, _WRITE, path, str(size_mi))
        except RwError as exc:
            # kubectl never started the command: nothing was written, so nothing to clean up
            write_started = exc.code not in ("dependency", "permission_denied", "not_found")
            raise
        out["applied"] = True
        out["writeExitCode"] = _exit_code(result)
        digits = (result.stdout or "").strip().splitlines()
        out["bytesWritten"] = int(digits[-1]) if digits and digits[-1].isdigit() else None
        text, truncated = limit_text((result.stderr or "").strip())
        out["writeError"], out["writeErrorTruncated"] = (text or None), truncated
        current, pod_state = _watch_pod(ctx, ns, name, cname, before, seconds)
    finally:
        if write_started:
            if pod_state == "ok":
                # the pod may have been lost while we watched: look again before the cleanup
                current, pod_state = _refresh_pod(ctx, ns, name, current)
            out["cleanup"] = _cleanup(ctx, ns, name, cname, path, pod_state)
    out["podPresent"] = current is not None
    out["podPhase"] = current.get("status", {}).get("phase") if current else None
    out["evicted"] = pod_state == "evicted"
    if current is not None:
        out["restartCountAfter"] = restart_count(current, cname)
        out["restarted"] = out["restartCountAfter"] > before
    if (
        out["writeExitCode"]
        and not out["bytesWritten"]
        and pod_state == "ok"
        and not out["restarted"]
    ):
        raise RwError(
            "invalid_input",
            f"nothing could be written to {path} in pod {name} ({cname}); "
            f"dd exited {out['writeExitCode']}: {out['writeError'] or ''}"[:MESSAGE_LIMIT],
        )
    return {"target": target_view(target, workload), "filltmp": out}


# ---------------------------------------------------------------- mangle ----


def _patch_service(ctx, ns, name, patch, dry=False):
    argv = ["kubectl", "-n", ns, "patch", "service", name, "--type=merge"]
    argv += ["-p", json.dumps(patch, separators=(",", ":")), "-o", "name"]
    if dry:
        argv.append("--dry-run=server")
    run_checked(ctx, argv)


def _read_ready(ctx, ns, service):
    """Ready endpoints of the Service from its EndpointSlices, or None (reported)."""
    try:
        slices = kubectl_json(
            ctx,
            "-n",
            ns,
            "get",
            "endpointslices.discovery.k8s.io",
            "-l",
            f"kubernetes.io/service-name={service}",
        )["items"]
    except RwError as exc:
        ctx.error(
            exc.code,
            f"could not read the endpoints of service {service}: {exc.message}"[:MESSAGE_LIMIT],
            scope="endpoints",
        )
        return None
    return ready_endpoints(slices)


def _wait_ready(ctx, ns, service, done, seconds):
    """Poll the ready endpoint count until done(count) or `seconds`: the last count."""
    deadline = clock.monotonic() + seconds
    while True:
        count = _read_ready(ctx, ns, service)
        if count is None or done(count) or clock.monotonic() >= deadline:
            return count
        clock.sleep(1.0)


def _pick_services(ctx, target, workload, service):
    """The Service documents to mangle."""
    ns = target["namespace"]
    if workload is None:
        if not service:
            raise RwError(
                "invalid_input",
                "a namespace target needs the service input: the name of the Service to mangle",
            )
        return [kubectl_json(ctx, "-n", ns, "get", "services", service)]
    labels = workload.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}
    if service:
        doc = kubectl_json(ctx, "-n", ns, "get", "services", service)
        if not selects_workload(doc, labels):
            raise RwError(
                "invalid_input",
                f"service {service} does not select the pods of {target['kind']} {target['name']}; nothing was changed",
            )
        return [doc]
    found = matching_services(kubectl_json(ctx, "-n", ns, "get", "services")["items"], labels)
    if not found:
        raise RwError(
            "not_found",
            f"no Service in {ns} selects the pods of {target['kind']} {target['name']}; nothing was changed",
        )
    if len(found) > MAX_SERVICES:
        raise RwError(
            "invalid_input",
            f"{len(found)} Services select this workload (more than {MAX_SERVICES}); name one with the service input",
        )
    return found


def _restore(ctx, ns, state):
    """Put one Service back and verify it. Fills state['restore']; returns an error
    message when the Service is not back as it was, else None."""
    name, original = state["name"], state["original"]
    restore = {"attempted": True, "restored": False, "verified": False, "reason": None}
    state["restore"] = restore
    try:
        current = kubectl_json(ctx, "-n", ns, "get", "services", name)
        if not is_restored(original, current):
            _patch_service(ctx, ns, name, restore_patch(original, current))
            current = kubectl_json(ctx, "-n", ns, "get", "services", name)
        restore["restored"] = restore["verified"] = is_restored(original, current)
        if not restore["verified"]:
            restore["reason"] = "differs_after_restore"
            return restore_failure_message(
                name, ns, original, "it still differs from the original after the restore patch"
            )
    except RwError as exc:
        restore["reason"] = exc.code
        return restore_failure_message(name, ns, original, f"{exc.code}: {exc.message}")
    return None


def mangle_service(ctx, mode="selector", revert_after="60s", service=None):
    if mode not in MODES:
        raise RwError("invalid_input", f"mode must be one of {list(MODES)}, got {mode!r}")
    hold = parse_duration(revert_after, "revertAfter", 1, 600)
    if service is not None and not _NAME.fullmatch(str(service)):
        raise RwError("invalid_input", f"service must be a Service name, got {service!r}")
    target = _target(ctx)
    ns = target["namespace"]
    workload = load_target(ctx, target)
    docs = _pick_services(ctx, target, workload, service)
    states = []
    for doc in docs:
        plan = mutation_plan(doc, mode)
        states.append(
            {
                "name": doc["metadata"]["name"],
                "original": original_spec(doc),
                "plan": plan,
                "readyBefore": None,
                "readyDuring": None,
                "readyAfterRestore": None,
                "restore": {"attempted": False, "restored": None, "verified": None, "reason": None},
                "patched": False,
                "heldMs": None,
            }
        )
    dry = bool(ctx.dry_run)
    for st in states:
        st["readyBefore"] = _read_ready(ctx, ns, st["name"])
    if dry:
        for st in states:
            _patch_service(ctx, ns, st["name"], st["plan"]["patch"], dry=True)
    else:
        _run_mangle(ctx, ns, states, mode, hold)
    return {
        "target": target_view(target, workload),
        "mangle": {
            "applied": not dry,
            "mode": mode,
            "revertAfterMs": _ms(hold),
            "serviceCount": len(states),
            "services": [_service_view(st, dry) for st in states][:50],
        },
    }


def _run_mangle(ctx, ns, states, mode, hold):
    patched_at = None
    try:
        for st in states:
            st["patched"] = True  # from here on a restore is needed, even if the patch call fails
            _patch_service(ctx, ns, st["name"], st["plan"]["patch"])
        patched_at = clock.monotonic()
        for st in states:
            if mode == "selector":
                st["readyDuring"] = _wait_ready(
                    ctx, ns, st["name"], lambda n: n == 0, min(20.0, hold)
                )
            else:
                clock.sleep(min(2.0, hold))
                st["readyDuring"] = _read_ready(ctx, ns, st["name"])
        remaining = hold - (clock.monotonic() - patched_at)
        if remaining > 0:
            clock.sleep(remaining)
    finally:
        held = None if patched_at is None else clock.monotonic() - patched_at
        problems = []
        for st in states:
            if not st["patched"]:
                continue
            st["heldMs"] = None if held is None else _ms(held)
            message = _restore(ctx, ns, st)
            if message:
                problems.append(message)
        if problems:
            raise RwError("internal", " | ".join(problems))
    for st in states:
        if mode == "selector" and st["readyBefore"]:
            st["readyAfterRestore"] = _wait_ready(
                ctx, ns, st["name"], lambda n, b=st["readyBefore"]: n >= b, 20.0
            )
        else:
            st["readyAfterRestore"] = _read_ready(ctx, ns, st["name"])


def _service_view(st, dry):
    plan = st["plan"]
    before, during = st["readyBefore"], st["readyDuring"]
    return {
        "name": st["name"],
        "change": {"field": plan["field"], "from": plan["from"], "to": plan["to"]},
        "readyEndpointsBefore": before,
        "readyEndpointsDuring": None if dry else during,
        "readyEndpointsAfterRestore": None if dry else st["readyAfterRestore"],
        "endpointsDropped": None if dry or before is None or during is None else (during < before),
        "heldMs": st["heldMs"],
        "restore": st["restore"],
    }
