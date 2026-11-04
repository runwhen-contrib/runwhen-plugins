"""kubectl access for this skill's scripts: objects, and HTTP through the API server's pod proxy."""

import json
import subprocess

from k8s_seaweedfs_health.errors import RwError

MESSAGE_LIMIT = 1024
PROXY_TIMEOUT = 20  # seconds for one request through the proxy

# (code, substrings of lower-cased stderr); first match wins.
_CLASSES = (
    (
        "permission_denied",
        ("forbidden", "cannot get", "cannot list", "cannot watch", "cannot create"),
    ),
    ("dependency", ("the server doesn't have a resource type", "executable file not found")),
    ("not_found", ("notfound", "not found")),
    (
        "unreachable",
        (
            "unable to connect",
            "i/o timeout",
            "connection refused",
            "no such host",
            "x509",
            "tls:",
            "tls handshake",
        ),
    ),
    ("timeout", ("deadline exceeded",)),
    ("rate_limited", ("429", "too many requests")),
)


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call."""
    text = (stderr or "").lower()
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            return code
    return "internal"


def _shown(stderr):
    # kubectl prints "Warning: ..." lines before the error; the error is what the message is for
    lines = [x for x in (stderr or "").strip().splitlines() if not x.startswith("Warning:")]
    return "\n".join(lines).strip() or (stderr or "").strip()


def failure(argv, result):
    """The RwError for a failed command result, with a D64 code from its stderr."""
    stderr = (result.stderr or "").strip()
    return RwError(
        classify(result.returncode, stderr),
        f"{argv[0]} exited {result.returncode}: {_shown(stderr)[:MESSAGE_LIMIT]}",
    )


def run_unchecked(ctx, argv, **run_kwargs):
    """`ctx.run(argv)` that returns the finished process whatever its exit code.

    The v13 runner raises on a non-zero exit by default; `check=False` keeps
    the return-code path, which this skill classifies itself. A command that outlives its
    timeout is the `timeout` error.
    """
    try:
        return ctx.run(argv, check=False, **run_kwargs)
    except subprocess.TimeoutExpired:
        raise RwError(
            "timeout", f"{argv[0]} did not finish in {run_kwargs.get('timeout')}s"
        ) from None


def run_checked(ctx, argv, **run_kwargs):
    """`run_unchecked`, raising RwError with a D64 code when the command failed."""
    result = run_unchecked(ctx, argv, **run_kwargs)
    if result.returncode:
        raise failure(argv, result)
    return result


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A failed call (forbidden, not found, unreachable) raises RwError and never
    turns into data here; output that is not JSON is an `internal` error.
    """
    result = run_checked(ctx, ["kubectl", *args, "-o", "json"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def kubectl_json_scoped(ctx, scope, *args):
    """A read the rest of the result can do without: (document, state).

    state is 'ok' or 'denied' (forbidden: reported with `ctx.error` as permission_denied for
    `scope`, document None). Every other failure (unreachable, timeout, missing API, bad
    output) still raises.
    """
    try:
        return kubectl_json(ctx, *args), "ok"
    except RwError as exc:
        if exc.code == "permission_denied":
            ctx.error(
                "permission_denied",
                f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
                scope=scope,
                hint=f"grant get/list on {scope} through a Role in the target's namespace",
            )
            return None, "denied"
        raise


# ---------------------------------------------------------------- pod proxy ----
#
# Every HTTP request to a SeaweedFS process goes through `ctx.k8s_proxy_get` (GET) or
# `ctx.k8s_proxy_request` (the PUT and DELETE of `s3-check`): RBAC `pods/proxy` get, update, delete.
# The answer is the backend's own status and body, and a redirect is never followed with a write and
# never off the pod with a read (security finding S17: `kubectl get|replace|delete --raw` re-sent the
# runner's bearer token to a redirect on another host). The API server's own errors are RwErrors
# of the helper.


class ProxyError(RwError):
    """A failed proxy call. `kind` says where it failed:

    rbac              the API server refused this identity (pods/proxy)
    upstream_denied   the workload answered 401/403 (it wants credentials the task does not hold)
    upstream_missing  the workload answered 404 (no such endpoint, key or bucket)
    upstream_error    the workload answered another non-2xx (400, 409, 500, 503 ...): a status that says
                      nothing about the API server or the network, so a health endpoint may call it
                      unhealthy
    upstream_redirect the workload answered a redirect, which a write never follows and a read follows
                      only to another path of the same pod
    pod_missing       the pod does not exist
    unreachable       the API server could not reach the pod and port
    timeout, rate_limited, other
    """

    def __init__(self, code, message, kind):
        super().__init__(code, message)
        self.kind = kind


BODY_HINT = 80  # characters of a failing reply's body that an error message shows
DROPPED = "x-rw-redirect-dropped"
_API_KINDS = {
    "permission_denied": "rbac",
    "not_found": "pod_missing",
    "unreachable": "unreachable",
    "timeout": "timeout",
    "rate_limited": "rate_limited",
}


def proxy_request(
    ctx,
    method,
    namespace,
    pod,
    port,
    path,
    body=None,
    scheme="http",
    timeout=PROXY_TIMEOUT,
    headers=None,
):
    """The body of a 2xx answer to <method> <path> on the pod's port, as text. Anything else raises
    ProxyError: an error of the API server (kind rbac, pod_missing, unreachable, timeout), or the
    workload's own status (upstream_denied 401/403, upstream_missing 404, upstream_error, and
    upstream_redirect for a redirect). A redirect to another host is a `dependency` error naming the
    host and is never followed; a GET redirected to another path of the same pod is followed once; a
    write is never re-sent anywhere. A 2xx body cut at the helper's size limit is `internal`, never
    parsed in part."""
    path = "/" + path.lstrip("/")
    hops = 0
    while True:
        try:
            if method == "GET":
                reply = ctx.k8s_proxy_get(
                    namespace, "pods", pod, port, path, scheme=scheme, timeout=timeout
                )
            else:
                reply = ctx.k8s_proxy_request(
                    method,
                    namespace,
                    "pods",
                    pod,
                    port,
                    path,
                    body=body,
                    headers=headers,
                    scheme=scheme,
                    timeout=timeout,
                )
        except RwError as exc:
            raise ProxyError(exc.code, exc.message, _API_KINDS.get(exc.code, "other")) from None
        status = reply["status"]
        if method == "GET" and 300 <= status < 400 and reply.get("location") and hops < 1:
            path, hops = reply["location"], hops + 1
            continue
        break
    text = reply.get("body") or ""
    what = f"{method} {path} on pod {pod!r} answered HTTP {status}"
    if 200 <= status < 300:
        if reply.get("truncated"):
            raise ProxyError(
                "internal",
                f"{method} {path} on pod {pod!r} was larger than the helper's limit",
                "other",
            )
        return text
    if 300 <= status < 400:
        dropped = (reply.get("headers") or {}).get(DROPPED)
        if dropped:
            where = f"redirecting off cluster to {dropped}"
        elif reply.get("location"):
            where = f"redirecting to {reply['location']}"
        else:
            where = "redirecting"
        raise ProxyError("dependency", f"{what}, {where} (not followed)", "upstream_redirect")
    shown = f": {text.strip()[:BODY_HINT]!r}" if text.strip() else ""
    if status in (401, 403):
        raise ProxyError("permission_denied", f"{what}{shown}", "upstream_denied")
    if status == 404:
        raise ProxyError("dependency", f"{what}{shown}", "upstream_missing")
    raise ProxyError("internal", f"{what}{shown}", "upstream_error")


def proxy_get(ctx, namespace, pod, port, path, scheme="http", timeout=PROXY_TIMEOUT):
    """The body of a 2xx answer to GET <path> (see proxy_request)."""
    return proxy_request(ctx, "GET", namespace, pod, port, path, None, scheme, timeout)


def proxy_put(ctx, namespace, pod, port, path, body, scheme="http", timeout=PROXY_TIMEOUT):
    """PUT `body` (text) to <path>. `ctx.k8s_proxy_request`: RBAC `pods/proxy` update."""
    headers = {"Content-Type": "text/plain"}
    return proxy_request(ctx, "PUT", namespace, pod, port, path, body, scheme, timeout, headers)


def proxy_delete(ctx, namespace, pod, port, path, scheme="http", timeout=PROXY_TIMEOUT):
    """DELETE <path>. `ctx.k8s_proxy_request`: RBAC `pods/proxy` delete."""
    return proxy_request(ctx, "DELETE", namespace, pod, port, path, None, scheme, timeout)


def proxy_json(ctx, namespace, pod, port, path, what, scheme="http"):
    """GET a JSON object. A body that is not a JSON object is an `internal` error: the pod answered
    with something this skill does not understand, and that is never read as healthy."""
    text = proxy_get(ctx, namespace, pod, port, path, scheme)
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise RwError(
            "internal",
            f"{what} on pod {pod!r} did not answer with a JSON object (first bytes: {text[:80]!r})",
        )
    return doc
