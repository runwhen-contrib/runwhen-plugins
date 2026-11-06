"""HTTP GETs to a pod through the API server's pod proxy, by `ctx.k8s_proxy_get`.

No exec and no port-forward: the API server forwards the request to the pod IP, so the only
RBAC needed is `get` on `pods/proxy`. The SDK helper returns the backend's own status and body,
also for a 404 or a 503 (a 503 from Jaeger is data), and never follows a redirect
(security finding S17: `kubectl get --raw` re-sent the runner's token to a redirect target on
another host). An error of the API server itself is an `RwError`: `permission_denied` for a
missing `pods/proxy` grant, `not_found` for a missing pod, `unreachable` for a pod it cannot
reach, `timeout`. A redirect to another path of the same pod is followed once; one to another host
is reported as such (`redirect_dropped`) and never followed.
"""

from dataclasses import dataclass

from k8s_jaeger_health.errors import RwError
from runwhen_capability import RwError as SdkError

REQUEST_TIMEOUT = 30  # seconds
BODY_HINT = 80  # characters of a failing reply's body that an error message shows
DROPPED = "x-rw-redirect-dropped"


@dataclass(frozen=True)
class Reply:
    status: int
    body: str
    redirect_dropped: str = None  # the host a redirect pointed to, which was not followed


def proxy_get(ctx, namespace, pod, scheme, port, path, timeout=REQUEST_TIMEOUT):
    """GET `path` (with its query) from `scheme://pod:port` through the pod proxy: a Reply for any
    status the backend gave, or RwError for what the API server answered itself (forbidden, no
    such pod, unreachable, timeout). A same-pod redirect is followed once. A 2xx body cut at the
    SDK's size limit is `internal`: never parsed in part."""
    hops = 0
    while True:
        try:
            raw = ctx.k8s_proxy_get(
                namespace, "pods", pod, port, path, scheme=scheme, timeout=timeout
            )
        except SdkError as exc:  # the helper's error, as this skill's RwError (its message cap)
            if isinstance(exc, RwError):
                raise
            raise RwError(
                exc.code, exc.message, scope=exc.scope, hint=exc.hint, details=exc.details
            ) from None
        status = raw["status"]
        location = raw.get("location")
        if 300 <= status < 400 and location and hops < 1:
            path, hops = location, hops + 1
            continue
        break
    body = raw.get("body") or ""
    if raw.get("truncated") and 200 <= status < 300:
        raise RwError(
            "internal",
            f"the reply from pod {pod} was larger than the proxy helper's limit and was cut",
            hint="the endpoint returned more than 4 MiB: narrow the query or use a smaller view",
        )
    dropped = (raw.get("headers") or {}).get(DROPPED)
    return Reply(status, body, dropped)


def status_error(reply, what):
    """The `dependency` error for a reply that is not the 200 the endpoint should give."""
    text = f"{what} answered HTTP {reply.status}"
    if reply.redirect_dropped:
        text += f" redirecting off cluster to {reply.redirect_dropped} (not followed)"
    elif reply.status >= 300 and reply.body.strip():
        text += f": {reply.body.strip()[:BODY_HINT]!r}"
    return RwError("dependency", text)


def expect_200(reply, what):
    """The body of a 200 reply; any other status is `dependency` (the endpoint did not serve)."""
    if reply.status != 200:
        raise status_error(reply, what)
    return reply.body
