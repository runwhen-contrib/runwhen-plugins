"""HTTP GETs to a pod through the API server's pod proxy, by `ctx.k8s_proxy_get`.

No exec, no port-forward and no credential: the API server forwards the request to the pod IP, so
the only RBAC needed is `get` on `pods/proxy`. The SDK helper returns the backend's own status and
body, also for a 401, 403, 404 or 503, and never follows a redirect (security finding S17: `kubectl
get --raw` re-sent the runner's token to a redirect target on another host, which is exactly what
Jenkins behind an SSO realm sends an anonymous request to). An error of the API server itself is
an `RwError` of the helper: `permission_denied` for a missing `pods/proxy` grant, `not_found` for a
missing pod, `unreachable` for a pod it cannot reach, `timeout`.

A redirect to another path of the same pod is followed once. A redirect to another host comes back
as a Reply with its 3xx status and `redirect_dropped` set to the host, so the caller can report it
(`ssoRedirect` on /login): it is never followed. Any status other than 200, 401, 403, 404, 503 and
a redirect is `internal`.
"""

from dataclasses import dataclass

from k8s_jenkins_health.errors import RwError

REQUEST_TIMEOUT = 30  # seconds
BODY_HINT = 80  # characters of a failing reply's body that an error message shows
DROPPED = "x-rw-redirect-dropped"


@dataclass(frozen=True)
class Reply:
    status: int
    body: str = ""
    redirect_dropped: str | None = None  # the host a redirect pointed to, which was not followed

    @property
    def redirected(self):
        return 300 <= self.status < 400


def describe_status(reply, what):
    """`<what> answered HTTP <status>`, naming the host of a redirect that was not followed."""
    text = f"{what} answered HTTP {reply.status}"
    if reply.redirect_dropped:
        text += f" redirecting off cluster to {reply.redirect_dropped} (not followed)"
    return text


def proxy_get(ctx, namespace, pod, scheme, port, path, timeout=REQUEST_TIMEOUT):
    """GET `path` (with its query) from `scheme://pod:port` through the pod proxy: a Reply (200,
    401, 403, 404, 503 or a redirect) or RwError. An RBAC refusal of the proxy (no `get` on
    pods/proxy), a missing pod, an unreachable one and a timeout raise; so does any other status,
    and a 2xx body cut at the SDK's size limit (never parsed in part)."""
    hops = 0
    while True:
        raw = ctx.k8s_proxy_get(namespace, "pods", pod, port, path, scheme=scheme, timeout=timeout)
        status = raw["status"]
        location = raw.get("location")
        if 300 <= status < 400 and location and hops < 1:
            path, hops = location, hops + 1
            continue
        break
    body = raw.get("body") or ""
    if 200 <= status < 300 and raw.get("truncated"):
        raise RwError(
            "internal",
            f"pod {pod!r} answered {path} with more than the proxy helper's limit and the body "
            "was cut",
            hint="the endpoint returned more than 4 MiB: it is not a Jenkins endpoint",
        )
    if status == 200:
        return Reply(200, body)
    if status in (401, 403, 404, 503):
        return Reply(status)
    if 300 <= status < 400:
        return Reply(status, redirect_dropped=(raw.get("headers") or {}).get(DROPPED))
    shown = f": {body.strip()[:BODY_HINT]!r}" if body.strip() else ""
    raise RwError("internal", f"pod {pod!r} answered HTTP {status} for {path}{shown}")
