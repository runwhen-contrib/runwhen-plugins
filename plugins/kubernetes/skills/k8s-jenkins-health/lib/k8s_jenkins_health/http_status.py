"""http-status: whether each Jenkins pod serves /login, and which Jenkins version it is."""

import re

from k8s_jenkins_health.common import cap
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.fetch import (
    Budget,
    ask,
    image_version,
    pod_endpoint,
    pod_ready,
    queried,
    no_pod_error,
)
from k8s_jenkins_health.tally import Tally

PATH = "/login"
# The page footer of a Jenkins login page ("Jenkins 2.452.3"): the identity of the page and the
# version. The bare word is not enough (an IdP page can say "Sign in to Jenkins").
FOOTER = re.compile(r"Jenkins (\d+\.\d+(?:\.\d+)?)")
# A page that asks for a login but is not Jenkins's: what an SSO identity provider serves.
LOGIN_PAGE = re.compile(
    r"type=[\"']?password|sign[ -]?in|log[ -]?in|single sign|saml|openid|oauth", re.IGNORECASE
)
SSO_HINT = (
    "/login answered 200 with a login page that is not Jenkins's (no 'Jenkins <version>' footer): "
    "Jenkins behind an SSO realm redirects anonymous requests to the identity provider, so this "
    "skill's facts are limited there; the pod's readiness and log say whether Jenkins is up"
)


def login_row(pod, port, probed, reply=None, scheme=None, prefix=None):
    endpoint = pod_endpoint(pod, port, scheme, prefix)
    version = image_version(pod, port)
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_ready(pod),
        "port": endpoint.port,
        "scheme": endpoint.scheme,
        "urlPrefix": endpoint.prefix,
        "queried": probed,
        "httpStatus": reply.status if reply else None,
        "ok": None,
        "authRequired": bool(reply and reply.status in (401, 403)),
        "ssoRedirect": None,
        "hint": None,
        "version": version,
        "versionSource": "image-tag" if version else None,
    }


def read_login(pod, port, reply, what, scheme=None, prefix=None):
    """The row of a /login reply. 200 with the Jenkins footer (`Jenkins <version>`) is ok true and
    its version; 200 with a login page that is not Jenkins's is `ssoRedirect: likely`, ok null and
    a hint; 503 is ok false; 404 is ok false too (nothing serves /login there); any other 200 is an
    `internal` error, never ok. A version the page does not give is the image tag's."""
    row = login_row(pod, port, True, reply, scheme, prefix)
    if reply.status == 200:
        footer = FOOTER.search(reply.body)
        if footer:
            row["ok"] = True
            row["version"], row["versionSource"] = footer.group(1), "login-footer"
        elif LOGIN_PAGE.search(reply.body):
            row["ssoRedirect"], row["hint"] = "likely", SSO_HINT
        else:
            raise RwError("internal", f"{what} answered 200 but is not a Jenkins login page")
    elif reply.status in (404, 503):
        row["ok"] = False
    return row


def view(ctx, workload, pods, scheme, port, limit, budget=None, prefix=None):
    """The http-status view. `scheme`, `port` and `prefix` are inputs, None meaning the pod's
    readiness probe decides. Pods that are not Running are listed and not asked. A failing pod is
    one error scoped to it; if no asked pod answered at all the first failure is raised, and if
    every answer wanted credentials the task is skipped."""
    budget, tally = budget or Budget(), Tally()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    rows = []
    for pod in cap(pods, limit):
        name = pod["metadata"]["name"]
        if name not in asked:
            rows.append(login_row(pod, port, False, None, scheme, prefix))
            continue
        endpoint = pod_endpoint(pod, port, scheme, prefix)
        what = f"{endpoint.prefix + PATH} on pod {name}"
        try:
            reply = ask(ctx, workload, pod, endpoint, endpoint.prefix + PATH, budget)
            rows.append(read_login(pod, port, reply, what, scheme, prefix))
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            tally.fail(f"pod/{name}", exc)
            rows.append(login_row(pod, port, True, None, scheme, prefix))
            continue
        tally.answer(reply, f"pod/{name}", what)
    tally.finish(ctx, asked)
    no_pod_error(ctx, workload, asked)
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    return {
        "workload": {
            "kind": workload["kind"],
            "name": workload["metadata"]["name"],
            "replicas": spec.get("replicas"),
            "readyReplicas": status.get("readyReplicas"),
        },
        "podCount": len(pods),
        "truncated": len(pods) > limit,
        "okCount": sum(1 for r in rows if r["ok"] is True),
        "notOkCount": sum(1 for r in rows if r["ok"] is False),
        "pods": rows,
    }
