"""Fetching the target and its pods (the only I/O besides k8s.py and the actions)."""

from k8s_chaos.common import DEPLOYMENT, RESOURCE, owned_pods, selector_string
from k8s_chaos.k8s import kubectl_json, kubectl_json_scoped


def load_target(ctx, target):
    """The workload document for a workload target (a missing one raises not_found), else None.

    A namespace target reads the Namespace object so that a deleted namespace is
    `not_found` and not an empty pod list. An identity that may not read Namespaces
    is not stopped by that: the denial is recorded (scope `namespaces`, making the
    result partial) and the pods are tried; if they are forbidden too, that raises.
    """
    if target["scope"] == "namespace":
        kubectl_json_scoped(ctx, "namespaces", "get", "namespaces", target["name"])
        return None
    return kubectl_json(
        ctx, "-n", target["namespace"], "get", RESOURCE[target["kind"]], target["name"]
    )


def list_pods(ctx, target, workload):
    """Every pod of the target, whatever its state: all pods of the namespace, or the pods
    the workload owns (selector AND ownerReferences)."""
    ns = target["namespace"]
    if workload is None:
        return kubectl_json(ctx, "-n", ns, "get", "pods")["items"]
    selector = selector_string(workload["spec"]["selector"])
    replicasets = []
    if target["kind"] == DEPLOYMENT:
        replicasets = kubectl_json(ctx, "-n", ns, "get", "replicasets.apps", "-l", selector)[
            "items"
        ]
    items = kubectl_json(ctx, "-n", ns, "get", "pods", "-l", selector)["items"]
    return owned_pods(target["kind"], workload, replicasets, items)


def get_pod(ctx, ns, name):
    return kubectl_json(ctx, "-n", ns, "get", "pods", name)


def list_replicasets(ctx, target, workload):
    """The ReplicaSets that can own the workload's pods (a Deployment's); [] for other kinds."""
    if target["kind"] != DEPLOYMENT:
        return []
    selector = selector_string(workload["spec"]["selector"])
    return kubectl_json(ctx, "-n", target["namespace"], "get", "replicasets.apps", "-l", selector)[
        "items"
    ]


def pods_selected_by(ctx, ns, selector):
    """Every pod a Service selector (a plain label map) matches, whatever its state."""
    return kubectl_json(
        ctx, "-n", ns, "get", "pods", "-l", selector_string({"matchLabels": selector})
    )["items"]
