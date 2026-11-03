"""Images the containers of a namespace run, and pods that cannot pull theirs."""

from k8s_namespace_resources.common import trunc
from k8s_namespace_resources.containers import containers_of

PULL_REASONS = ("ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull", "InvalidImageName")


def parse_image(ref):
    """(repository, tag, digest) of an image reference.

    `registry:5000/team/app:1.2@sha256:...` -> ('registry:5000/team/app', '1.2', 'sha256:...').
    A colon only starts a tag in the last path segment, so a registry port is not
    read as one. tag and digest are None when absent; an untagged reference has
    no tag here even though the runtime pulls `latest`.
    """
    name, _, digest = ref.partition("@")
    digest = digest or None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    else:
        tag = None
    return name, tag, digest


def _statuses(pod):
    status = pod.get("status") or {}
    return {
        **{s["name"]: s for s in status.get("initContainerStatuses") or []},
        **{s["name"]: s for s in status.get("containerStatuses") or []},
    }


def image_entries(pods):
    """(entries, pull_failures) for every container (init included) of the pods."""
    entries, failures = [], []
    for pod in pods:
        meta, spec = pod["metadata"], pod.get("spec") or {}
        statuses = _statuses(pod)
        for init, c in containers_of(spec):
            repository, tag, digest = parse_image(c["image"])
            status = statuses.get(c["name"]) or {}
            entries.append(
                {
                    "pod": meta["name"],
                    "phase": (pod.get("status") or {}).get("phase"),
                    "container": c["name"],
                    "init": init,
                    "image": c["image"],
                    "repository": repository,
                    "tag": tag,
                    "digest": digest,
                    "pullPolicy": c.get("imagePullPolicy"),
                    "imageID": status.get("imageID") or None,
                }
            )
            waiting = (status.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in PULL_REASONS:
                message, cut = trunc(waiting.get("message"))
                failures.append(
                    {
                        "pod": meta["name"],
                        "container": c["name"],
                        "init": init,
                        "image": c["image"],
                        "reason": waiting["reason"],
                        "message": message,
                        "truncated": cut,
                    }
                )
    key = lambda e: (e["pod"], e["init"] is False, e["container"])  # noqa: E731
    entries.sort(key=key)
    failures.sort(key=key)
    return entries, failures
