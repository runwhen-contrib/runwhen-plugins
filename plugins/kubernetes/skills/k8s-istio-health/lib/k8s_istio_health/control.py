"""control-plane-status: replicas, restarts, version and webhook configurations of an istiod or gateway Deployment."""

import re

from k8s_istio_health.common import MAX_NESTED, redact, trunc
from k8s_istio_health.sidecar import SIDECAR

# containers whose image tag is "the version": istiod's, then a gateway's proxy
VERSION_CONTAINERS = ("discovery", SIDECAR)
# Webhook configurations Istio creates (names carry the revision or namespace after the prefix).
MUTATING_PREFIXES = ("istio-sidecar-injector", "istio-revision-tag-")
VALIDATING_PREFIXES = ("istio-validator", "istiod-default-validator")

_REGISTRY_PORT = re.compile(r"^[^/]*:\d+$")


def parse_image(image):
    """(tag, has_digest) of an image reference: 'docker.io/istio/pilot:1.20.3' -> ('1.20.3', False);
    no tag -> (None, ...). A registry port ('localhost:5000/x') is not a tag."""
    ref, _, digest = (image or "").partition("@")
    last = ref.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else None
    return tag, bool(digest)


def deployment_view(dep):
    spec, status = dep.get("spec") or {}, dep.get("status") or {}
    conditions = []
    for c in (status.get("conditions") or [])[:MAX_NESTED]:
        message, cut = trunc(c.get("message"))
        conditions.append(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": message,
                "truncated": cut,
                "lastTransitionTime": c.get("lastTransitionTime"),
            }
        )
    return {
        "name": dep["metadata"]["name"],
        "replicas": spec.get("replicas", 1),
        "readyReplicas": status.get("readyReplicas", 0),
        "availableReplicas": status.get("availableReplicas", 0),
        "updatedReplicas": status.get("updatedReplicas", 0),
        "unavailableReplicas": status.get("unavailableReplicas", 0),
        "generation": dep["metadata"].get("generation"),
        "observedGeneration": status.get("observedGeneration"),
        "paused": bool(spec.get("paused")),
        "conditions": conditions,
    }


def _containers(pod):
    spec = pod.get("spec") or {}
    st = pod.get("status") or {}
    regular = [(c, False) for c in spec.get("containers") or []]
    native = [
        (c, True) for c in spec.get("initContainers") or [] if c.get("restartPolicy") == "Always"
    ]
    statuses = {(s.get("name"), False): s for s in st.get("containerStatuses") or []} | {
        (s.get("name"), True): s for s in st.get("initContainerStatuses") or []
    }
    return [(c, init, statuses.get((c.get("name"), init))) for c, init in regular + native]


def pod_view(pod):
    """One pod: phase, readiness, restarts per container (init containers marked `init`)."""
    st = pod.get("status") or {}
    spec = pod.get("spec") or {}
    containers = []
    for c, init, cs in _containers(pod):
        cs = cs or {}
        state = cs.get("state") or {}
        kind = next(iter(state), None)
        detail = (state.get(kind) or {}) if kind else {}
        last = (cs.get("lastState") or {}).get("terminated")
        reason, _ = trunc(detail.get("reason"))
        containers.append(
            {
                "name": c["name"],
                "init": init,
                "ready": bool(cs.get("ready")),
                "restartCount": cs.get("restartCount", 0),
                "state": kind,
                "reason": reason,
                "lastTerminated": (
                    None
                    if not last
                    else {
                        "reason": last.get("reason"),
                        "exitCode": last.get("exitCode"),
                        "finishedAt": last.get("finishedAt"),
                    }
                ),
            }
        )
    return {
        "name": pod["metadata"]["name"],
        "phase": st.get("phase"),
        "ready": any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in st.get("conditions") or []
        ),
        "node": spec.get("nodeName"),
        "terminating": bool(pod["metadata"].get("deletionTimestamp")),
        "restartCount": sum(c["restartCount"] for c in containers),
        "containers": containers[:MAX_NESTED],
        "truncated": len(containers) > MAX_NESTED,
    }


def sort_pods(views):
    """Most restarts first, then not ready, then name."""
    return sorted(views, key=lambda p: (-p["restartCount"], p["ready"], p["name"]))


def versions(dep, pods):
    """The Istio version(s) from image tags. From the running pods when they are known
    (gateways use the placeholder image `auto` in the Deployment; the injected pod has the
    real one); else from the Deployment's template."""
    if pods is not None:
        pairs = [
            (c["image"], c["name"]) for p in pods for c, _i, _s in _containers(p) if c.get("image")
        ]
        source = "pods"
    else:
        template = ((dep.get("spec") or {}).get("template") or {}).get("spec") or {}
        pairs = [
            (c["image"], c["name"]) for c in template.get("containers") or [] if c.get("image")
        ]
        source = "template"
    for name in VERSION_CONTAINERS:
        counts = {}
        for image, cname in pairs:
            if cname == name:
                tag, _digest = parse_image(image)
                counts[tag] = counts.get(tag, 0) + 1
        if counts:
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
            return {
                "container": name,
                "source": source,
                "tags": [{"tag": t, "count": n} for t, n in ordered[:MAX_NESTED]],
            }
    return {"container": None, "source": source, "tags": []}


def matching_configurations(items, prefixes):
    return sorted(
        (i for i in items if i["metadata"]["name"].startswith(prefixes)),
        key=lambda i: i["metadata"]["name"],
    )


def service_ref(webhook):
    cc = webhook.get("clientConfig") or {}
    svc = cc.get("service")
    return (
        None
        if not svc
        else {
            "namespace": svc.get("namespace"),
            "name": svc.get("name"),
            "port": svc.get("port", 443),
            "path": svc.get("path"),
        }
    )


def ready_endpoints(slices):
    """(ready, not ready) endpoint counts over the EndpointSlices of one Service."""
    ready = not_ready = 0
    for s in slices:
        for ep in s.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is True:
                ready += 1
            else:
                not_ready += 1
    return ready, not_ready


def webhook_view(config, kind, webhook, target_ns, target_name, endpoints):
    """One webhook of a configuration. `endpoints` is the readiness of its Service's
    EndpointSlices when they were read ({'ready': n, 'notReady': m}), else None."""
    svc = service_ref(webhook)
    url, _ = trunc(redact(webhook.get("clientConfig", {}).get("url") or "") or None)
    return {
        "configuration": config["metadata"]["name"],
        "kind": kind,
        "webhook": webhook.get("name"),
        "failurePolicy": webhook.get("failurePolicy"),
        "sideEffects": webhook.get("sideEffects"),
        "timeoutSeconds": webhook.get("timeoutSeconds"),
        "caBundlePresent": bool((webhook.get("clientConfig") or {}).get("caBundle")),
        "service": svc,
        "url": url,
        "pointsAtTarget": bool(
            svc and svc["namespace"] == target_ns and svc["name"] == target_name
        ),
        "endpoints": endpoints,
    }


def webhook_views(configs, kind, target_ns, target_name, endpoints_of):
    """Every webhook of the matched configurations. `endpoints_of(namespace, name)` returns
    {'ready','notReady'} or None (not looked up: another namespace, or not allowed)."""
    out = []
    for config in configs:
        for webhook in config.get("webhooks") or []:
            svc = service_ref(webhook)
            eps = endpoints_of(svc["namespace"], svc["name"]) if svc else None
            out.append(webhook_view(config, kind, webhook, target_ns, target_name, eps))
    return out
