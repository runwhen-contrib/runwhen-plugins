"""Kubernetes label selectors, evaluated the way the Prometheus operator evaluates them."""

from k8s_prometheus_health.errors import RwError

OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")


def matches(selector, labels):
    """Whether the label set matches a metav1.LabelSelector given as a dict. An empty selector
    matches everything; the caller handles an unset (nil) selector, whose meaning is the
    caller's (the operator's `serviceMonitorSelector: nil` selects nothing)."""
    labels = labels or {}
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op not in OPERATORS:
            raise RwError(
                "invalid_input", f"label selector operator {op!r} is not one of {OPERATORS}"
            )
        present = key in labels
        if op == "In" and not (present and labels[key] in values):
            return False
        if op == "NotIn" and present and labels[key] in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
    return True
