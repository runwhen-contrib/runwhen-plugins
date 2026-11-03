"""Fetching the target workloads, their pods, events and Vault's own health (the only I/O besides
k8s.py)."""

import re
import subprocess

from k8s_vault_health.errors import RwError
from k8s_vault_health.k8s import (
    MESSAGE_LIMIT,
    failure,
    kubectl_json_scoped,
    run_unchecked,
)
from k8s_vault_health.project import (
    DAEMONSET_FIELDS,
    POD_FIELDS,
    STATEFULSET_FIELDS,
    kubectl_projected,
)
from k8s_vault_health.vault import REMOVED_DOC, health_path, parse_health
from k8s_vault_health.workload import (
    NAMESPACE,
    STATEFULSET,
    owned_pods,
    resolve_target,
    selector_string,
)

PROXY_TIMEOUT = 10  # seconds for one request through the API server's proxy, at most

MTLS_HINT = (
    "the Vault listener requires a client certificate (mutual TLS) and the API server's pod "
    "proxy presents none: this task cannot ask such a listener"
)
NOT_FOUND_HINT = (
    "the pod proxy answered 404: nothing serves Vault's /v1/sys/health on this port (the port "
    "input is Vault's API port, 8200 by default), or the pod is gone"
)
# kubectl's text for a status code the proxy cannot carry; 530 is Vault's "removed from cluster"
_REMOVED = re.compile(r"(?<![\d.:/])530(?![\d.])")


def target(ctx):
    """(Kind, kubectl resource) of the target. Needs no cluster call, so it runs first."""
    return resolve_target(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def target_namespace(ctx, kind):
    """The namespace the tasks look in: the target's own, or for a namespace target its name."""
    return ctx.resource.name if kind == NAMESPACE else ctx.resource.namespace


def load_workloads(ctx, kind, resource, wanted, label):
    """The workloads the task is about, as (namespace, [documents]). Only the fields the tasks read
    come back (name, uid, selector, replicas, update strategy, status): never the pod template, so
    never an env value.

    A StatefulSet or DaemonSet target is that one object (a missing one raises not_found). A
    namespace target is every `wanted` workload in it carrying the Vault Helm chart `label`: the
    Namespace is read first (a deleted namespace is not_found; one that may not be read is
    reported and the lists are tried), and a namespace with none is not_found, never an empty
    result. A target of the other workload kind is invalid_input.
    """
    ns = target_namespace(ctx, kind)
    projection = STATEFULSET_FIELDS if wanted == STATEFULSET else DAEMONSET_FIELDS
    if kind == NAMESPACE:
        kubectl_json_scoped(ctx, "namespaces", "get", "namespaces", ns)
        plural = "statefulsets.apps" if wanted == STATEFULSET else "daemonsets.apps"
        items = kubectl_projected(ctx, projection, "-n", ns, "get", plural, "-l", label, many=True)[
            "items"
        ]
        if not items:
            raise RwError(
                "not_found",
                f"no {wanted} labelled {label} in namespace {ns}; target the workload itself",
            )
        return ns, items
    if kind != wanted:
        raise RwError("invalid_input", f"this task reads a {wanted} or a namespace, not a {kind}")
    return ns, [kubectl_projected(ctx, projection, "-n", ns, "get", resource, ctx.resource.name)]


def load_pods(ctx, ns, workload):
    """The pods the workload owns (selector AND ownerReferences), sorted by name."""
    selector = selector_string(workload["spec"]["selector"])
    items = kubectl_projected(ctx, POD_FIELDS, "-n", ns, "get", "pods", "-l", selector, many=True)[
        "items"
    ]
    return owned_pods(workload, items)


def load_warning_events(ctx, ns):
    """(events, ok): the namespace's Warning events; forbidden is reported and returns ([], False)."""
    doc, state = kubectl_json_scoped(
        ctx, "events", "-n", ns, "get", "events", "--field-selector", "type=Warning"
    )
    return (doc["items"] if doc else []), state == "ok"


def vault_health(ctx, ns, pod_name, scheme, port, budget):
    """The /v1/sys/health document of one pod, through the API server's pod proxy (no port-forward,
    no exec, no credential: the endpoint is unauthenticated). A call made after the task's time
    budget is spent raises timeout without running. A Vault that answered 530 (removed from its
    raft cluster) where the proxy dropped the body is the removed document."""
    path = health_path(ns, pod_name, scheme, port)
    timeout = budget.timeout(PROXY_TIMEOUT)
    argv = ["kubectl", "get", "--raw", path]
    try:
        result = run_unchecked(ctx, argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"Vault did not answer within {timeout:g}s") from None
    if result.returncode:
        stderr = (result.stderr or "").lower()
        if "http request to an https server" in stderr:
            raise RwError("invalid_input", "the pod serves HTTPS: set the scheme input to https")
        if "does not look like a tls handshake" in stderr:
            raise RwError(
                "invalid_input", "the pod serves plain HTTP: set the scheme input to http"
            )
        if "bad certificate" in stderr:
            raise RwError(
                "unreachable",
                "the Vault listener refused the proxy's connection: tls: bad certificate",
                hint=MTLS_HINT,
            )
        if _REMOVED.search(stderr):
            return dict(REMOVED_DOC)
        error = failure(argv, result)
        if error.code == "not_found":
            raise RwError("not_found", error.message, hint=NOT_FOUND_HINT)
        raise error
    return parse_health(result.stdout)


def error_entry(exc):
    """{code, message} of a RwError, for a pod entry."""
    return {"code": exc.code, "message": str(getattr(exc, "message", exc))[:MESSAGE_LIMIT]}
