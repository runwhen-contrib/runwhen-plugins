"""HTTP GETs to a pod or a Service through the API server's proxy, by `ctx.k8s_proxy_get`.

No exec, no port-forward and no credential: the API server forwards the request to the pod IP (or
to an endpoint of the Service), so the only RBAC needed is `get` on `pods/proxy` or
`services/proxy`. The SDK helper returns the backend's own status and body, also for a 401, 403,
404 or 503 (Artifactory says /readiness is not ready with a 503), and never follows a redirect
(security finding S17: `kubectl get --raw` re-sent the runner's token to a redirect target on
another host). An error of the API server itself is an `RwError` of the helper: `permission_denied`
for a missing `pods/proxy` grant, `not_found` for a missing pod or Service, `unreachable` for a
pod it cannot reach and for a Service with no ready endpoint ("no endpoints available for
service"; that one is read here as a 503 with `reason: no_endpoints`), `timeout`.

A redirect to another path of the same pod or Service is followed once; one to another host is an
error that names the host (`dependency`) and is never followed. Any other status than 200, 401,
403, 404 and 503 is `internal`.
"""

from dataclasses import dataclass

from k8s_artifactory_health.errors import RwError

REQUEST_TIMEOUT = 30  # seconds
BODY_HINT = 80  # characters of a failing reply's body that an error message shows
DROPPED = "x-rw-redirect-dropped"
_NO_ENDPOINTS = "no endpoints available for service"


@dataclass(frozen=True)
class Reply:
    status: int
    body: str = ""
    # why a 503 happened when the proxy says so: "no_endpoints" or "unavailable"
    reason: str | None = None


@dataclass(frozen=True)
class Target:
    """One request: `kind` (pods or services) `name` in `namespace`, at `scheme` and `port`, GET
    `path` (with its query)."""

    namespace: str
    kind: str
    name: str
    scheme: str
    port: object
    path: str


def pod_target(namespace, pod, scheme, port, path):
    return Target(namespace, "pods", pod, scheme, port, path)


def service_target(namespace, service, scheme, port, path):
    """GET `path` from an endpoint of the Service's port `port` (its name, or its number)."""
    return Target(namespace, "services", service, scheme, port, path)


def proxy_get(ctx, target, timeout=REQUEST_TIMEOUT):
    """GET `target`: a Reply (200, 401, 403, 404 or 503) or RwError. An RBAC refusal of the proxy
    (no `get` on pods/proxy), a missing pod or Service (not_found), an unreachable one and a timeout
    raise; a redirect to another host or any other HTTP status from the backend raises too, and a
    2xx body cut at the SDK's size limit is `internal`: never parsed in part."""
    path, hops = target.path, 0
    while True:
        try:
            raw = ctx.k8s_proxy_get(
                target.namespace,
                target.kind,
                target.name,
                target.port,
                path,
                scheme=target.scheme,
                timeout=timeout,
            )
        except RwError as exc:
            if exc.code == "unreachable" and _NO_ENDPOINTS in exc.message:
                return Reply(503, reason="no_endpoints")
            raise
        status = raw["status"]
        location = raw.get("location")
        if 300 <= status < 400 and location and hops < 1:
            path, hops = location, hops + 1
            continue
        break
    body = raw.get("body") or ""
    what = f"{target.kind[:-1]} {target.name!r} answered HTTP {status} for {path}"
    if 200 <= status < 300 and raw.get("truncated"):
        raise RwError(
            "internal",
            f"{target.kind[:-1]} {target.name!r} answered {path} with more than the proxy helper's "
            "limit and the body was cut",
            hint="the endpoint returned more than 4 MiB: it is not an Artifactory health endpoint",
        )
    if status == 200:
        return Reply(200, body)
    if status in (401, 403, 404):
        return Reply(status)
    if status == 503:
        return Reply(503, reason="unavailable")
    dropped = (raw.get("headers") or {}).get(DROPPED)
    if 300 <= status < 400:
        where = f" redirecting off cluster to {dropped} (not followed)" if dropped else ""
        raise RwError("dependency", f"{what}{where}")
    shown = f": {body.strip()[:BODY_HINT]!r}" if body.strip() else ""
    raise RwError("internal", f"{what}{shown}")
