"""HTTP GETs to a pod through the API server's pod proxy (`kubectl get --raw`).

No exec, no port-forward and no credential: the API server forwards the request to the pod IP, so
the only RBAC needed is `get` on `pods/proxy`. Through `kubectl get --raw` an HTTP error from the
backend is not an HTTP status but an error line, and the backend's body is mostly lost, so each
status is told apart by its line:

- 404 "the server could not find the requested resource": the backend does not serve that path.
- 503 "the server is currently unable to handle the request": the backend answered unavailable
  (Jenkins while it starts or shuts down); an API server that is itself overloaded says the same.
- 403 `(Forbidden)` with no RBAC wording: Jenkins refused an anonymous caller (the usual answer of
  `/queue/api/json` and `/computer/api/json` once anonymous read is off). An RBAC refusal of the
  proxy itself (`User "u" cannot get resource "pods/proxy"`) is not this: it raises
  permission_denied.
- 401 `You must be logged in to the server`: kubectl's line for a backend 401, which it also prints
  for a bad API credential; the workload read made first would have failed on that, so on a proxy
  call it is the backend's.
A pod the API server could not reach ("error trying to reach service") is `unreachable`, never an
HTTP status.

Response headers: the proxy forwards the backend's headers and `kubectl -v=8` logs them on stderr,
so `proxy_get(..., headers=True)` reads one header (X-Jenkins) from there. `headers_exposed` says
whether the log showed any response headers at all, so a missing header is told from a proxy that
did not show headers. The log lines carry no credential: client-go masks Authorization.
"""

import re
from dataclasses import dataclass

from k8s_jenkins_health.k8s import failure, run_unchecked

REQUEST_TIMEOUT = "30s"
_NOT_FOUND = "the server could not find the requested resource"
_UNAVAILABLE = "currently unable to handle the request"
_LOGIN = "you must be logged in to the server"
_RBAC = ("cannot get resource", "is forbidden: user")
_ERROR_LINE = re.compile(r"^(Error from server|error:|Unable to connect|Error:)", re.IGNORECASE)
_RESPONSE_HEADERS = re.compile(r'"Response".*headers=<')
_VERSION = re.compile(r"^\s*X-Jenkins:[ \t]*(\S{1,64})", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Reply:
    status: int
    body: str = ""
    # the X-Jenkins header when the headers were asked for and shown, else None
    version: str | None = None
    headers_exposed: bool = False


def pod_path(namespace, pod, scheme, port, path):
    """The API path that proxies GET `path` (with its query) to `scheme://pod:port`."""
    return f"/api/v1/namespaces/{namespace}/pods/{scheme}:{pod}:{port}/proxy{path}"


def _error_text(stderr):
    """The error lines of kubectl's stderr: with -v=8 it also holds log lines and a header dump,
    none of which say what failed."""
    lines = [line for line in (stderr or "").splitlines() if _ERROR_LINE.match(line)]
    return "\n".join(lines) if lines else (stderr or "")


def proxy_get(ctx, api_path, request_timeout=REQUEST_TIMEOUT, headers=False):
    """GET through the proxy: a Reply (200, 401, 403, 404 or 503) or RwError.

    An RBAC refusal of the proxy (no `get` on pods/proxy), a missing pod or namespace
    (not_found), an unreachable pod and a timeout raise; any other HTTP error from the backend is
    `internal`.
    """
    argv = ["kubectl", "get", "--raw", api_path, f"--request-timeout={request_timeout}"]
    if headers:
        argv.append("-v=8")
    result = run_unchecked(ctx, argv)
    stderr = result.stderr or ""
    version = None
    exposed = bool(headers and _RESPONSE_HEADERS.search(stderr))
    if exposed:
        found = _VERSION.search(stderr)
        version = found.group(1) if found else None
    if not result.returncode:
        return Reply(200, result.stdout or "", version, exposed)
    text = _error_text(stderr).lower()
    if any(needle in text for needle in _RBAC):
        raise failure(argv, _with_stderr(result, _error_text(stderr)))
    if _NOT_FOUND in text:
        return Reply(404, "", version, exposed)
    if _UNAVAILABLE in text:
        return Reply(503, "", version, exposed)
    if _LOGIN in text:
        return Reply(401, "", version, exposed)
    if "(forbidden)" in text:
        return Reply(403, "", version, exposed)
    raise failure(argv, _with_stderr(result, _error_text(stderr)))


def _with_stderr(result, stderr):
    return type(
        "Result", (), {"returncode": result.returncode, "stdout": result.stdout, "stderr": stderr}
    )
