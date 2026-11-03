"""ResourceQuota usage against its hard limits."""

from k8s_namespace_health.common import cap, nested, parse_quantity


def _scope_selector(selector):
    """spec.scopeSelector as a bounded [{scopeName, operator, values}], or None."""
    expressions = (selector or {}).get("matchExpressions")
    if not expressions:
        return None
    out = []
    for expr in nested(expressions)[0]:
        values, _ = nested(expr.get("values") or [])
        out.append(
            {"scopeName": expr.get("scopeName"), "operator": expr.get("operator"), "values": values}
        )
    return out


def quota_usage(quotas, ratio_threshold, max_items):
    """One entry per resource of every ResourceQuota: used, hard and used/hard.

    `atOrAbove` is ratio >= `ratio_threshold`. A hard limit of zero has no
    ratio (null) and counts as at the limit, since nothing can be created. When
    the quota controller has not computed `status` yet, `used` and `ratio` are
    null. Rows with a null ratio (hard 0, usage unknown) come first so a
    `max_items` cut never drops an at-limit row; then highest ratio first.
    Quantities are compared exactly (500m, 1Gi, 2).
    """
    threshold = parse_quantity(str(ratio_threshold))
    out = []
    for q in quotas:
        status = q.get("status") or {}
        hard = status.get("hard") or (q.get("spec") or {}).get("hard") or {}
        used = status.get("used")
        spec = q.get("spec") or {}
        scopes, _ = nested(spec.get("scopes") or [])
        scope_selector = _scope_selector(spec.get("scopeSelector"))
        for resource in sorted(hard):
            limit = hard[resource]
            if used is None or resource not in used:
                ratio_value, at = None, None
                used_text = None
            else:
                used_text = used[resource]
                hard_q, used_q = parse_quantity(limit), parse_quantity(used_text)
                if hard_q == 0:
                    ratio_value, at = None, True
                else:
                    ratio = used_q / hard_q
                    ratio_value, at = round(float(ratio), 4), ratio >= threshold
            out.append(
                {
                    "quota": q["metadata"]["name"],
                    "resource": resource,
                    "used": used_text,
                    "hard": limit,
                    "ratio": ratio_value,
                    "atOrAbove": at,
                    "scopes": scopes,
                    "scopeSelector": scope_selector,
                }
            )
    out.sort(
        key=lambda r: (
            r["ratio"] is not None,
            -(r["ratio"] or 0),
            r["quota"],
            r["resource"],
        )
    )
    return cap(out, max_items)
