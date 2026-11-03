"""What an identity may do, as RBAC rules: the pure half of check-permissions.

A rule is expanded into single checks (group, resource[/subresource], verb, resourceName) or
(nonResourceURL, verb); each check is asked of the API server as a SelfSubjectAccessReview, or
answered from a SelfSubjectRulesReview evaluated here with the authorizer's own matching.
Nothing in this module calls kubectl."""

import re
from collections.abc import Mapping

MAX_RULES = 500
MAX_EXPANSION = 200  # checks one rule may expand to; a bigger one is unverified, not sampled
EVIDENCE_LIMIT = 400
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

RESOURCE, NONRESOURCE = "r", "n"
MAX_SCOPE = 500  # namespaces one `scope` lists; the evidence says when it was cut
CLUSTER, NAMESPACED = "cluster", "namespaced"

# Cluster-scoped kinds as (group, resource), used with (and when) `kubectl api-resources` cannot say.
STATIC_CLUSTER_SCOPED = frozenset(
    {
        ("", "namespaces"),
        ("", "nodes"),
        ("", "persistentvolumes"),
        ("", "componentstatuses"),
        ("storage.k8s.io", "storageclasses"),
        ("storage.k8s.io", "csidrivers"),
        ("storage.k8s.io", "csinodes"),
        ("storage.k8s.io", "volumeattachments"),
        ("rbac.authorization.k8s.io", "clusterroles"),
        ("rbac.authorization.k8s.io", "clusterrolebindings"),
        ("apiextensions.k8s.io", "customresourcedefinitions"),
        ("apiregistration.k8s.io", "apiservices"),
        ("scheduling.k8s.io", "priorityclasses"),
        ("networking.k8s.io", "ingressclasses"),
        ("node.k8s.io", "runtimeclasses"),
        ("certificates.k8s.io", "certificatesigningrequests"),
        ("admissionregistration.k8s.io", "validatingwebhookconfigurations"),
        ("admissionregistration.k8s.io", "mutatingwebhookconfigurations"),
        ("metrics.k8s.io", "nodes"),
    }
)


def plain(value):
    """JSON-plain copy of a value that may be an SDK AttrView (read-only Mapping)."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _strings(value, empty_ok=False):
    """A non-empty list of strings; "" is a name only where `empty_ok` (the core API group)."""
    return (
        isinstance(value, list)
        and len(value) >= 1
        and all(isinstance(v, str) and (v or empty_ok) for v in value)
    )


def expand_rule(rule):
    """(checks, None) for a rule this task can evaluate, else (None, why).

    A check is ("r", group, resource, subresource, verb, name) or ("n", path, verb). A rule
    is granted only if every expansion is: the cross product of apiGroups, resources, verbs
    and resourceNames (a rule with no resourceNames asks without a name)."""
    if not isinstance(rule, dict):
        return None, "the rule is not an object"
    verbs = rule.get("verbs")
    if not _strings(verbs):
        return None, "verbs is missing or not a list of names"
    if "nonResourceURLs" in rule:
        if set(rule) - {"nonResourceURLs", "verbs"} or not _strings(rule["nonResourceURLs"]):
            return None, "a non-resource rule is only nonResourceURLs and verbs"
        checks = [(NONRESOURCE, url, verb) for url in rule["nonResourceURLs"] for verb in verbs]
    else:
        if set(rule) - {"apiGroups", "resources", "resourceNames", "verbs"}:
            return None, "the rule has fields other than apiGroups, resources, resourceNames, verbs"
        groups, resources = rule.get("apiGroups"), rule.get("resources")
        if not _strings(groups, empty_ok=True) or not _strings(resources):
            return None, "apiGroups and resources must be lists of names"
        if "resourceNames" in rule:
            names = rule["resourceNames"]
            if not _strings(names):
                return None, "resourceNames is not a list of names"
        else:
            names = [""]
        checks = []
        for group in groups:
            for resource in resources:
                base, _, sub = resource.partition("/")
                checks += [
                    (RESOURCE, group, base, sub, verb, name) for verb in verbs for name in names
                ]
    checks = list(dict.fromkeys(checks))
    if len(checks) > MAX_EXPANSION:
        return None, f"the rule expands to {len(checks)} checks (limit {MAX_EXPANSION})"
    return checks, None


def describe(check):
    """A check as `verb resource.group/name` for evidence."""
    if check[0] == NONRESOURCE:
        return f"{check[2]} {check[1]}"
    _, group, resource, sub, verb, name = check
    text = f"{verb} {resource}" + (f"/{sub}" if sub else "") + (f".{group}" if group else "")
    return text + (f" {name}" if name else "")


def review_item(check, namespace=""):
    """The SelfSubjectAccessReview manifest for a check; namespace "" is cluster-wide."""
    if check[0] == NONRESOURCE:
        spec = {"nonResourceAttributes": {"path": check[1], "verb": check[2]}}
    else:
        _, group, resource, sub, verb, name = check
        attrs = {"verb": verb, "group": group, "resource": resource}
        for key, value in (("namespace", namespace), ("subresource", sub), ("name", name)):
            if value:
                attrs[key] = value
        spec = {"resourceAttributes": attrs}
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": spec,
    }


def rules_review_item(namespace):
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectRulesReview",
        "spec": {"namespace": namespace},
    }


def _has(values, wanted):
    """The authorizer's rule matching for verbs and API groups: `*` or an exact value."""
    return "*" in values or wanted in values


def _resource_has(rule_resources, resource, sub):
    combined = f"{resource}/{sub}" if sub else resource
    return any(r == "*" or r == combined or (sub and r == f"*/{sub}") for r in rule_resources)


def allowed_by_rules(status, check):
    """Does a SelfSubjectRulesReview status grant a resource check in its namespace? Matches as
    the RBAC authorizer does: `*` in verbs, groups and resources (also `*/sub`), a subresource
    matched as `resource/sub`, and a rule with resourceNames only for a request with a name in
    them. A requested `*` matches only a rule's `*`, as on the server."""
    _, group, resource, sub, verb, name = check
    for rule in status.get("resourceRules") or []:
        if not _has(rule.get("verbs") or [], verb):
            continue
        if not _has(rule.get("apiGroups") or [], group):
            continue
        if not _resource_has(rule.get("resources") or [], resource, sub):
            continue
        names = rule.get("resourceNames") or []
        if names and name not in names:
            continue
        return True
    return False


def valid_namespaces(namespaces):
    """The namespace names given as the input, checked; None-safe."""
    if not isinstance(namespaces, (list, tuple)) or not all(
        isinstance(n, str) and len(n) <= 63 and _NAMESPACE.match(n) for n in namespaces
    ):
        return None
    return list(dict.fromkeys(namespaces))


def _trim(text, limit=EVIDENCE_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_cluster_scoped(text):
    """{(group, resource)} from `kubectl api-resources --namespaced=false -o name`."""
    found = set()
    for line in text.split():
        resource, _, group = line.partition(".")
        found.add((group, resource))
    return found


def scope_of(check, discovered):
    """CLUSTER, NAMESPACED or None (unknown) for a resource check, by its group and base
    resource (a subresource inherits). `discovered` is the cluster-scoped set from api-resources,
    or None when that call failed; the static table backs it up. A `*` group or resource spans
    both and is asked as namespaced."""
    group, base = check[1], check[2]
    if "*" in (group, base):
        return NAMESPACED
    if (group, base) in STATIC_CLUSTER_SCOPED or (discovered and (group, base) in discovered):
        return CLUSTER
    return None if discovered is None else NAMESPACED


def is_named_namespace_get(check):
    """`get namespaces <name>`: a namespace is its own scope, so a grant inside it counts."""
    if check[0] != RESOURCE:
        return False
    _, group, resource, sub, verb, name = check
    return (group, resource, sub, verb) == ("", "namespaces", "", "get") and bool(name)


def verdict_for(
    checks, cluster, per_namespace, namespaces, complete, why_incomplete="", scopes=None
):
    """(verdict, scope, evidence) for a rule from its expansions.

    cluster: {check: (allowed, text)} for every check; allowed is True, False or None (the
    review failed or the server could not evaluate it: never read as a refusal). per_namespace:
    {ns: {check: True|False|None}}. namespaces: the namespaces to look in. complete: whether every
    one was probed; a rule not granted anywhere is `denied` only then, else `unverified`.
    scopes: {check: CLUSTER | NAMESPACED | None}; a cluster-scoped expansion refused cluster-wide
    makes the rule denied (no namespace can grant it), an unknown one cannot be `partial`."""
    scopes = scopes or {}
    if all(cluster[c][0] is True for c in checks):
        return "granted", None, _trim(f"allowed cluster-wide: {cluster[checks[0]][1]}")
    refused = [c for c in checks if cluster[c][0] is not True]
    first = refused[0]
    if checks[0][0] == NONRESOURCE:
        if cluster[first][0] is None:
            return "unverified", None, _trim(f"could not be checked: {cluster[first][1]}")
        return "denied", None, _trim(f"not allowed: {describe(first)} ({cluster[first][1]})")
    at_cluster = [c for c in refused if scopes.get(c, NAMESPACED) == CLUSTER]
    for c in at_cluster:
        if cluster[c][0] is False:
            return (
                "denied",
                None,
                _trim(f"{describe(c)} is cluster-scoped and not allowed cluster-wide"),
            )
    unasked = next((c for c in refused if cluster[c][0] is None), None)
    if unasked:  # a review that was not answered is never read as a refusal, nor as a partial
        return (
            "unverified",
            None,
            _trim(f"could not be checked: {describe(unasked)} ({cluster[unasked][1]})"),
        )
    unknown_scope = [c for c in refused if scopes.get(c, NAMESPACED) is None]
    scope, unknown = [], False
    for ns in namespaces:
        got = per_namespace.get(ns)
        if got is None:
            unknown = True
            continue
        states = [True if cluster[c][0] is True else got.get(c) for c in checks]
        if all(s is True for s in states):
            scope.append(ns)
        elif any(s is None for s in states) and not any(s is False for s in states):
            unknown = True
    if scope and unknown_scope:
        return (
            "unverified",
            None,
            _trim(
                f"allowed in {len(scope)} namespaces, but whether {describe(unknown_scope[0])} "
                "is cluster-scoped could not be told (api-resources failed)"
            ),
        )
    if scope:
        evidence = (
            f"allowed in {len(scope)} of {len(namespaces)} namespaces, not cluster-wide "
            f"({describe(first)} is refused cluster-wide)"
        )
        if unknown or not complete:
            evidence += "; some namespaces could not be checked, so scope may be incomplete"
        if len(scope) > MAX_SCOPE:
            evidence += f"; scope cut to the first {MAX_SCOPE} of {len(scope)}"
            scope = scope[:MAX_SCOPE]
        return "partial", scope, _trim(evidence)
    if unknown or not complete:
        return (
            "unverified",
            None,
            _trim(
                f"not allowed cluster-wide ({describe(first)}); "
                + (why_incomplete or "some namespaces could not be checked")
            ),
        )
    where = f"in any of {len(namespaces)} namespaces" if namespaces else "(no namespace asked)"
    return "denied", None, _trim(f"not allowed cluster-wide or {where}: {describe(first)}")
