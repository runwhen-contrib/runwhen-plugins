"""HTTP GETs to a pod or a Service through the API server's proxy (`kubectl get --raw`).

No exec, no port-forward and no credential: the API server forwards the request to the pod IP (or
to an endpoint of the Service), so the only RBAC needed is `get` on `pods/proxy` or
`services/proxy`. Through `kubectl get --raw` an HTTP error from the backend is not an HTTP status
but an `Error from server (<Reason>): <message>` line, and the backend's body is lost except for a
401 or 403 (kubectl keeps the body there). Each status is told apart by the line's reason and the
apiserver's own fixed message, never by the backend's body text:

- NotFound "the server could not find the requested resource": the backend does not serve that
  path. Any other NotFound (a missing pod or namespace) is `not_found`.
- ServiceUnavailable "the server is currently unable to handle the request": the backend answered
  unavailable (how Artifactory says /readiness is not ready); an API server that is itself
  overloaded says the same. A Service with no ready endpoint says `no endpoints available for
  service`. "error trying to reach service" is `unreachable`, never an HTTP status.
- Forbidden whose message is not the RBAC wording, and Unauthorized: the backend asked for
  credentials. The skill sends none. An RBAC refusal of the proxy itself (`pods "p" is forbidden:
  User "u" cannot get resource "pods/proxy"`, anchored at the start of the message) is not this:
  it raises permission_denied.
- kubectl's own line `error: You must be logged in to the server` is a 401. It is also what kubectl
  prints when the API credential itself is rejected or its token has expired; the workload read
  made first would have failed on that, but a token that expires between that read and a proxy
  call is read here as the backend wanting credentials.
"""

import re
from dataclasses import dataclass

from k8s_artifactory_health.k8s import failure, run_unchecked, split_reason

REQUEST_TIMEOUT = "30s"
_NOT_FOUND = "the server could not find the requested resource"
_UNAVAILABLE = "the server is currently unable to handle the request"
_NO_ENDPOINTS = "no endpoints available for service"
_LOGIN = "you must be logged in to the server"
# `<resource> "<name>" is forbidden: User "<user>" cannot get resource "pods/proxy" ...`: the
# apiserver's authorization wording, matched from the start of the message (a backend's body that
# merely mentions it does not match)
_RBAC = re.compile(r'^[a-z][a-z0-9.-]*(?:/[a-z]+)?(?: "[^"]*")? is forbidden: User "[^"]*" cannot ')


@dataclass(frozen=True)
class Reply:
    status: int
    body: str = ""
    # why a 503 happened when the proxy says so: "no_endpoints" or "unavailable"
    reason: str | None = None


def pod_path(namespace, pod, scheme, port, path):
    """The API path that proxies GET `path` (with its query) to `scheme://pod:port`."""
    return f"/api/v1/namespaces/{namespace}/pods/{scheme}:{pod}:{port}/proxy{path}"


def service_path(namespace, service, scheme, port, path):
    """The API path that proxies GET `path` to an endpoint of the Service's port `port` (its
    name, or its number)."""
    return f"/api/v1/namespaces/{namespace}/services/{scheme}:{service}:{port}/proxy{path}"


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
    elif reason == "ServiceUnavailable":
        if message.startswith(_NO_ENDPOINTS):
            return Reply(503, reason="no_endpoints")
        if message == _UNAVAILABLE:
            return Reply(503, reason="unavailable")
    raise failure(argv, result)
