from k8s_base.errors import RwError
from k8s_base.permissions import check


def _bounded(settings, name, low, high):
    value = getattr(settings, name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RwError("invalid_input", f"setting {name} must be an integer from {low} to {high}")
    return value


def main(ctx, rules, namespaces=()):
    max_reviews = _bounded(ctx.settings, "max_reviews", 1, 20000)
    max_namespaces = _bounded(ctx.settings, "max_namespaces", 1, 5000)
    return {
        "access": check(ctx, list(rules), list(namespaces), max_reviews, max_namespaces),
    }
