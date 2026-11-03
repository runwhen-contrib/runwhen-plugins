"""Pure functions for mangle-service: which Services, what to change, what restores it."""

import json

from k8s_chaos.errors import RwError

ANNOTATION = "runwhen.com/chaos-original-spec"
MANGLED_VALUE = "rw-chaos-none"
FIRST_CHAOS_PORT = 9999
MAX_SERVICES = 5
MODES = ("selector", "port")


def selects_workload(service, pod_labels):
    """True when the Service has a selector and every pair of it is in the pod template labels."""
    selector = service.get("spec", {}).get("selector") or {}
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
    """The original spec as compact JSON, for the annotation and for error messages."""
    return json.dumps(original, sort_keys=True, separators=(",", ":"))


def chaos_port(ports):
    """The first port number from 9999 down that the Service does not already use."""
    used = {p.get("port") for p in ports}
    port = FIRST_CHAOS_PORT
    while port in used:
        port -= 1
    return port


def mutation_plan(service, mode):
    """What to change on `service`: {mode, field, from, to, patch} (patch = merge-patch body).

    A Service without a selector cannot be mangled by selector (raises
    RwError('invalid_input')); the patch also records the original spec in an
    annotation, so a human can restore it if this task is killed before it does.
    """
    if mode not in MODES:
        raise RwError("invalid_input", f"mode must be one of {list(MODES)}, got {mode!r}")
    name = service["metadata"]["name"]
    earlier = (service["metadata"].get("annotations") or {}).get(ANNOTATION)
    if earlier is not None:
        # an earlier run died before it restored: what is on the Service now is not the original
        raise RwError(
            "invalid_input",
            f"service {name} is still mangled by an earlier run (annotation {ANNOTATION}); "
            f"restore it first. Original spec: {earlier}"[:1024],
        )
    original = original_spec(service)
    note = {ANNOTATION: spec_json(original)}
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
            "patch": {"metadata": {"annotations": note}, "spec": {"selector": selector}},
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
        "patch": {"metadata": {"annotations": note}, "spec": {"ports": mangled}},
    }


def restore_patch(original, current):
    """Merge-patch body that puts the original selector and ports back and removes the
    annotation. Selector keys added since are deleted (null), the rest replaced."""
    selector = dict(original["selector"])
    for key in current.get("spec", {}).get("selector") or {}:
        selector.setdefault(key, None)
    spec = {"ports": original["ports"]}
    if original["selector"] or current.get("spec", {}).get("selector"):
        spec["selector"] = selector
    return {"metadata": {"annotations": {ANNOTATION: None}}, "spec": spec}


def is_restored(original, service):
    """True when the Service's selector and ports equal the original and the annotation is gone."""
    annotations = (service.get("metadata") or {}).get("annotations") or {}
    return original_spec(service) == original and ANNOTATION not in annotations


def ready_endpoints(slices):
    """Endpoints that are ready across a Service's EndpointSlices (ready unset counts as ready)."""
    count = 0
    for sl in slices:
        for ep in sl.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is not False:
                count += 1
    return count


def restore_failure_message(name, namespace, original, why):
    """The message of the `internal` error for a Service that is still mangled:
    carries the original spec so a human can put it back."""
    return (
        f"service {namespace}/{name} could not be restored ({why}). Original spec to restore "
        f"(selector and ports, also in annotation {ANNOTATION}): {spec_json(original)}"
    )
