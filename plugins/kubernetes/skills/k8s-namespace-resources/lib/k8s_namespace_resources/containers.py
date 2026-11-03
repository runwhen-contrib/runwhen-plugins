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


def resource_settings(container):
    """The four settings (cpu/memory request/limit) as the quantity strings the
    API holds, None when not set."""
    resources = container.get("resources") or {}
    return {key: (resources.get(kind) or {}).get(name) or None for key, kind, name in SETTINGS}


def missing_settings(settings):
    return [key for key, _, _ in SETTINGS if settings[key] is None]


def is_running(pod):
    return (pod.get("status") or {}).get("phase") == "Running"
