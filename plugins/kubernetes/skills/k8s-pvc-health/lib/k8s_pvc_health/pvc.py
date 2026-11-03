"""Pure functions behind the k8s-pvc-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

import re
from datetime import datetime, timedelta, timezone

from k8s_pvc_health.errors import RwError

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field

# ---------------------------------------------------------------- time ----


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises RwError(invalid_input)."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError("invalid_input", f"not a duration: {text!r}")
    return timedelta(milliseconds=sum(int(n) * _UNIT_MS[u] for n, u in parts))


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


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


def nested(items):
    """Bound a list nested inside an output entry to NESTED_LIMIT entries."""
    return list(items)[:NESTED_LIMIT]


def trunc(text):
    """(text cut to TEXT_LIMIT characters, whether it was cut). None stays None, not cut."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:TEXT_LIMIT], True) if len(text) > TEXT_LIMIT else (text, False)


# ------------------------------------------------------------ quantity ----

_QUANTITY = re.compile(r"^([+-]?\d+(?:\.\d+)?)(?:[eE]([+-]?\d+)|([A-Za-z]{0,2}))$")
_SUFFIX = {
    "": 1,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def parse_quantity(text):
    """A Kubernetes quantity ('100Mi', '1.5Gi', '500M', '1e9') -> bytes (int), or None for no value.

    Every suffix is understood: the classic check only converted Gi/Ti/Pi and
    treated a Mi- or Ki-sized claim as zero bytes.
    """
    if text is None or text == "":
        return None
    m = _QUANTITY.match(str(text).strip())
    if not m:
        raise ValueError(f"not a quantity: {text!r}")
    number, exp, suffix = m.groups()
    if exp is not None:
        return int(float(number) * 10 ** int(exp))
    if suffix not in _SUFFIX:
        raise ValueError(f"not a quantity: {text!r}")
    return int(round(float(number) * _SUFFIX[suffix]))


# --------------------------------------------------------------- claim ----


def _conditions(claim):
    rows = []
    for c in (claim.get("status") or {}).get("conditions") or []:
        message, cut = trunc(c.get("message"))
        rows.append(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": message,
                "truncated": cut,
                "lastTransition": c.get("lastTransitionTime"),
            }
        )
    return nested(rows)


def claim_view(claim):
    """Phase, class, volume, requested vs bound capacity and resize conditions of the claim."""
    spec = claim.get("spec") or {}
    status = claim.get("status") or {}
    requested = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    bound = (status.get("capacity") or {}).get("storage")
    requested_bytes = parse_quantity(requested)
    bound_bytes = parse_quantity(bound)
    return {
        "name": claim["metadata"]["name"],
        "namespace": claim["metadata"].get("namespace"),
        "phase": status.get("phase"),
        "storageClassName": spec.get("storageClassName"),
        "volumeName": spec.get("volumeName") or None,
        "volumeMode": spec.get("volumeMode"),
        "accessModes": nested(spec.get("accessModes") or []),
        "capacity": {
            "requested": requested,
            "requestedBytes": requested_bytes,
            "bound": bound,
            "boundBytes": bound_bytes,
            "boundBelowRequested": bound_bytes is not None
            and requested_bytes is not None
            and bound_bytes < requested_bytes,
        },
        "conditions": _conditions(claim),
        "deletionTimestamp": (claim.get("metadata") or {}).get("deletionTimestamp"),
    }


def storage_class_view(name, sc, looked):
    """The claim's StorageClass. `exists` is True/False when it was looked up, else None.

    `looked` is False when the claim names no class or the read was denied
    (then the caller has put permission_denied on the result).
    """
    view = {
        "name": name or None,
        "exists": None,
        "isDefault": None,
        "provisioner": None,
        "volumeBindingMode": None,
        "reclaimPolicy": None,
        "allowVolumeExpansion": None,
    }
    if not looked or not name:
        return view
    view["exists"] = sc is not None
    if sc is not None:
        annotations = (sc.get("metadata") or {}).get("annotations") or {}
        view.update(
            isDefault=annotations.get("storageclass.kubernetes.io/is-default-class") == "true",
            provisioner=sc.get("provisioner"),
            volumeBindingMode=sc.get("volumeBindingMode"),
            reclaimPolicy=sc.get("reclaimPolicy"),
            allowVolumeExpansion=bool(sc.get("allowVolumeExpansion")),
        )
    return view


def _volume_source(pv_spec):
    csi = pv_spec.get("csi")
    if csi:
        return {"type": "csi", "driver": csi.get("driver")}
    for key in sorted(pv_spec):
        if key in (
            "local",
            "hostPath",
            "nfs",
            "iscsi",
            "fc",
            "awsElasticBlockStore",
            "gcePersistentDisk",
            "azureDisk",
            "azureFile",
            "cephfs",
            "rbd",
        ):
            return {"type": key, "driver": None}
    return {"type": None, "driver": None}


def volume_view(claim, pv, looked):
    """The PersistentVolume the claim is bound to, or None when the claim names none.

    `exists` is True/False when it was looked up, else None. A PV that is being
    deleted shows as `terminating` (its deletionTimestamp): a PV has no
    Terminating phase, kubectl derives that column from the timestamp.
    """
    name = (claim.get("spec") or {}).get("volumeName")
    if not name:
        return None
    view = {
        "name": name,
        "exists": None,
        "phase": None,
        "capacity": None,
        "capacityBytes": None,
        "reclaimPolicy": None,
        "storageClassName": None,
        "accessModes": None,
        "claimRef": None,
        "claimRefMatches": None,
        "source": None,
        "terminating": None,
        "deletionTimestamp": None,
        "finalizers": None,
    }
    if not looked:
        return view
    view["exists"] = pv is not None
    if pv is None:
        return view
    spec = pv.get("spec") or {}
    meta = pv.get("metadata") or {}
    ref = spec.get("claimRef") or {}
    capacity = (spec.get("capacity") or {}).get("storage")
    view.update(
        phase=(pv.get("status") or {}).get("phase"),
        capacity=capacity,
        capacityBytes=parse_quantity(capacity),
        reclaimPolicy=spec.get("persistentVolumeReclaimPolicy"),
        storageClassName=spec.get("storageClassName"),
        accessModes=nested(spec.get("accessModes") or []),
        claimRef={"namespace": ref.get("namespace"), "name": ref.get("name"), "uid": ref.get("uid")}
        if ref
        else None,
        claimRefMatches=bool(ref)
        and ref.get("name") == claim["metadata"]["name"]
        and ref.get("namespace") == claim["metadata"].get("namespace")
        and (not ref.get("uid") or ref.get("uid") == claim["metadata"].get("uid")),
        source=_volume_source(spec),
        terminating=meta.get("deletionTimestamp") is not None,
        deletionTimestamp=meta.get("deletionTimestamp"),
        finalizers=nested(meta.get("finalizers") or []),
    )
    return view


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


def claim_events(events, claim, since, now, max_items):
    """Events about this claim whose last occurrence falls in the window, grouped
    by type and reason (newest first, capped).

    Normal events are kept: a Pending claim waiting for its first consumer has
    only a Normal `WaitForFirstConsumer` event, which is the difference between
    waiting and broken. Events of an earlier claim with the same name (other
    uid) are left out. `count` sums the events' own lifetime counters.
    """
    name = claim["metadata"]["name"]
    uid = claim["metadata"].get("uid")
    cutoff = now - parse_duration(since)
    groups = {}
    for ev in events:
        obj = ev.get("involvedObject") or {}
        if obj.get("kind") != "PersistentVolumeClaim" or obj.get("name") != name:
            continue
        if uid and obj.get("uid") and obj["uid"] != uid:
            continue
        first, last = _event_times(ev)
        if last is None or last < cutoff:
            continue
        series = ev.get("series") or {}
        count = series.get("count") or ev.get("count") or 1
        key = (ev.get("type"), ev.get("reason"))
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "type": ev.get("type"),
                "reason": ev.get("reason"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "truncated": False,
                "reportingComponent": ev.get("reportingComponent")
                or (ev.get("source") or {}).get("component"),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last >= g["lastSeen"]:
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["reason"] or ""), reverse=True)
    rows = cap(rows, max_items)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = trunc(g["message"])
    return rows


# ---------------------------------------------------------- terminating ----


def termination(claim, now):
    """Whether the claim is being deleted, since when, and what holds it."""
    meta = claim["metadata"]
    stamp = parse_time(meta.get("deletionTimestamp"))
    return {
        "name": meta["name"],
        "phase": (claim.get("status") or {}).get("phase"),
        "terminating": stamp is not None,
        "deletionTimestamp": fmt_time(stamp),
        "terminatingSeconds": max(0, int((now - stamp).total_seconds())) if stamp else None,
        "finalizers": nested(meta.get("finalizers") or []),
        "gracePeriodSeconds": meta.get("deletionGracePeriodSeconds"),
    }


# ---------------------------------------------------------------- pods ----


def _claim_volumes(pod, claim_name):
    """Names of the pod's volumes that are this claim (a claim reference or a generic ephemeral volume)."""
    pod_name = pod["metadata"]["name"]
    names = set()
    for vol in (pod.get("spec") or {}).get("volumes") or []:
        ref = (vol.get("persistentVolumeClaim") or {}).get("claimName")
        if ref == claim_name or (
            vol.get("ephemeral") is not None and f"{pod_name}-{vol['name']}" == claim_name
        ):
            names.add(vol["name"])
    return names


def _container_ready(pod):
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in (pod.get("status") or {}).get("conditions") or []
    )


def mounting_pods(pods, claim_name):
    """Pods in the namespace that mount the claim, with where each container mounts it.

    `active` is false for Succeeded and Failed pods: they no longer hold the
    claim, so they do not block its deletion. Sorted by name.
    """
    rows = []
    for pod in pods:
        volumes = _claim_volumes(pod, claim_name)
        if not volumes:
            continue
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        mounts = []
        for init, key in ((False, "containers"), (True, "initContainers")):
            for c in spec.get(key) or []:
                for vm in c.get("volumeMounts") or []:
                    if vm.get("name") in volumes:
                        mounts.append(
                            {
                                "container": c["name"],
                                "init": init,
                                "volume": vm["name"],
                                "mountPath": vm.get("mountPath"),
                                "readOnly": bool(vm.get("readOnly")),
                            }
                        )
        phase = status.get("phase")
        rows.append(
            {
                "name": pod["metadata"]["name"],
                "phase": phase,
                "ready": _container_ready(pod),
                "node": spec.get("nodeName"),
                "active": phase not in ("Succeeded", "Failed"),
                "terminating": pod["metadata"].get("deletionTimestamp") is not None,
                "mounts": nested(mounts),
            }
        )
    return sorted(rows, key=lambda r: r["name"])


# --------------------------------------------------------------- usage ----


def pick_usage_target(pods, pod_objects):
    """(pod, container, mountPath) to run df in, or None.

    A Running, not-terminating pod from `pods` (mounting_pods output) with a
    running regular container that mounts the claim; ready pods first.
    `pod_objects` maps pod name -> the Pod, for the container states.
    """
    ordered = sorted(
        (p for p in pods if p["phase"] == "Running" and not p["terminating"]),
        key=lambda p: (not p["ready"], p["name"]),
    )
    for p in ordered:
        statuses = {
            s["name"]: s
            for s in (pod_objects[p["name"]].get("status") or {}).get("containerStatuses") or []
        }
        for m in p["mounts"]:
            if m["init"]:
                continue
            if "running" in ((statuses.get(m["container"]) or {}).get("state") or {}):
                return p["name"], m["container"], m["mountPath"]
    return None


_DF_LINE = re.compile(
    r"^(?P<fs>.+?)\s+(?P<blocks>\d+)\s+(?P<used>\d+)\s+(?P<avail>\d+)\s+(?P<cap>\d+%|-)\s+(?P<mount>/.*)$"
)


def parse_df(stdout):
    """`df -Pk <path>` output -> {filesystem, mountedOn, sizeBytes, usedBytes, availableBytes}.

    Raises ValueError on anything that is not POSIX df output: an unreadable
    result is an error, never zero usage.
    """
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"unexpected df output: {stdout.strip()[:200]!r}")
    m = _DF_LINE.match(lines[-1].strip())
    if not m:
        raise ValueError(f"unexpected df output: {lines[-1].strip()[:200]!r}")
    return {
        "filesystem": m["fs"],
        "mountedOn": m["mount"],
        "sizeBytes": int(m["blocks"]) * 1024,
        "usedBytes": int(m["used"]) * 1024,
        "availableBytes": int(m["avail"]) * 1024,
    }


def usage_view(df, pod, container, mount_path, running_pods, threshold_percent):
    """Usage of the mounted volume with the `atOrAbove` flag against the setting.

    percent is used / (used + available), the share of what a non-root writer
    can use (df's own Use% column, unrounded to one decimal).
    """
    total = df["usedBytes"] + df["availableBytes"]
    percent = round(df["usedBytes"] * 100 / total, 1) if total else None
    return {
        "pod": pod,
        "container": container,
        "mountPath": mount_path,
        "filesystem": df["filesystem"],
        "sizeBytes": df["sizeBytes"],
        "usedBytes": df["usedBytes"],
        "availableBytes": df["availableBytes"],
        "percent": percent,
        "thresholdPercent": threshold_percent,
        "atOrAbove": percent >= threshold_percent if percent is not None else None,
        "runningPods": running_pods,
    }


# ---------------------------------------------------------- attachment ----


def mounting_nodes(pods):
    """(live, terminating): sorted node names of active mounting pods, split by deletionTimestamp.

    Succeeded and Failed pods hold nothing. A terminating pod still holds the
    volume until it is gone, but it is on its way out, so it is kept apart.
    """
    live = {p["node"] for p in pods if p["active"] and not p["terminating"] and p["node"]}
    going = {p["node"] for p in pods if p["active"] and p["terminating"] and p["node"]}
    return sorted(live), sorted(going)


def attachment_access(claim, pods):
    """Access modes and the nodes live pods mount the claim from; `pods` None means unknown.

    `multiNodeConflict` is true for a ReadWriteOnce claim (and not also
    ReadWriteMany or ReadOnlyMany) that live pods mount from more than one
    node, and for a ReadWriteOncePod claim that more than one live pod mounts:
    the second pod cannot start or loses the volume. Terminating pods are not
    counted (the conflict is transient); their nodes are in `terminatingNodes`.
    The conflict is computed from every node; `nodes` is capped, `nodeCount`
    is the full count.
    """
    spec = claim.get("spec") or {}
    modes = list(spec.get("accessModes") or [])
    shared = {"ReadWriteMany", "ReadOnlyMany"} & set(modes)
    rwo = "ReadWriteOnce" in modes and not shared
    rwop = "ReadWriteOncePod" in modes
    out = {
        "accessModes": nested(modes),
        "readWriteOnce": rwo,
        "readWriteOncePod": rwop,
        "volumeName": spec.get("volumeName") or None,
        "nodes": None,
        "nodeCount": None,
        "truncated": None,
        "terminatingNodes": None,
        "multiNodeConflict": None,
    }
    if pods is None:
        return out
    live, going = mounting_nodes(pods)
    live_pods = sum(1 for p in pods if p["active"] and not p["terminating"])
    out.update(
        nodes=nested(live),
        nodeCount=len(live),
        truncated=len(live) > NESTED_LIMIT,
        terminatingNodes=nested(going),
        multiNodeConflict=(rwo and len(live) > 1) or (rwop and live_pods > 1),
    )
    return out


def volume_attachments(items, pv_name, nodes):
    """VolumeAttachments for the PV: node, attached state and attach/detach errors.

    `nodes` is every node with an active mounting pod (live or terminating), or
    None when the pods could not be listed (nodeHasMountingPod is then null).
    """
    rows = []
    for va in items:
        spec = va.get("spec") or {}
        if (spec.get("source") or {}).get("persistentVolumeName") != pv_name:
            continue
        status = va.get("status") or {}
        attach_err = status.get("attachError") or {}
        detach_err = status.get("detachError") or {}
        attach_msg, attach_cut = trunc(attach_err.get("message"))
        detach_msg, detach_cut = trunc(detach_err.get("message"))
        rows.append(
            {
                "name": va["metadata"]["name"],
                "node": spec.get("nodeName"),
                "attacher": spec.get("attacher"),
                "attached": bool(status.get("attached")),
                "attachError": attach_msg,
                "attachErrorTime": attach_err.get("time"),
                "detachError": detach_msg,
                "truncated": attach_cut or detach_cut,
                "detachErrorTime": detach_err.get("time"),
                "nodeHasMountingPod": None if nodes is None else spec.get("nodeName") in nodes,
                "terminating": va["metadata"].get("deletionTimestamp") is not None,
            }
        )
    return sorted(rows, key=lambda r: (r["node"] or "", r["name"]))
