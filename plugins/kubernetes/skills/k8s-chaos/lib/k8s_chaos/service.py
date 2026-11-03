"""Pure functions for mangle-service: which Services, what to change, what restores it."""

import json

from k8s_chaos.common import owned_pods
from k8s_chaos.errors import RwError

ANNOTATION = "runwhen.com/chaos-original-spec"
MANGLED_VALUE = "rw-chaos-none"
FIRST_CHAOS_PORT = 9999
MAX_SERVICES = 5
MODES = ("selector", "port")
# the Service every cluster has: never mangled
PROTECTED_SERVICES = (("default", "kubernetes"),)
SHARED_LIMIT = 50


def check_service_allowed(namespace, name):
    """Refuse the Services that must never be mangled (invalid_input, no override)."""
    if (namespace, name) in PROTECTED_SERVICES:
        raise RwError(
            "invalid_input",
            f"service {namespace}/{name} is the cluster's own API Service; it is never mangled",
            details={"service": name, "namespace": namespace},
        )


def annotation_value(service):
    """The raw annotation text, or None when the Service does not carry it."""
    return ((service.get("metadata") or {}).get("annotations") or {}).get(ANNOTATION)


def annotated_original(service):
    """The original spec ({selector, ports}) from the annotation, or None when the Service
    has no annotation or it is not a spec."""
    raw = annotation_value(service)
    if raw is None:
        return None
    try:
        spec = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("selector"), dict):
        return None
    if not isinstance(spec.get("ports"), list):
        return None
    return {"selector": spec["selector"], "ports": spec["ports"]}


def _selector_of(service):
    """The selector that says which workload the Service belongs to: the recorded original when
    an earlier run left the annotation (the current selector is the mangled one), else its own."""
    original = annotated_original(service)
    if original is not None:
        return original["selector"]
    return service.get("spec", {}).get("selector") or {}


def selects_workload(service, pod_labels):
    """True when the Service has a selector and every pair of it is in the pod template labels.
    A Service an earlier run left mangled counts by the selector it had before."""
    selector = _selector_of(service)
    pod_labels = pod_labels or {}
    return bool(selector) and all(pod_labels.get(k) == v for k, v in selector.items())


def matching_services(services, pod_labels):
    return sorted(
        (s for s in services if selects_workload(s, pod_labels)),
        key=lambda s: s["metadata"]["name"],
    )


def original_spec(service):
    """The part of the Service spec this task can change, as it is now."""
    spec = service.get("spec", {})
    return {
        "selector": dict(spec.get("selector") or {}),
        "ports": json.loads(json.dumps(spec.get("ports") or [])),
    }


def spec_json(original):
    """The original spec as compact JSON, for the annotation."""
    return json.dumps(original, sort_keys=True, separators=(",", ":"))


def chaos_port(ports):
    """The first port number from 9999 down that the Service does not already use."""
    used = {p.get("port") for p in ports}
    port = FIRST_CHAOS_PORT
    while port in used:
        port -= 1
    return port


def refuse_annotated(service):
    """RwError for a Service an earlier run left mangled: the annotated spec is in `details`
    (an object, never cut), the message says how to recover."""
    name = service["metadata"]["name"]
    original = annotated_original(service)
    return RwError(
        "invalid_input",
        f"service {name} is still mangled by an earlier run (annotation {ANNOTATION}); "
        "rerun with recover: true to restore it from the annotation",
        details={
            "service": name,
            "annotation": ANNOTATION,
            "originalSpec": original
            if original is not None
            else {"raw": annotation_value(service)[:1024]},
        },
    )


def mutation_plan(service, mode):
    """What to change on `service`: {mode, field, from, to, patch} (patch = merge-patch body).

    A Service without a selector cannot be mangled by selector (raises
    RwError('invalid_input')); the patch also records the original spec in an
    annotation, so a human can restore it if this task is killed before it does, and
    carries the Service's resourceVersion, so a change made since it was read is a
    conflict and nothing is mangled.
    """
    if mode not in MODES:
        raise RwError("invalid_input", f"mode must be one of {list(MODES)}, got {mode!r}")
    name = service["metadata"]["name"]
    if annotation_value(service) is not None:
        # an earlier run died before it restored: what is on the Service now is not the original
        raise refuse_annotated(service)
    original = original_spec(service)
    meta = {"annotations": {ANNOTATION: spec_json(original)}}
    if service["metadata"].get("resourceVersion"):
        meta["resourceVersion"] = service["metadata"]["resourceVersion"]
    if mode == "selector":
        if not original["selector"]:
            raise RwError(
                "invalid_input", f"service {name} has no selector, so there is nothing to mangle"
            )
        key = sorted(original["selector"])[0]
        selector = dict(original["selector"])
        selector[key] = MANGLED_VALUE
        return {
            "mode": mode,
            "field": f"spec.selector.{key}",
            "from": original["selector"][key],
            "to": MANGLED_VALUE,
            "patch": {"metadata": meta, "spec": {"selector": selector}},
        }
    ports = original["ports"]
    if not ports:
        raise RwError("invalid_input", f"service {name} has no ports to mangle")
    new_port = chaos_port(ports)
    mangled = json.loads(json.dumps(ports))
    mangled[0].setdefault("targetPort", ports[0].get("port"))
    mangled[0]["port"] = new_port
    return {
        "mode": mode,
        "field": "spec.ports[0].port",
        "from": ports[0].get("port"),
        "to": new_port,
        "patch": {"metadata": meta, "spec": {"ports": mangled}},
    }


def restore_plan(original, current, expected=None):
    """How to put back what a mangle changed, touching only fields that still hold a mangled value.

    `expected` is the run's own plan ({mode, to}) when it restores its own change; None when
    recovering from the annotation alone, where the mangled value is inferred (a selector
    value of MANGLED_VALUE, or ports[0].port differing with the rest of that entry equal).
    A field that is neither the mangled value nor the original was changed by someone else:
    it is left alone and listed in `concurrent`, and the annotation is kept for a human.

    Returns {patch, changes, concurrent}: patch is a merge-patch body or None (nothing to
    change), changes a list of {field, from, to}, concurrent a list of field names. The patch
    carries the resourceVersion read, so a change made since is a conflict.
    """
    meta = current.get("metadata") or {}
    spec = current.get("spec") or {}
    now_selector = dict(spec.get("selector") or {})
    now_ports = list(spec.get("ports") or [])
    mode = (expected or {}).get("mode")
    changes, concurrent = [], []

    selector_patch = {}
    for key in sorted(set(original["selector"]) | set(now_selector)):
        was, now = original["selector"].get(key), now_selector.get(key)
        if was == now:
            continue
        ours = now == MANGLED_VALUE and key in original["selector"] and mode in (None, "selector")
        if ours:
            selector_patch[key] = was
            changes.append({"field": f"spec.selector.{key}", "from": now, "to": was})
        else:
            concurrent.append(f"spec.selector.{key}")

    ports = None
    if original["ports"]:
        if not now_ports:
            concurrent.append("spec.ports")
        elif now_ports[0] != original["ports"][0]:
            first_was, first_now = original["ports"][0], now_ports[0]
            same_rest = {k: v for k, v in first_now.items() if k != "port"} == {
                k: v for k, v in first_was.items() if k != "port"
            }
            ours = (
                same_rest
                and first_now.get("port") != first_was.get("port")
                and (mode is None or (mode == "port" and first_now.get("port") == expected["to"]))
            )
            if ours:
                ports = [json.loads(json.dumps(first_was)), *now_ports[1:]]
                changes.append(
                    {
                        "field": "spec.ports[0].port",
                        "from": first_now.get("port"),
                        "to": first_was.get("port"),
                    }
                )
            else:
                concurrent.append("spec.ports[0]")

    patch_meta = {}
    if meta.get("resourceVersion"):
        patch_meta["resourceVersion"] = meta["resourceVersion"]
    if annotation_value(current) is not None and not concurrent:
        patch_meta["annotations"] = {ANNOTATION: None}
        changes.append(
            {"field": f"metadata.annotations[{ANNOTATION}]", "from": "present", "to": "removed"}
        )
    body_spec = {}
    if selector_patch:
        body_spec["selector"] = selector_patch
    if ports is not None:
        body_spec["ports"] = ports
    patch = None
    if body_spec or "annotations" in patch_meta:
        patch = {"metadata": patch_meta}
        if body_spec:
            patch["spec"] = body_spec
    return {"patch": patch, "changes": changes, "concurrent": concurrent}


def is_restored(original, service):
    """True when the Service's selector and its first port equal the original and the annotation
    is gone. Other ports are not this task's to judge."""
    spec = service.get("spec") or {}
    if dict(spec.get("selector") or {}) != original["selector"]:
        return False
    ports = spec.get("ports") or []
    if original["ports"] and ports[:1] != original["ports"][:1]:
        return False
    return annotation_value(service) is None


def ready_endpoints(slices):
    """Endpoints that are ready across a Service's EndpointSlices (ready unset counts as ready)."""
    count = 0
    for sl in slices:
        for ep in sl.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is not False:
                count += 1
    return count


def foreign_pods(kind, workload, replicasets, pods):
    """The pods in `pods` (those a Service selects) that the workload does not own: the
    Services' other users, which a mangle would cut off too. Names, sorted."""
    mine = {p["metadata"]["uid"] for p in owned_pods(kind, workload, replicasets, pods)}
    return sorted(p["metadata"]["name"] for p in pods if p["metadata"]["uid"] not in mine)


def restore_failure(name, namespace, original, why, concurrent=()):
    """One Service that is not back as it was: (message, details entry). The message is short and
    bounded; the original spec is in the details entry as an object."""
    reason = str(why)[:120]
    message = (
        f"service {namespace}/{name} could not be restored ({reason}); "
        f"original spec: details, and annotation {ANNOTATION}"
    )
    entry = {
        "service": name,
        "namespace": namespace,
        "annotation": ANNOTATION,
        "originalSpec": original,
        "concurrentChange": bool(concurrent),
        "changedFields": list(concurrent),
    }
    return message, entry


def restore_error(failures, limit=1024):
    """The `internal` RwError for Services that are still mangled: `failures` is a list of
    (message, details entry). Messages are whole (a message that would pass `limit` is
    replaced by a count); the original specs are in details.services."""
    shown, used = [], 0
    for message, _ in failures:
        if used + len(message) + 3 > limit - 40:
            break
        shown.append(message)
        used += len(message) + 3
    text = " | ".join(shown)
    if len(shown) < len(failures):
        text += f" | and {len(failures) - len(shown)} more (see details)"
    return RwError(
        "internal",
        text,
        scope="restore",
        hint=(
            f"put each Service back by hand from details.services[].originalSpec or its "
            f"{ANNOTATION} annotation, or rerun mangle-service with recover: true"
        ),
        details={"services": [entry for _, entry in failures]},
    )
