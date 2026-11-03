"""The four chaos actions. Each one picks its victims, acts, observes what happened
and returns facts. Anything that changes the cluster and must be undone is undone in a
`finally` path and the undo is verified."""

import hashlib
import json
import re
import socket
import subprocess

from k8s_chaos import clock
from k8s_chaos.common import (
    MIB,
    RESOURCE,
    allocation_bytes,
    cap,
    check_container_name,
    check_int,
    container_status,
    eligible_pods,
    first_container,
    is_running,
    limit_text,
    memory_limit,
    parse_duration,
    pick,
    pod_view,
    ready_condition,
    restart_count,
    resolve_target,
    seed_of,
    target_view,
)
from k8s_chaos.errors import RwError
from k8s_chaos.fetch import (
    get_pod,
    list_pods,
    list_replicasets,
    load_target,
    pods_selected_by,
)
from k8s_chaos.k8s import (
    MESSAGE_LIMIT,
    exec_failure,
    failure,
    kubectl_argv,
    kubectl_json,
    run_checked,
    run_unchecked,
)
from k8s_chaos.service import (
    ANNOTATION,
    MAX_SERVICES,
    MODES,
    SHARED_LIMIT,
    annotated_original,
    annotation_value,
    check_service_allowed,
    foreign_pods,
    is_restored,
    matching_services,
    mutation_plan,
    original_spec,
    ready_endpoints,
    restore_error,
    restore_failure,
    restore_plan,
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


def _running_pods(ctx, target, workload, include_uncontrolled=False):
    """(all pods of the target, the running ones to act on); a target with none raises not_found.

    A namespace target acts only on pods a controller recreates (`include_uncontrolled` adds
    the ones nothing owns) and never on mirror pods, DaemonSet pods or the pod this task runs
    in (the hostname is the pod name); a workload target's pods are its own by construction.
    """
    pods = list_pods(ctx, target, workload)
    candidates = pods
    if workload is None:
        candidates = eligible_pods(pods, include_uncontrolled, socket.gethostname())
    running = [p for p in candidates if is_running(p)]
    if not running:
        where = (
            "the namespace"
            if target["scope"] == "namespace"
            else f"{target['kind']} {target['name']}"
        )
        raise RwError(
            "not_found",
            f"no running pod in {where} to act on ({len(pods)} pods in any state, "
            f"{len(pods) - len(candidates)} skipped as mirror, DaemonSet, uncontrolled or this "
            "task's own pod); nothing was changed",
        )
    return pods, running


def _seed(ctx, target, seed):
    """(seed, source): the input, else the target's path plus the UTC date of ctx.now()."""
    path = getattr(ctx.resource, "path", None) or (
        f"{target['kind']}/{target['namespace']}/{target['name']}"
    )
    return seed_of(seed, path, ctx.now())


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


def _replicas(doc):
    """spec.replicas of a workload; the API default (1) when unset."""
    replicas = doc.get("spec", {}).get("replicas")
    return 1 if replicas is None else replicas


def _await_recovery(ctx, target, workload, deleted_uids, seconds):
    """Poll until the workload has its desired ready replicas again, none of them a
    pod this task deleted: {waited, recovered, recoveryMs, waitedMs, ...}.

    Each poll reads the workload again: the desired count is its current spec.replicas (an
    autoscaler or a human may have changed it), its status counts only once
    observedGeneration has caught up with the generation, and the ready pods counted are
    the ones the workload owns (selector and ownerReferences), not any pod with its labels.
    """
    ns, kind, name = target["namespace"], target["kind"], target["name"]
    result = {
        "waited": True,
        "recovered": False,
        "recoveryMs": None,
        "waitedMs": 0,
        "desiredReplicas": _replicas(workload),
        "readyReplicasBefore": workload.get("status", {}).get("readyReplicas") or 0,
        "readyReplicasAfter": None,
    }
    started = clock.monotonic()
    try:
        while True:
            current = kubectl_json(ctx, "-n", ns, "get", RESOURCE[kind], name)
            owned = list_pods(ctx, target, current)
            elapsed = clock.monotonic() - started
            desired = _replicas(current)
            status = current.get("status", {})
            status_ready = status.get("readyReplicas") or 0
            settled = (status.get("observedGeneration") or 0) >= (
                current["metadata"].get("generation") or 0
            )
            result["desiredReplicas"] = desired
            result["readyReplicasAfter"] = status_ready
            fresh_ready = sum(
                1
                for p in owned
                if is_running(p) and ready_condition(p) and p["metadata"]["uid"] not in deleted_uids
            )
            if settled and desired > 0 and status_ready >= desired and fresh_ready >= desired:
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


def delete_pods(
    ctx,
    count=1,
    wait_for_recovery=True,
    timeout="120s",
    seed=None,
    include_uncontrolled=False,
):
    count = check_int(count, "count", 1, 10)
    seconds = parse_duration(timeout, "timeout", 1, 300)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, target, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload, include_uncontrolled)
    by_name = _by_name(running)
    chosen = pick(list(by_name), count, seed_value)
    dry = bool(ctx.dry_run)

    argv = kubectl_argv("-n", ns, "delete", "pod", *chosen, "--wait=false", "--ignore-not-found")
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
    argv = kubectl_argv(
        "-n", ns, "exec", pod, "-c", container, "--", "sh", "-c", script, "sh", *args
    )
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


def _can_exec(ctx, ns):
    """Raise permission_denied when this identity may not `create pods/exec` in `ns`.

    A dry run runs no exec, so without this it would pass for an identity whose real run is
    forbidden. `kubectl auth can-i` is a SelfSubjectAccessReview, which every authenticated
    user may create (the system:basic-user role), so it needs no entry in the task's permissions.
    """
    argv = kubectl_argv("auth", "can-i", "create", "pods/exec", "-n", ns)
    result = run_unchecked(ctx, argv)
    answer = (result.stdout or "").strip().lower()
    if answer == "yes" and not result.returncode:
        return
    if answer == "no":
        raise RwError(
            "permission_denied",
            f"not allowed to create pods/exec in namespace {ns}; the real run would be forbidden",
            hint=f"grant create on pods/exec (and get on pods) through a Role in {ns}",
        )
    if result.returncode:
        raise failure(argv, result)
    raise RwError("internal", f"kubectl auth can-i answered {answer[:80]!r}, not yes or no")


def oomkill_pod(ctx, container=None, timeout="30s", seed=None, include_uncontrolled=False):
    container = check_container_name(container)
    seconds = parse_duration(timeout, "timeout", 1, 120)
    max_mi = int(ctx.settings.max_memory_limit_mi)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, target, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload, include_uncontrolled)
    candidates = _oom_candidates(running, container)
    if not candidates:
        what = f"container {container!r}" if container else "any container"
        raise RwError(
            "invalid_input",
            f"none of the {len(running)} running pods has {what} with a memory limit; "
            "without a limit the container cannot be OOM-killed by this task, so nothing was run",
        )
    # The allocation (limit plus a quarter, at least 64Mi) is one dd block: `bs=N count=1`
    # reads N bytes in a single read(), and dd cannot take a block of 2GiB or more (busybox and
    # GNU both cap bs below that). maxMemoryLimitMi defaults to 1536, whose allocation is
    # 1920Mi, so a larger limit is refused here as invalid_input rather than failing in dd.
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
        "execKilled": None,
        "observedMs": None,
        "podPresent": None,
    }
    if ctx.dry_run:
        _can_exec(ctx, ns)
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
    # dd was killed (137) but the container was not: the kernel killed one process, not the
    # container (cgroup v1, or no memory.oom.group), so the limit held and nothing restarted
    out["execKilled"] = out["exitCode"] == 137 and not out["oomKilled"]
    if exec_error is not None and not out["oomKilled"]:
        raise exec_error
    return {"target": target_view(target, workload), "oomkill": out}


# ---------------------------------------------------------------- fill tmp ----

_WRITE = 'dd if=/dev/zero of="$1" bs=1M count="$2"; rc=$?; wc -c < "$1"; exit $rc'
_CLEAN = 'rm -f -- "$1"; test ! -e "$1"'


CLEANUP_SECONDS = 30


def _restarting(pod, container, before):
    """True when the container restarted since `before` or is not running now (restarting,
    waiting out a back-off): an exec into it fails until it is back."""
    status = container_status(pod, container) if pod is not None else None
    if status is None:
        return False
    return restart_count(pod, container) > before or "running" not in (status.get("state") or {})


def _cleanup(ctx, ns, pod, container, path, pod_state, before=0):
    """Remove the file and check it is gone: {attempted, succeeded, reason}.

    When the container restarted or is restarting the exec cannot get in yet, so the removal
    is retried for up to CLEANUP_SECONDS until it runs; if it never does the reason is
    `container_restarting` and a `cleanup` error with a hint says how to remove the file.
    `pod_gone` means a read of the pod said not_found; a terminating pod is `pod_terminating`.
    """
    if pod_state in ("evicted", "terminating", "gone"):
        return {"attempted": False, "succeeded": False, "reason": f"pod_{pod_state}"}
    hint = f"remove the file by hand: kubectl -n {ns} exec {pod} -c {container} -- rm -f {path}"
    deadline = clock.monotonic() + CLEANUP_SECONDS
    while True:
        try:
            result = _exec(ctx, ns, pod, container, _CLEAN, path, seconds=60)
        except RwError as exc:
            current = None
            try:
                current = get_pod(ctx, ns, pod)
            except RwError as read:
                if read.code == "not_found":
                    return {"attempted": True, "succeeded": False, "reason": "pod_gone"}
            if current is not None and _pod_state(current) != "ok":
                return {
                    "attempted": True,
                    "succeeded": False,
                    "reason": f"pod_{_pod_state(current)}",
                }
            if exc.code in ("permission_denied", "dependency") or not _restarting(
                current, container, before
            ):
                ctx.error(
                    exc.code,
                    f"could not remove {path} from {ns}/{pod} ({container}): {exc.message}"[
                        :MESSAGE_LIMIT
                    ],
                    scope="cleanup",
                    hint=hint,
                )
                return {"attempted": True, "succeeded": False, "reason": exc.code}
            if clock.monotonic() >= deadline:
                ctx.error(
                    exc.code,
                    f"could not remove {path} from {ns}/{pod} ({container}): the container was "
                    f"restarting for {CLEANUP_SECONDS}s and the exec never got in: "
                    f"{exc.message}"[:MESSAGE_LIMIT],
                    scope="cleanup",
                    hint=hint,
                )
                return {"attempted": True, "succeeded": False, "reason": "container_restarting"}
            clock.sleep(min(POLL_SECONDS, max(deadline - clock.monotonic(), 0.1)))
            continue
        if result.returncode:
            ctx.error(
                "internal",
                f"{path} is still in {ns}/{pod} ({container}) after rm (exit {result.returncode})",
                scope="cleanup",
                hint=hint,
            )
            return {"attempted": True, "succeeded": False, "reason": "still_present"}
        return {"attempted": True, "succeeded": True, "reason": None}


def _pod_state(pod):
    """'ok', 'evicted' (Failed/Evicted) or 'terminating' (deletionTimestamp set)."""
    status = pod.get("status", {})
    if status.get("reason") == "Evicted" or status.get("phase") == "Failed":
        return "evicted"
    if pod["metadata"].get("deletionTimestamp") is not None:
        return "terminating"
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


def fill_tmp(
    ctx, size_mi=100, container=None, observe="10s", seed=None, include_uncontrolled=False
):
    size_mi = check_int(size_mi, "sizeMi", 1, 1024)
    container = check_container_name(container)
    seconds = parse_duration(observe, "observe", 0, 120)
    target = _target(ctx)
    ns = target["namespace"]
    seed_value, seed_source = _seed(ctx, target, seed)
    workload = load_target(ctx, target)
    _, running = _running_pods(ctx, target, workload, include_uncontrolled)
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
        _can_exec(ctx, ns)
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
            out["cleanup"] = _cleanup(ctx, ns, name, cname, path, pod_state, before)
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

# Restore: one try plus three retries, waiting 1, 3 and 6 seconds before them, and no new try
# after RESTORE_SECONDS from the first (each try is at most three kubectl calls of 30s).
RESTORE_BACKOFF = (1, 3, 6)
RESTORE_SECONDS = 90


def _is_conflict(exc):
    text = f"{exc.code} {exc.message}".lower()
    return "conflict" in text or "the object has been modified" in text


def _patch_service(ctx, ns, name, patch, dry=False):
    argv = kubectl_argv("-n", ns, "patch", "service", name, "--type=merge")
    argv += ["-p", json.dumps(patch, separators=(",", ":")), "-o", "name"]
    if dry:
        argv.append("--dry-run=server")
    run_checked(ctx, argv)


def _mangle_patch(ctx, ns, name, patch, dry=False):
    """The mangle patch; a Service changed since it was read is a conflict, which aborts."""
    try:
        _patch_service(ctx, ns, name, patch, dry)
    except RwError as exc:
        if exc.code == "internal" and _is_conflict(exc):
            raise RwError(
                "internal",
                f"concurrent change: service {ns}/{name} was modified after it was read; "
                "nothing was mangled on it",
                scope="mangle",
                details={"service": name, "namespace": ns},
            ) from None
        raise


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


def _pick_services(ctx, target, workload, service, recover=False):
    """The Service documents to act on (to mangle; with `recover`, to restore)."""
    ns = target["namespace"]
    if workload is None:
        if not service:
            raise RwError(
                "invalid_input",
                "a namespace target needs the service input: the name of the Service to mangle",
            )
        check_service_allowed(ns, service)
        return [kubectl_json(ctx, "-n", ns, "get", "services", service)]
    labels = workload.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}
    if service:
        check_service_allowed(ns, service)
        doc = kubectl_json(ctx, "-n", ns, "get", "services", service)
        if not selects_workload(doc, labels):
            raise RwError(
                "invalid_input",
                f"service {service} does not select the pods of {target['kind']} {target['name']}; nothing was changed",
            )
        return [doc]
    found = matching_services(kubectl_json(ctx, "-n", ns, "get", "services")["items"], labels)
    for doc in found:
        check_service_allowed(ns, doc["metadata"]["name"])
    if recover:
        found = [d for d in found if annotation_value(d) is not None]
    if not found:
        if recover:
            return []
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


def _shared_with(ctx, target, workload, docs, allow_shared):
    """{service name: pods the Service selects that the workload does not own}.

    A Service shared with other workloads would cut them off too: refused as invalid_input
    (naming them in details.sharedWith) unless `allowShared`. A namespace target has no workload
    whose pods to compare with, so the Service it names is the caller's choice.
    """
    if workload is None:
        return {doc["metadata"]["name"]: [] for doc in docs}
    ns = target["namespace"]
    replicasets = list_replicasets(ctx, target, workload)
    out = {}
    for doc in docs:
        name = doc["metadata"]["name"]
        selector = doc["spec"].get("selector") or {}
        selected = pods_selected_by(ctx, ns, selector) if selector else []
        out[name] = foreign_pods(target["kind"], workload, replicasets, selected)
    refused = {name: pods for name, pods in out.items() if pods}
    if refused and not allow_shared:
        listing = ", ".join(f"{name} ({len(pods)} pods)" for name, pods in sorted(refused.items()))
        raise RwError(
            "invalid_input",
            f"{target['kind']} {target['name']} does not own all the pods its Services select "
            f"({listing}); mangling would cut the others off too. Nothing was changed; set "
            "allowShared: true to mangle anyway",
            details={
                "sharedWith": {n: pods[:SHARED_LIMIT] for n, pods in sorted(refused.items())},
                "namespace": ns,
            },
        )
    return {name: pods[:SHARED_LIMIT] for name, pods in out.items()}


def _restore_once(ctx, ns, state):
    """One try at putting the Service back: reads it, patches only the fields that still hold a
    mangled value, reads it again. Returns the plan; raises on a failed call."""
    name, original = state["name"], state["original"]
    current = kubectl_json(ctx, "-n", ns, "get", "services", name)
    plan = restore_plan(original, current, state.get("expected"))
    if plan["patch"] is not None:
        _patch_service(ctx, ns, name, plan["patch"])
        current = kubectl_json(ctx, "-n", ns, "get", "services", name)
    plan["verified"] = not plan["concurrent"] and is_restored(original, current)
    return plan


def _restore(ctx, ns, state):
    """Put one Service back and verify it. Fills state['restore']; returns None when it is
    back as it was, else (message, details entry) for the error.

    Never raises: a failed call (timeout, OS error, API error) is tried again after 1, 3 and
    6 seconds, within RESTORE_SECONDS; a field someone else changed in the meantime is not ours
    to overwrite (`concurrentChange`: the annotation stays for a human) and is not retried.
    """
    name, original = state["name"], state["original"]
    restore = state["restore"] = {
        "attempted": True,
        "restored": False,
        "verified": False,
        "reason": None,
        "concurrentChange": False,
    }
    started = clock.monotonic()
    why, concurrent = "", ()
    for attempt in range(len(RESTORE_BACKOFF) + 1):
        try:
            plan = _restore_once(ctx, ns, state)
        except Exception as exc:  # TimeoutExpired, OSError, RwError: any failed call is retried
            if isinstance(exc, RwError):
                restore["reason"], why = exc.code, f"{exc.code}: {exc.message}"
            elif isinstance(exc, subprocess.TimeoutExpired):
                restore["reason"], why = "timeout", "timeout: kubectl did not finish"
            else:
                restore["reason"], why = "internal", f"{type(exc).__name__}: {exc}"
        else:
            if plan["verified"]:
                restore.update(restored=True, verified=True, reason=None)
                return None
            if plan["concurrent"]:
                concurrent = tuple(plan["concurrent"])
                restore.update(reason="concurrent_change", concurrentChange=True)
                why = "concurrent change: " + ", ".join(concurrent) + " changed by someone else"
                break
            restore["reason"], why = (
                "differs_after_restore",
                "it still differs from the original after the restore patch",
            )
        if attempt == len(RESTORE_BACKOFF):
            break
        delay = RESTORE_BACKOFF[attempt]
        if clock.monotonic() - started + delay > RESTORE_SECONDS:
            break
        clock.sleep(delay)
    return restore_failure(name, ns, original, why, concurrent)


def mangle_service(
    ctx, mode="selector", revert_after="60s", service=None, recover=False, allow_shared=False
):
    if mode not in MODES:
        raise RwError("invalid_input", f"mode must be one of {list(MODES)}, got {mode!r}")
    hold = parse_duration(revert_after, "revertAfter", 1, 600)
    if service is not None and not _NAME.fullmatch(str(service)):
        raise RwError("invalid_input", f"service must be a Service name, got {service!r}")
    target = _target(ctx)
    ns = target["namespace"]
    if service:
        check_service_allowed(ns, service)  # before any call: no input overrides this
    workload = load_target(ctx, target)
    docs = _pick_services(ctx, target, workload, service, recover)
    dry = bool(ctx.dry_run)
    if recover:
        docs = [d for d in docs if annotation_value(d) is not None]
        return _recover(ctx, target, workload, docs, dry)
    shared = _shared_with(ctx, target, workload, docs, bool(allow_shared))
    states = []
    for doc in docs:
        plan = mutation_plan(doc, mode)
        states.append(
            {
                "name": doc["metadata"]["name"],
                "original": original_spec(doc),
                "plan": plan,
                "expected": {"mode": plan["mode"], "to": plan["to"]},
                "sharedWith": shared[doc["metadata"]["name"]],
                "readyBefore": None,
                "readyDuring": None,
                "readyAfterRestore": None,
                "restore": _no_restore(),
                "patched": False,
                "heldMs": None,
            }
        )
    for st in states:
        st["readyBefore"] = _read_ready(ctx, ns, st["name"])
    if dry:
        for st in states:
            _mangle_patch(ctx, ns, st["name"], st["plan"]["patch"], dry=True)
    else:
        _run_mangle(ctx, ns, states, mode, hold)
    return {
        "target": target_view(target, workload),
        "mangle": {
            "applied": not dry,
            "recover": False,
            "mode": mode,
            "revertAfterMs": _ms(hold),
            "serviceCount": len(states),
            "services": [_service_view(st, dry) for st in states][:50],
        },
    }


def _no_restore():
    return {
        "attempted": False,
        "restored": None,
        "verified": None,
        "reason": None,
        "concurrentChange": False,
    }


def _run_mangle(ctx, ns, states, mode, hold):
    patched_at = None
    try:
        for st in states:
            st["patched"] = True  # from here on a restore is needed, even if the patch call fails
            _mangle_patch(ctx, ns, st["name"], st["plan"]["patch"])
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
        _restore_all(ctx, ns, states, patched_at)
    for st in states:
        if mode == "selector" and st["readyBefore"]:
            st["readyAfterRestore"] = _wait_ready(
                ctx, ns, st["name"], lambda n, b=st["readyBefore"]: n >= b, 20.0
            )
        else:
            st["readyAfterRestore"] = _read_ready(ctx, ns, st["name"])


def _restore_all(ctx, ns, states, patched_at, failures=None):
    """Restore every Service that was (or may have been) patched, each on its own: one that
    cannot be restored never skips the others. Raises one `internal` error for all that failed
    (`failures`: ones already known to have failed)."""
    held = None if patched_at is None else clock.monotonic() - patched_at
    failures = list(failures or [])
    for st in states:
        if not st["patched"]:
            continue
        st["heldMs"] = None if held is None else _ms(held)
        problem = _restore(ctx, ns, st)
        if problem:
            failures.append(problem)
    if failures:
        raise restore_error(failures)


def _recover(ctx, target, workload, docs, dry):
    """Put back every Service that still carries the annotation of an earlier run that died,
    from that annotation; mangles nothing. A dry run sends the restore patch server-side.
    A Service that cannot be restored never skips the others; one `internal` error names all."""
    ns = target["namespace"]
    states, failures = [], []
    for doc in docs:
        name = doc["metadata"]["name"]
        original = annotated_original(doc)
        if original is None:
            failures.append(
                restore_failure(
                    name,
                    ns,
                    {"raw": (annotation_value(doc) or "")[:1024]},
                    "the annotation is not a recorded spec",
                )
            )
            continue
        plan = restore_plan(original, doc)
        states.append(
            {
                "name": name,
                "original": original,
                "plan": {"mode": None, **plan["changes"][0]} if plan["changes"] else None,
                "recovery": plan,
                "expected": None,
                "sharedWith": [],
                "readyBefore": None,
                "readyDuring": None,
                "readyAfterRestore": None,
                "restore": _no_restore(),
                "patched": True,
                "heldMs": None,
            }
        )
    for st in states:
        st["readyBefore"] = _read_ready(ctx, ns, st["name"])
    if dry:
        for st in states:
            plan = st["recovery"]
            if plan["concurrent"]:
                st["restore"]["concurrentChange"] = True
                failures.append(
                    restore_failure(
                        st["name"],
                        ns,
                        st["original"],
                        "concurrent change: "
                        + ", ".join(plan["concurrent"])
                        + " changed by someone else",
                        plan["concurrent"],
                    )
                )
            elif plan["patch"] is not None:
                _patch_service(ctx, ns, st["name"], plan["patch"], dry=True)
        if failures:
            raise restore_error(failures)
    else:
        _restore_all(ctx, ns, states, None, failures)
        for st in states:
            st["readyAfterRestore"] = _read_ready(ctx, ns, st["name"])
    return {
        "target": target_view(target, workload),
        "mangle": {
            "applied": not dry and bool(states),
            "recover": True,
            "mode": None,
            "revertAfterMs": None,
            "serviceCount": len(states),
            "services": [_service_view(st, dry) for st in states][:50],
        },
    }


def _service_view(st, dry):
    plan = st["plan"] or {
        "field": f"metadata.annotations[{ANNOTATION}]",
        "from": "present",
        "to": "removed",
    }
    before, during = st["readyBefore"], st["readyDuring"]
    return {
        "name": st["name"],
        "change": {"field": plan["field"], "from": plan["from"], "to": plan["to"]},
        "sharedWith": st["sharedWith"],
        "readyEndpointsBefore": before,
        "readyEndpointsDuring": None if dry else during,
        "readyEndpointsAfterRestore": None if dry else st["readyAfterRestore"],
        "endpointsDropped": None if dry or before is None or during is None else (during < before),
        "heldMs": st["heldMs"],
        "restore": st["restore"],
    }
