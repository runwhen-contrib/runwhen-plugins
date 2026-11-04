"""Fetching the target workloads, their pods, events and Vault's own health (the only I/O besides
k8s.py)."""

from k8s_vault_health.errors import RwError
from k8s_vault_health.k8s import MESSAGE_LIMIT, kubectl_json_scoped
from k8s_vault_health.project import (
    DAEMONSET_FIELDS,
    POD_FIELDS,
    STATEFULSET_FIELDS,
    kubectl_projected,
)
from k8s_vault_health.vault import (
    HEALTH_PATH,
    STATE_BY_STATUS,
    check_listener,
    parse_health,
)
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
    """(status, document) of one pod's /v1/sys/health, through the API server's pod proxy by
    `ctx.k8s_proxy_get` (no port-forward, no exec, no credential: the endpoint is unauthenticated;
    a redirect is never followed). The status is Vault's own: 200 active, 429 standby, 472, 473,
    501 uninitialized, 503 sealed, 530 removed from its raft cluster. A call made after the task's
    time budget is spent raises timeout without running."""
    check_listener(scheme, port)
    timeout = budget.timeout(PROXY_TIMEOUT)
    try:
        reply = ctx.k8s_proxy_get(
            ns, "pods", pod_name, port, HEALTH_PATH, scheme=scheme, timeout=timeout
        )
    except RwError as exc:
        text = str(getattr(exc, "message", exc)).lower()
        if exc.code == "timeout":
            raise RwError("timeout", f"Vault did not answer within {timeout:g}s") from None
        if exc.code == "unreachable" and "does not look like a tls handshake" in text:
            raise RwError(
                "invalid_input", "the pod serves plain HTTP: set the scheme input to http"
            ) from None
        if exc.code == "unreachable" and "bad certificate" in text:
            raise RwError(
                "unreachable",
                "the Vault listener refused the proxy's connection: tls: bad certificate",
                hint=MTLS_HINT,
            ) from None
        if exc.code == "not_found":
            raise RwError("not_found", exc.message, hint=NOT_FOUND_HINT) from None
        raise
    status = reply["status"]
    if reply.get("truncated"):
        raise RwError(
            "internal",
            "the pod's /v1/sys/health body was cut at the size limit",
            hint="something other than Vault answers on this port: check the port input",
        )
    if status in STATE_BY_STATUS:
        return status, parse_health(reply["body"], status)
    headers = reply.get("headers") or {}
    if 300 <= status < 400:
        dropped = headers.get("x-rw-redirect-dropped")
        where = f"off cluster to {dropped}" if dropped else "elsewhere"
        raise RwError(
            "internal",
            f"the pod's /v1/sys/health answered a redirect ({status}) {where}; not followed",
            hint="something other than Vault answers on this port: check the port input",
        )
    if status == 404:
        raise RwError("not_found", "the pod's /v1/sys/health answered 404", hint=NOT_FOUND_HINT)
    if status == 400 and "http request to an https server" in reply["body"].lower():
        raise RwError("invalid_input", "the pod serves HTTPS: set the scheme input to https")
    raise RwError("internal", f"the pod's /v1/sys/health answered HTTP {status}")


def error_entry(exc):
    """{code, message} of a RwError, for a pod entry."""
    return {"code": exc.code, "message": str(getattr(exc, "message", exc))[:MESSAGE_LIMIT]}
