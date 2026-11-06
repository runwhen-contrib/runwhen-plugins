"""injection-status: the namespace's injection labels and, per workload, who has the sidecar."""

from k8s_istio_health.common import MAX_NESTED, controller_uids, matches_selector
from k8s_istio_health.sidecar import (
    INJECT,
    SIDECAR,
    find_sidecar,
    resources_view,
    sidecar_status,
)

KINDS = (
    ("Deployment", "deployments"),
    ("StatefulSet", "statefulsets"),
    ("DaemonSet", "daemonsets"),
)


# Namespaces Istio's injector never touches.
NEVER_INJECTED = ("kube-system", "kube-public", "kube-node-lease", "local-path-storage")
DATAPLANE = "istio.io/dataplane-mode"
REVISION = "istio.io/rev"


def namespace_injection(ns, name=None):
    """What the namespace's labels say. `mode` is label (istio-injection=enabled), revision
    (istio.io/rev with no istio-injection label), disabled (istio-injection set to anything
    else) or unlabelled; enabled/mode are None when the Namespace could not be read.
    `dataplaneMode` is the istio.io/dataplane-mode label (ambient), `excluded` is true for
    namespaces Istio never injects (kube-system and the like, by name)."""
    name = name or ((ns or {}).get("metadata") or {}).get("name")
    excluded = name in NEVER_INJECTED
    if ns is None:
        return {
            "label": None,
            "revision": None,
            "dataplaneMode": None,
            "excluded": excluded,
            "enabled": None,
            "mode": None,
        }
    labels = (ns.get("metadata") or {}).get("labels") or {}
    label, rev = labels.get("istio-injection"), labels.get(REVISION)
    # istio-injection takes precedence over istio.io/rev: the revisioned webhooks require
    # that the istio-injection label does not exist.
    if label == "enabled":
        mode = "label"
    elif label is not None:
        mode = "disabled"
    elif rev:
        mode = "revision"
    else:
        mode = "unlabelled"
    return {
        "label": label,
        "revision": rev,
        "dataplaneMode": labels.get(DATAPLANE),
        "excluded": excluded,
        "enabled": mode in ("label", "revision"),
        "mode": mode,
    }


def template_label(template, key):
    return (((template or {}).get("metadata") or {}).get("labels") or {}).get(key)


def inject_override(template):
    """(value, source) of sidecar.istio.io/inject on the pod template: the label wins over
    the older annotation. (None, None) when neither is set."""
    meta = (template or {}).get("metadata") or {}
    for source, block in (("label", meta.get("labels")), ("annotation", meta.get("annotations"))):
        value = (block or {}).get(INJECT)
        if value is not None:
            return str(value).lower(), source
    return None, None


def injection_expected(injection, override, source, host_network, revision=None, dataplane=None):
    """Would Istio's webhook inject this workload's pods? True/False, or None when the
    namespace could not be read and the template does not decide.

    Never in an excluded namespace, with `inject: "false"` or on the host network. An ambient
    pod (dataplane `ambient`) gets no sidecar unless it asks with `inject: "true"`. The
    namespace labels (istio-injection=enabled or istio.io/rev) enable it; in an unlabelled
    namespace a pod-level opt-in does: the label `sidecar.istio.io/inject=true` or an
    `istio.io/rev` label on the pod template (the annotation form of inject does not)."""
    if host_network or override == "false" or injection["excluded"]:
        return False
    if dataplane == "ambient" and override != "true":
        return False
    mode = injection["mode"]
    if mode in ("label", "revision"):
        return True
    opt_in = (override == "true" and source == "label") or bool(revision)
    if opt_in and mode in (None, "unlabelled"):
        return True
    return None if mode is None else False


def pod_view(pod):
    container, native = find_sidecar(pod)
    status = sidecar_status(pod, native) if container else None
    return {
        "name": pod["metadata"]["name"],
        "hasSidecar": container is not None,
        "native": native,
        "ready": None if status is None else bool(status.get("ready")),
        "container": container,
    }


def pods_of(kind, workload, replicasets, pods):
    """Pods that match the workload's selector and are owned by it (Deployment through its
    ReplicaSets). With `replicasets` None the owner chain is unknown: selector only."""
    selector = (workload.get("spec") or {}).get("selector")
    chosen = [p for p in pods if matches_selector(p["metadata"].get("labels"), selector)]
    if kind == "Deployment":
        if replicasets is None:
            return chosen
        uid = workload["metadata"]["uid"]
        owners = {r["metadata"]["uid"] for r in replicasets if uid in controller_uids(r)}
    else:
        owners = {workload["metadata"]["uid"]}
    return [p for p in chosen if controller_uids(p) & owners]


def workload_view(kind, workload, owned_pods, injection):
    """One workload: its injection settings and, when `owned_pods` (the output of pods_of)
    is known, how many of its pods carry istio-proxy. None leaves the pod facts None."""
    spec = workload.get("spec") or {}
    template = spec.get("template") or {}
    pod_spec = template.get("spec") or {}
    override, source = inject_override(template)
    host_network = bool(pod_spec.get("hostNetwork"))
    revision = template_label(template, REVISION)
    # a pod label of dataplane-mode (ambient, or none to opt out) wins over the namespace's
    dataplane = template_label(template, DATAPLANE) or injection["dataplaneMode"]
    expected = injection_expected(
        injection, override, source, host_network, revision=revision, dataplane=dataplane
    )
    in_template = any(
        c.get("name") == SIDECAR
        for c in (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])
    )
    view = {
        "kind": kind,
        "name": workload["metadata"]["name"],
        "replicas": (
            (workload.get("status") or {}).get("desiredNumberScheduled")
            if kind == "DaemonSet"
            else spec.get("replicas", 1)
        ),
        "injectOverride": override,
        "injectOverrideSource": source,
        "templateRevision": revision,
        "dataplaneMode": dataplane,
        "hostNetwork": host_network,
        "injectionExpected": expected,
        "sidecarInTemplate": in_template,
        "pods": None,
        "podsWithSidecar": None,
        "podsWithoutSidecar": None,
        "podsMissingSidecar": None,
        "sidecarNotReady": None,
        "sidecarResources": None,
        "missingWhileExpected": None,
        "truncated": False,
    }
    if owned_pods is None:
        return view
    owned = [pod_view(p) for p in owned_pods]
    with_sidecar = [p for p in owned if p["hasSidecar"]]
    without = [p["name"] for p in owned if not p["hasSidecar"]]
    distinct = {}
    for p in with_sidecar:
        res = resources_view(p["container"])
        key = (
            res["requests"]["cpu"],
            res["requests"]["memory"],
            res["limits"]["cpu"],
            res["limits"]["memory"],
        )
        distinct.setdefault(key, {"requests": res["requests"], "limits": res["limits"], "pods": 0})
        distinct[key]["pods"] += 1
    resources = sorted(distinct.values(), key=lambda r: (-r["pods"], str(r)))
    view.update(
        {
            "pods": len(owned),
            "podsWithSidecar": len(with_sidecar),
            "podsWithoutSidecar": len(without),
            "podsMissingSidecar": without[:MAX_NESTED],
            "sidecarNotReady": sum(1 for p in with_sidecar if p["ready"] is False),
            "sidecarResources": resources[:MAX_NESTED],
            "missingWhileExpected": None if expected is None else bool(expected and without),
            "truncated": len(without) > MAX_NESTED or len(resources) > MAX_NESTED,
        }
    )
    return view


def sort_workloads(views):
    """Workloads that are missing the sidecar while injection is expected first, then by kind and name."""
    return sorted(
        views, key=lambda v: (v["missingWhileExpected"] is not True, v["kind"], v["name"])
    )


def other_pods(pods, claimed):
    """Counts of pods in the namespace that no listed workload owns (bare pods, Job pods, ...)."""
    rest = [p for p in pods if p["metadata"]["name"] not in claimed]
    return {
        "pods": len(rest),
        "withSidecar": sum(1 for p in rest if find_sidecar(p)[0] is not None),
    }
