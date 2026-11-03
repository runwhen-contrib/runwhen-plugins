"""servicemonitor-coverage: which ServiceMonitors the Prometheus selects, which of them produced
targets, and whether its ServiceAccount can discover in the namespaces they point at."""

import json

from k8s_prometheus_health.common import NESTED_LIMIT, cap
from k8s_prometheus_health.errors import RwError
from k8s_prometheus_health.selectors import matches

SERVICEMONITORS = "servicemonitors.monitoring.coreos.com"
MAX_RBAC_NAMESPACES = 16
VERBS = ("list", "watch")


def selector_scope(prom):
    """How the CR picks namespaces: 'own' (serviceMonitorNamespaceSelector unset: only the
    Prometheus's namespace), 'all' ({}: every namespace) or 'selector' (match labels)."""
    selector = (prom.get("spec") or {}).get("serviceMonitorNamespaceSelector")
    if selector is None:
        return "own"
    if not selector.get("matchLabels") and not selector.get("matchExpressions"):
        return "all"
    return "selector"


def namespace_selected(prom, namespace, ns_labels):
    """True/False whether `namespace` is one the Prometheus watches for ServiceMonitors; None when
    that depends on namespace labels that could not be read."""
    scope = selector_scope(prom)
    if scope == "own":
        return namespace == prom["metadata"]["namespace"]
    if scope == "all":
        return True
    if ns_labels is None or namespace not in ns_labels:
        return None
    return matches(prom["spec"]["serviceMonitorNamespaceSelector"], ns_labels[namespace])


def selection(prom, sm, ns_labels):
    """(selected, reason) for one ServiceMonitor: selected is True/False, or None when the
    namespace selector could not be evaluated; reason says why not (None when selected)."""
    selector = (prom.get("spec") or {}).get("serviceMonitorSelector")
    if selector is None:
        return (
            False,
            "the Prometheus has no serviceMonitorSelector (an unset selector selects nothing)",
        )
    if not matches(selector, (sm["metadata"].get("labels") or {})):
        return False, "its labels do not match serviceMonitorSelector"
    in_namespace = namespace_selected(prom, sm["metadata"]["namespace"], ns_labels)
    if in_namespace is None:
        return (
            None,
            "namespace labels could not be read to evaluate serviceMonitorNamespaceSelector",
        )
    if not in_namespace:
        return False, "its namespace is not selected by serviceMonitorNamespaceSelector"
    return True, None


def pool_prefix(sm):
    return f"serviceMonitor/{sm['metadata']['namespace']}/{sm['metadata']['name']}/"


def view(prom, sms, ns_labels, targets, limit, complete):
    """The coverage output. `targets` is the merged target table of targets.merge."""
    rows = []
    for sm in sorted(sms, key=lambda s: (s["metadata"]["namespace"], s["metadata"]["name"])):
        selected, reason = selection(prom, sm, ns_labels)
        prefix = pool_prefix(sm)
        mine = [e for e in targets.values() if (e["scrapePool"] or "").startswith(prefix)]
        pools = {e["scrapePool"] for e in mine}
        rows.append(
            {
                "namespace": sm["metadata"]["namespace"],
                "name": sm["metadata"]["name"],
                "selected": selected,
                "notSelectedReason": reason,
                "endpointCount": len((sm.get("spec") or {}).get("endpoints") or []),
                "activeTargets": len(mine) if selected else None,
                "targetsDown": sum(1 for e in mine if e["health"] == "down") if selected else None,
                "endpointsWithTargets": len(pools) if selected else None,
            }
        )
    selected_rows = [r for r in rows if r["selected"]]
    return {
        "prometheus": {
            "name": prom["metadata"]["name"],
            "namespace": prom["metadata"]["namespace"],
            "serviceMonitorSelector": (prom.get("spec") or {}).get("serviceMonitorSelector"),
            "serviceMonitorNamespaceSelector": (prom.get("spec") or {}).get(
                "serviceMonitorNamespaceSelector"
            ),
            "namespaceScope": selector_scope(prom),
        },
        "complete": complete,
        "serviceMonitorCount": len(rows),
        "selectedCount": len(selected_rows),
        "notSelectedCount": sum(1 for r in rows if r["selected"] is False),
        "undeterminedCount": sum(1 for r in rows if r["selected"] is None),
        "selectedWithoutTargets": sum(1 for r in selected_rows if not r["activeTargets"]),
        "serviceMonitors": cap(rows, limit),
        "truncated": len(rows) > limit,
    }


def discovery_resources(prom):
    """(group, resource) pairs the Prometheus needs to list and watch to discover targets with its
    serviceDiscoveryRole (Endpoints by default, or EndpointSlice)."""
    role = (prom.get("spec") or {}).get("serviceDiscoveryRole") or "Endpoints"
    return role, [
        ("", "services"),
        ("", "pods"),
        ("discovery.k8s.io", "endpointslices") if role == "EndpointSlice" else ("", "endpoints"),
    ]


def target_namespaces(prom, sms, selections):
    """(sorted namespaces, notes): the namespaces the selected ServiceMonitors point their
    Services at (their own, `namespaceSelector.matchNames`), capped; `any: true` cannot be
    enumerated and is noted."""
    spaces, notes = set(), []
    for sm in sms:
        if not selections[(sm["metadata"]["namespace"], sm["metadata"]["name"])]:
            continue
        chosen = (sm.get("spec") or {}).get("namespaceSelector") or {}
        if chosen.get("any"):
            notes.append(
                f"{sm['metadata']['namespace']}/{sm['metadata']['name']} has namespaceSelector.any: "
                "every namespace, not enumerated"
            )
        elif chosen.get("matchNames"):
            spaces.update(chosen["matchNames"])
        else:
            spaces.add(sm["metadata"]["namespace"])
    ordered = sorted(spaces)
    if len(ordered) > MAX_RBAC_NAMESPACES:
        notes.append(f"{len(ordered) - MAX_RBAC_NAMESPACES} more namespaces not checked")
    return ordered[:MAX_RBAC_NAMESPACES], notes


def service_account(prom):
    """(namespace, name) of the ServiceAccount the Prometheus pods run as."""
    name = (prom.get("spec") or {}).get("serviceAccountName") or "default"
    return prom["metadata"]["namespace"], name


def review_list(prom, namespaces):
    """(document, order): one SubjectAccessReview per namespace, resource and verb, as a v1 List
    for one `kubectl create -f`, and the (namespace, resource, group, verb) of each item."""
    sa_ns, sa_name = service_account(prom)
    _, resources = discovery_resources(prom)
    items, order = [], []
    for namespace in namespaces:
        for group, resource in resources:
            for verb in VERBS:
                order.append((namespace, resource, group, verb))
                items.append(
                    {
                        "apiVersion": "authorization.k8s.io/v1",
                        "kind": "SubjectAccessReview",
                        "spec": {
                            "user": f"system:serviceaccount:{sa_ns}:{sa_name}",
                            "groups": [
                                "system:serviceaccounts",
                                f"system:serviceaccounts:{sa_ns}",
                                "system:authenticated",
                            ],
                            "resourceAttributes": {
                                "namespace": namespace,
                                "verb": verb,
                                "group": group,
                                "resource": resource,
                            },
                        },
                    }
                )
    return {"apiVersion": "v1", "kind": "List", "items": items}, order


def parse_reviews(text, expected):
    """The `status.allowed` of each review in `kubectl create -f <List> -o json`, which prints one
    JSON document per item, not a List. A count other than `expected`, or an item with no boolean
    `allowed`, is an error: the check did not run."""
    decoder, pos, allowed = json.JSONDecoder(), 0, []
    text = text or ""
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            doc, pos = decoder.raw_decode(text, pos)
        except ValueError as exc:
            raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None
        flag = ((doc or {}).get("status") or {}).get("allowed") if isinstance(doc, dict) else None
        if not isinstance(flag, bool):
            raise RwError("internal", "a SubjectAccessReview came back without status.allowed")
        allowed.append(flag)
    if len(allowed) != expected:
        raise RwError("internal", f"expected {expected} SubjectAccessReviews, got {len(allowed)}")
    return allowed


def access_view(prom, namespaces, notes, order, allowed):
    role, _ = discovery_resources(prom)
    sa_ns, sa_name = service_account(prom)
    table = {}
    for (namespace, resource, group, verb), flag in zip(order, allowed, strict=True):
        row = table.setdefault(
            (namespace, resource),
            {"namespace": namespace, "resource": resource, "group": group or "core"},
        )
        row[verb] = flag
    checks = cap(list(table.values()), NESTED_LIMIT)
    return {
        "serviceAccount": f"{sa_ns}/{sa_name}",
        "serviceDiscoveryRole": role,
        "namespaces": namespaces,
        "checks": checks,
        "deniedCount": sum(1 for flag in allowed if not flag),
        "notes": notes,
    }
