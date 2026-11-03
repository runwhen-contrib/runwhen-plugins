"""Facts about the istio-proxy sidecar of a pod (shared by injection-status and the other tasks)."""

SIDECAR = "istio-proxy"
INJECT = "sidecar.istio.io/inject"


def find_sidecar(pod):
    """(container spec, native) for the istio-proxy container of a pod, or (None, False).

    A native sidecar is an init container (restartPolicy Always); it counts the same as a
    regular container."""
    spec = pod.get("spec") or {}
    for c in spec.get("containers") or []:
        if c.get("name") == SIDECAR:
            return c, False
    for c in spec.get("initContainers") or []:
        if c.get("name") == SIDECAR:
            return c, True
    return None, False


def sidecar_status(pod, native):
    """The containerStatus of istio-proxy, or None while the kubelet has not reported one."""
    key = "initContainerStatuses" if native else "containerStatuses"
    for st in (pod.get("status") or {}).get(key) or []:
        if st.get("name") == SIDECAR:
            return st
    return None


def waiting_reason(status):
    """'ContainerCreating' etc. when the container is waiting, else None."""
    waiting = ((status or {}).get("state") or {}).get("waiting")
    return (waiting.get("reason") or "waiting") if waiting else None


def resources_view(container):
    """requests and limits for cpu and memory exactly as written (None where not set)."""
    res = container.get("resources") or {}
    out = {}
    for kind in ("requests", "limits"):
        block = res.get(kind) or {}
        out[kind] = {"cpu": block.get("cpu"), "memory": block.get("memory")}
    return out
