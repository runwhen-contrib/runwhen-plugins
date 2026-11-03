"""HTTP GETs to a pod or a Service through the API server's proxy (`kubectl get --raw`).

No exec, no port-forward and no credential: the API server forwards the request to the pod IP (or
to an endpoint of the Service), so the only RBAC needed is `get` on `pods/proxy` or
`services/proxy`. Through `kubectl get --raw` an HTTP error from the backend is not an HTTP status
but an error line, and the backend's body is mostly lost, so each status is told apart by its line:

- 404 "the server could not find the requested resource": the backend does not serve that path.
- 503 "the server is currently unable to handle the request": the backend answered unavailable
  (how Artifactory says /readiness is not ready); an API server that is itself overloaded says
  the same. A Service with no ready endpoint says `no endpoints available for service`.
- 403 `(Forbidden)` with no RBAC wording, and 401 `You must be logged in to the server`: the
  backend asked for credentials. The skill sends none. An RBAC refusal of the proxy itself
  (`User "u" cannot get resource "pods/proxy"`) is not this: it raises permission_denied. The 401
  line is the one kubectl prints for a bad API credential too; the workload read made first would
  have failed on that, so on a proxy call it is the backend's.
A pod the API server could not reach ("error trying to reach service") is `unreachable`, never an
HTTP status.
"""

from dataclasses import dataclass

from k8s_artifactory_health.k8s import failure, run_unchecked

REQUEST_TIMEOUT = "30s"
_NOT_FOUND = "the server could not find the requested resource"
_UNAVAILABLE = "currently unable to handle the request"
_NO_ENDPOINTS = "no endpoints available for service"
_LOGIN = "you must be logged in to the server"
_RBAC = ("cannot get resource", "is forbidden: user")


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
    stderr = (result.stderr or "").lower()
    if any(text in stderr for text in _RBAC):
        raise failure(argv, result)
    if _NOT_FOUND in stderr:
        return Reply(404)
    if _NO_ENDPOINTS in stderr:
        return Reply(503, reason="no_endpoints")
    if _UNAVAILABLE in stderr:
        return Reply(503, reason="unavailable")
    if _LOGIN in stderr:
        return Reply(401)
    if "(forbidden)" in stderr:
        return Reply(403)
    raise failure(argv, result)
