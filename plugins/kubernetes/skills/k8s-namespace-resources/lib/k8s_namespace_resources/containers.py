"""Containers of pods and pod templates: their requests and limits, and the images they run."""

SETTINGS = (
    ("cpuRequest", "requests", "cpu"),
    ("cpuLimit", "limits", "cpu"),
    ("memoryRequest", "requests", "memory"),
    ("memoryLimit", "limits", "memory"),
)


def containers_of(spec):
    """(init, container) for the pod spec's init containers (native sidecars
    included) and then its containers."""
    for c in spec.get("initContainers") or []:
        yield True, c
    for c in spec.get("containers") or []:
        yield False, c


def resource_settings(container, effective=False):
    """The four settings (cpu/memory request/limit) as the quantity strings the
    API holds, None when not set.

    With `effective`, a request that is unset is the limit, which is what the API
    server does when it creates a pod from a template that sets a limit only.
    """
    resources = container.get("resources") or {}
    out = {key: (resources.get(kind) or {}).get(name) or None for key, kind, name in SETTINGS}
    if effective:
        for name in ("cpu", "memory"):
            out[f"{name}Request"] = out[f"{name}Request"] or out[f"{name}Limit"]
    return out


def effective_requests(container):
    """{cpu, memory}: the request, or the limit when no request is set (see `resource_settings`)."""
    s = resource_settings(container, effective=True)
    return {"cpu": s["cpuRequest"], "memory": s["memoryRequest"]}


def missing_settings(settings):
    return [key for key, _, _ in SETTINGS if settings[key] is None]


def is_running(pod):
    return (pod.get("status") or {}).get("phase") == "Running"
