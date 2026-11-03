"""PodDisruptionBudget coverage of the multi-replica workloads in a namespace."""

from k8s_namespace_health.common import cap, matches_selector, nested

WORKLOADS = (("Deployment", "deployments"), ("StatefulSet", "statefulsets"))


def _selected(pdb, template_labels):
    """A PDB with no selector selects nothing; an empty selector selects every pod."""
    selector = pdb.get("spec", {}).get("selector")
    return selector is not None and matches_selector(template_labels, selector)


def pdb_coverage(deployments, statefulsets, pdbs, max_items):
    """(uncovered, zero_disruption, inspected).

    `uncovered`: Deployments and StatefulSets with more than one replica
    (spec.replicas) that no PDB selects through their pod template labels.
    `zero_disruption`: PDBs whose status.disruptionsAllowed is 0, with the
    healthy counts the disruption controller works from and the workloads each
    one selects. A PDB whose status has no disruptionsAllowed yet is not listed.
    `deployments` or `statefulsets` may be None when they could not be listed:
    `inspected` then reports null for that kind.
    """
    workloads = [("Deployment", d) for d in deployments or []] + [
        ("StatefulSet", s) for s in statefulsets or []
    ]
    uncovered = []
    selects = {p["metadata"]["name"]: [] for p in pdbs}
    for kind, w in workloads:
        template_labels = (
            w.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}
        )
        covering = [p for p in pdbs if _selected(p, template_labels)]
        for p in covering:
            selects[p["metadata"]["name"]].append({"kind": kind, "name": w["metadata"]["name"]})
        replicas = w.get("spec", {}).get("replicas", 1)
        if replicas > 1 and not covering:
            uncovered.append({"kind": kind, "name": w["metadata"]["name"], "replicas": replicas})
    uncovered.sort(key=lambda w: (w["kind"], w["name"]))
    zero = []
    for p in pdbs:
        status, spec = p.get("status", {}), p.get("spec", {})
        if status.get("disruptionsAllowed") != 0:
            continue
        chosen, more = nested(selects[p["metadata"]["name"]])
        zero.append(
            {
                "name": p["metadata"]["name"],
                "selector": spec.get("selector"),
                "minAvailable": spec.get("minAvailable"),
                "maxUnavailable": spec.get("maxUnavailable"),
                "disruptionsAllowed": 0,
                "currentHealthy": status.get("currentHealthy"),
                "desiredHealthy": status.get("desiredHealthy"),
                "expectedPods": status.get("expectedPods"),
                "selects": chosen,
                "truncated": more,
            }
        )
    zero.sort(key=lambda p: p["name"])
    inspected = {
        "deployments": None if deployments is None else len(deployments),
        "statefulsets": None if statefulsets is None else len(statefulsets),
        "pdbs": len(pdbs),
    }
    return cap(uncovered, max_items), cap(zero, max_items), inspected
