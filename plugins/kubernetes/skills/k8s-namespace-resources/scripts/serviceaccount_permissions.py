import json
import os
import tempfile

from k8s_namespace_resources.access import (
    access_entry,
    check_service_account,
    normalize_checks,
    review_manifest,
    user_of,
)
from k8s_namespace_resources.fetch import load_namespace, read, report_denied
from k8s_namespace_resources.k8s import RwError, kubectl_json


def main(ctx, service_account="default", checks=None):
    service_account = check_service_account(service_account)
    checks = normalize_checks(checks)
    ns = load_namespace(ctx, "serviceaccount-permissions")

    # Whether the ServiceAccount exists is a fact beside the answers: a review for a name
    # that does not exist is still answered (by the groups every account gets).
    try:
        _, denied = read(ctx, "-n", ns, "get", "serviceaccounts", service_account)
    except RwError as exc:
        if exc.code != "not_found":
            raise
        exists, denied = False, None
    else:
        exists = True if denied is None else None
    if denied is not None:
        report_denied(ctx, "serviceaccounts", denied)

    # Reviews are created, not stored: `kubectl create -f` of a LocalSubjectAccessReview returns the
    # API server's answer and persists nothing. ctx.run has no stdin, so each goes through a file
    # in the task's scratch directory (cwd, so the argv is the same on every run).
    with tempfile.TemporaryDirectory() as fallback:
        scratch = os.environ.get("RW_OUT") or fallback
        access = []
        for i, check in enumerate(checks):
            name = f"sar-{i}.json"
            with open(os.path.join(scratch, name), "w") as f:
                json.dump(review_manifest(ns, service_account, check), f)
            # --validate=false: no OpenAPI fetch, which a narrow identity may not be allowed, and
            # which would hide the real answer (forbidden to create reviews) behind "failed to check CRD".
            review = kubectl_json(ctx, "create", "--validate=false", "-f", name, cwd=scratch)
            access.append(access_entry(check, review))
    return {
        "serviceAccount": {
            "name": service_account,
            "namespace": ns,
            "user": user_of(ns, service_account),
            "exists": exists,
        },
        "access": access,
    }
