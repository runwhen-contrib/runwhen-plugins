"""Pure functions on Vault's /v1/sys/health document and on the StatefulSet around it."""

import json
from urllib.parse import quote

from k8s_vault_health.errors import RwError
from k8s_vault_health.workload import limit_text, pod_ready, restart_total

SCHEMES = ("http", "https")

# Vault answers /v1/sys/health with a status code that says the state (429 standby, 472 DR
# secondary, 473 performance standby, 501 uninitialized, 503 sealed, 530 removed from the raft
# cluster) and the same JSON body for all of them. The API server's proxy turns any non-2xx answer
# into an error and drops the body, so every code is overridden to 200: the body then always
# arrives and the state is read from it. A Vault too old to know `removedcode` ignores it.
QUERY = (
    "activecode=200&standbycode=200&performancestandbycode=200"
    "&drsecondarycode=200&sealedcode=200&uninitcode=200&removedcode=200"
)


def health_path(namespace, pod, scheme, port):
    """The API server path of a pod's /v1/sys/health through the pod proxy subresource."""
    if scheme not in SCHEMES:
        raise RwError("invalid_input", f"scheme must be http or https, not {scheme!r}")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RwError("invalid_input", f"port must be 1 to 65535, not {port!r}")
    return (
        f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{scheme}:{quote(pod, safe='')}:{port}"
        f"/proxy/v1/sys/health?{QUERY}"
    )


# what stands in for the body of a Vault that answered 530 where the proxy dropped it
REMOVED_DOC = {"removed_from_cluster": True}


def parse_health(text):
    """The health document from the proxy's body; anything else is an internal error."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise RwError("internal", "the pod's /v1/sys/health did not return JSON") from None
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("initialized"), bool)
        or not isinstance(doc.get("sealed"), bool)
    ):
        raise RwError("internal", "the pod's /v1/sys/health is not a Vault health document")
    return doc


def _bool(doc, key):
    value = doc.get(key)
    return value if isinstance(value, bool) else None


def _str(doc, key):
    """A string the server reports, bounded (it is server-supplied free text)."""
    value = doc.get(key)
    return limit_text(value)[0] if isinstance(value, str) else None


def health_state(doc):
    """(state, code): what the health document means and the HTTP status Vault returns for it with
    its default settings (uninitialized, sealed, removed from the cluster, DR secondary,
    performance standby, standby, active). Not an error: a standby or sealed Vault answers."""
    if doc.get("removed_from_cluster") is True:
        return "removed", 530
    if not doc["initialized"]:
        return "uninitialized", 501
    if doc["sealed"]:
        return "sealed", 503
    if doc.get("replication_dr_mode") == "secondary":
        return "dr_secondary", 472
    if doc.get("performance_standby") is True:
        return "performance_standby", 473
    if doc.get("standby") is True:
        return "standby", 429
    return "active", 200


def health_facts(doc):
    """The facts of one Vault server from its health document. A field the server leaves out (the
    cluster name of a sealed Vault) is null."""
    state, code = health_state(doc)
    return {
        "state": state,
        "healthCode": code,
        "initialized": _bool(doc, "initialized"),
        "sealed": _bool(doc, "sealed"),
        "standby": _bool(doc, "standby"),
        "performanceStandby": _bool(doc, "performance_standby"),
        "version": _str(doc, "version"),
        "clusterName": _str(doc, "cluster_name"),
        "clusterId": _str(doc, "cluster_id"),
        "replicationPerformanceMode": _str(doc, "replication_performance_mode"),
        "replicationDrMode": _str(doc, "replication_dr_mode"),
        "enterprise": _bool(doc, "enterprise"),
    }


def statefulset_view(sts):
    """Replica counts of a StatefulSet. The API omits a count that is zero, so a missing
    readyReplicas is 0."""
    meta, spec, status = sts["metadata"], sts.get("spec", {}), sts.get("status", {})
    generation = meta.get("generation")
    observed = status.get("observedGeneration")
    return {
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "desired": spec.get("replicas", 1),
        "current": status.get("currentReplicas", 0),
        "ready": status.get("readyReplicas", 0),
        "updated": status.get("updatedReplicas", 0),
        "available": status.get("availableReplicas", 0),
        "generation": generation,
        "observedGeneration": observed,
        "generationObserved": None if generation is None else observed == generation,
        "updateStrategy": (spec.get("updateStrategy") or {}).get("type"),
    }


def pod_entry(statefulset, pod):
    """The entry of one pod before Vault is asked: queried false, health null."""
    status = pod.get("status", {})
    return {
        "statefulset": statefulset,
        "pod": pod["metadata"]["name"],
        "phase": status.get("phase"),
        "ready": pod_ready(pod),
        "containerRestarts": restart_total(pod),
        "queried": False,
        "health": None,
        "error": None,
    }


def askable(pod):
    """A pod Vault can be asked through the proxy: Running with an address. A Vault that is sealed
    or uninitialized is Running and not ready, so readiness is not required."""
    status = pod.get("status", {})
    return (
        status.get("phase") == "Running"
        and bool(status.get("podIP"))
        and not pod["metadata"].get("deletionTimestamp")
    )


def sort_entries(entries):
    """Sealed, uninitialized or removed first, then pods that are not ready, then by name."""

    def key(e):
        state = (e["health"] or {}).get("state")
        return (state not in ("sealed", "uninitialized", "removed"), e["ready"], e["pod"])

    return sorted(entries, key=key)


def counts(entries):
    states = {}
    for e in entries:
        if e["health"]:
            states[e["health"]["state"]] = states.get(e["health"]["state"], 0) + 1
    return {
        "pods": len(entries),
        "ready": sum(1 for e in entries if e["ready"]),
        "queried": sum(1 for e in entries if e["queried"]),
        "failed": sum(1 for e in entries if e["error"] is not None),
        "byState": dict(sorted(states.items())),
    }
