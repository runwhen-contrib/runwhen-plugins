"""What a ServiceAccount may do in the namespace, asked as SubjectAccessReviews."""

import re

from k8s_namespace_resources.common import MAX_NESTED, trunc
from k8s_namespace_resources.k8s import RwError

DEFAULT_CHECKS = (
    {"verb": "get", "resource": "pods"},
    {"verb": "list", "resource": "pods"},
    {"verb": "get", "resource": "configmaps"},
    {"verb": "list", "resource": "services"},
)

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_VERB = re.compile(r"^[a-z*][a-z-]{0,62}$")
_RESOURCE = re.compile(r"^(\*|[a-z0-9][a-z0-9.-]{0,62})(/[a-z0-9][a-z0-9.-]{0,62})?$")
_GROUP = re.compile(r"^(\*|[a-z0-9][a-z0-9.-]{0,252})?$")


def _get(obj, key):
    return obj.get(key) if hasattr(obj, "get") else getattr(obj, key, None)


def normalize_checks(checks):
    """The checks as [{verb, resource, subresource, group}]; a malformed one is invalid_input.

    `resource` may be written `pods/log` (resource and subresource). `group` is
    the API group ('' for core).
    """
    if checks is None:
        checks = DEFAULT_CHECKS
    checks = list(checks)
    if not checks:
        raise RwError("invalid_input", "checks must list at least one {verb, resource}")
    if len(checks) > MAX_NESTED:
        raise RwError("invalid_input", f"checks has {len(checks)} entries, at most {MAX_NESTED}")
    out = []
    for i, check in enumerate(checks):
        verb, resource = _get(check, "verb"), _get(check, "resource")
        group = _get(check, "group") or ""
        if not isinstance(verb, str) or not _VERB.match(verb):
            raise RwError("invalid_input", f"checks[{i}].verb is not a verb: {verb!r}")
        if not isinstance(resource, str) or not _RESOURCE.match(resource):
            raise RwError("invalid_input", f"checks[{i}].resource is not a resource: {resource!r}")
        if not isinstance(group, str) or not _GROUP.match(group):
            raise RwError("invalid_input", f"checks[{i}].group is not an API group: {group!r}")
        resource, _, subresource = resource.partition("/")
        out.append(
            {"verb": verb, "resource": resource, "subresource": subresource or None, "group": group}
        )
    return out


def check_service_account(name):
    if not isinstance(name, str) or not _NAME.match(name):
        raise RwError("invalid_input", f"serviceAccount is not a ServiceAccount name: {name!r}")
    return name


def user_of(namespace, service_account):
    return f"system:serviceaccount:{namespace}:{service_account}"


def review_manifest(namespace, service_account, check):
    """The SubjectAccessReview asking whether the ServiceAccount, with the groups
    the API server gives it, may do `check` in the namespace."""
    attributes = {
        "namespace": namespace,
        "verb": check["verb"],
        "resource": check["resource"],
        "group": check["group"],
    }
    if check["subresource"]:
        attributes["subresource"] = check["subresource"]
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": user_of(namespace, service_account),
            "groups": [
                "system:serviceaccounts",
                f"system:serviceaccounts:{namespace}",
                "system:authenticated",
            ],
            "resourceAttributes": attributes,
        },
    }


def access_entry(check, review):
    """One output entry from the SubjectAccessReview the API server answered."""
    status = review.get("status") or {}
    reason, cut_reason = trunc(status.get("reason"))
    evaluation, cut_eval = trunc(status.get("evaluationError"))
    return {
        "verb": check["verb"],
        "resource": check["resource"],
        "subresource": check["subresource"],
        "group": check["group"],
        "allowed": bool(status.get("allowed")),
        "denied": bool(status.get("denied")),
        "reason": reason,
        "evaluationError": evaluation,
        "truncated": cut_reason or cut_eval,
    }
