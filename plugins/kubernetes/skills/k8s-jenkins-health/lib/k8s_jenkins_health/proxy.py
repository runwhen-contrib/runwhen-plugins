"""HTTP GETs to a pod through the API server's pod proxy (`kubectl get --raw`).

No exec, no port-forward and no credential: the API server forwards the request to the pod IP, so
the only RBAC needed is `get` on `pods/proxy`. Through `kubectl get --raw` an HTTP error from the
backend is not an HTTP status but an `Error from server (<Reason>): <message>` line, and the
backend's body is lost except for a 401 or 403 (kubectl keeps the body there). Each status is told
apart by the line's reason and the apiserver's own fixed message, never by the backend's body text:

- NotFound "the server could not find the requested resource": the backend does not serve that
  path. Any other NotFound (a missing pod or namespace) is `not_found`.
- ServiceUnavailable "the server is currently unable to handle the request": the backend answered
  unavailable (Jenkins while it starts or shuts down); an API server that is itself overloaded says
  the same. "error trying to reach service" is `unreachable`, never an HTTP status.
- Forbidden whose message is not the RBAC wording: Jenkins refused an anonymous caller (the usual
  answer of `/queue/api/json` and `/computer/api/json` once anonymous read is off). An RBAC refusal
  of the proxy itself (`pods "p" is forbidden: User "u" cannot get resource "pods/proxy"`, anchored
  at the start of the message) is not this: it raises permission_denied.
- Unauthorized, and kubectl's own line `error: You must be logged in to the server`: a 401. kubectl
  prints that line for a rejected API credential too, and for a token that has expired; the
  workload read made first would have failed on that, but a token that expires between that read
  and a proxy call is read here as the backend wanting credentials.

TODO(redirect-token test pending): a backend 3xx (Jenkins behind an SSO realm redirects anonymous
requests to the IdP) is not handled here yet; what the proxy and kubectl do with a redirect, and
whether the redirect target can carry a token, is being tested separately. Until it reports, an
SSO login page that comes back as a 200 is told apart in http_status.py (`ssoRedirect: likely`).
"""

import re
from dataclasses import dataclass

from k8s_jenkins_health.k8s import failure, run_unchecked, split_reason

REQUEST_TIMEOUT = "30s"
_NOT_FOUND = "the server could not find the requested resource"
_UNAVAILABLE = "the server is currently unable to handle the request"
_LOGIN = "you must be logged in to the server"
# `<resource> "<name>" is forbidden: User "<user>" cannot get resource "pods/proxy" ...`: the
# apiserver's authorization wording, matched from the start of the message (a backend's body that
# merely mentions it does not match)
_RBAC = re.compile(r'^[a-z][a-z0-9.-]*(?:/[a-z]+)?(?: "[^"]*")? is forbidden: User "[^"]*" cannot ')


@dataclass(frozen=True)
class Reply:
    status: int
    body: str = ""


def pod_path(namespace, pod, scheme, port, path):
    """The API path that proxies GET `path` (with its query) to `scheme://pod:port`."""
    return f"/api/v1/namespaces/{namespace}/pods/{scheme}:{pod}:{port}/proxy{path}"


def proxy_get(ctx, api_path, request_timeout=REQUEST_TIMEOUT):
    """GET through the proxy: a Reply (200, 401, 403, 404 or 503) or RwError.

    An RBAC refusal of the proxy (no `get` on pods/proxy), a missing pod or namespace
    (not_found), an unreachable pod and a timeout raise; any other HTTP error from the backend is
    `internal`.
    """
    argv = ["kubectl", "get", "--raw", api_path, f"--request-timeout={request_timeout}"]
    result = run_unchecked(ctx, argv)
    if not result.returncode:
        return Reply(200, result.stdout or "")
    reason, message = split_reason(result.stderr)
    if reason is None:
        if _LOGIN in message.lower():
            return Reply(401)
    elif reason == "Forbidden" and not _RBAC.match(message):
        return Reply(403)
    elif reason == "Unauthorized":
        return Reply(401)
    elif reason == "NotFound" and message == _NOT_FOUND:
        return Reply(404)
    elif reason == "ServiceUnavailable" and message == _UNAVAILABLE:
        return Reply(503)
    raise failure(argv, result)
