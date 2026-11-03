"""HTTP GETs to a pod through the API server's pod proxy (`kubectl get --raw`).

No exec and no port-forward: the API server forwards the request to the pod IP, so the only
RBAC needed is `get` on `pods/proxy`. The skill adds no credential to the request and reads no
Secret.
Through `kubectl get --raw` an HTTP error from the backend is not an HTTP status but an error
line, and the backend's body is lost, so two statuses are told apart by their line: 404 ("the
server could not find the requested resource": the pod does not serve that path) and 503 ("the
server is currently unable to handle the request": the pod answered unavailable; an API server
that is itself overloaded says the same). A backend 403 is not told apart from a missing
`pods/proxy` permission: both are `permission_denied`. A pod the API server could not reach
("error trying to reach service") is `unreachable`, never an HTTP status.
"""

from dataclasses import dataclass

from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.k8s import failure, run_unchecked

REQUEST_TIMEOUT = "30s"
_NOT_FOUND = "the server could not find the requested resource"
_UNAVAILABLE = "currently unable to handle the request"


@dataclass(frozen=True)
class Reply:
    status: int
    body: str


def pod_path(namespace, pod, scheme, port, path):
    """The API path that proxies GET `path` (with its query) to `scheme://pod:port`."""
    return f"/api/v1/namespaces/{namespace}/pods/{scheme}:{pod}:{port}/proxy{path}"


def proxy_get(ctx, api_path, request_timeout=REQUEST_TIMEOUT):
    """GET through the proxy: a Reply (200, 404 or 503) or RwError.

    Forbidden (no `pods/proxy get`), a missing pod or namespace (not_found), an unreachable
    pod and a timeout raise; any other HTTP error from the backend is `internal`.
    """
    argv = ["kubectl", "get", "--raw", api_path, f"--request-timeout={request_timeout}"]
    result = run_unchecked(ctx, argv)
    if not result.returncode:
        return Reply(200, result.stdout or "")
    stderr = (result.stderr or "").lower()
    if _NOT_FOUND in stderr:
        return Reply(404, "")
    if _UNAVAILABLE in stderr:
        return Reply(503, "")
    raise failure(argv, result)


def expect_200(reply, what):
    """The body of a 200 reply; 404 and 503 are `dependency` (the endpoint did not serve)."""
    if reply.status != 200:
        raise RwError("dependency", f"{what} answered HTTP {reply.status}")
    return reply.body
