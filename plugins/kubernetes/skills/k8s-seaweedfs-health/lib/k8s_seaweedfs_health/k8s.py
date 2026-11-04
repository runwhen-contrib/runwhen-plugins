"""kubectl access for this skill's scripts: objects, and HTTP through the API server's pod proxy."""

import json
import re
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
# HTTP to a SeaweedFS process goes through the API server: GET/PUT/DELETE
# /api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/<path>, RBAC `pods/proxy`
# (get, update for PUT, delete). `kubectl get --raw` prints the body of a 2xx answer and
# NOTHING of any other: a non-2xx from the workload comes back as a bare "Error from server
# (<Reason>): ..." line, so the HTTP status is known only as that reason. proxy_failure maps it,
# and never lets a proxy error read as a healthy answer.


class ProxyError(RwError):
    """A failed proxy call. `kind` says where it failed:

    rbac              the API server refused this identity (pods/proxy)
    upstream_denied   the workload answered 401/403 (it wants credentials the task does not hold)
    upstream_missing  the workload answered 404 (no such endpoint, key or bucket)
    upstream_error    the workload answered another non-2xx (400, 409, ...): a status that says nothing
                      about the API server or the network, so a health endpoint may call it unhealthy
    api_unavailable   `Error from server (ServiceUnavailable)`: a 503 that kubectl cannot tell from the
                      API server's own
    api_error         `Error from server (InternalError)`: a 500, likewise
    pod_missing       the pod does not exist
    unreachable       the API server could not reach the pod and port
    timeout, rate_limited, other
    """

    def __init__(self, code, message, kind):
        super().__init__(code, message)
        self.kind = kind


def proxy_path(namespace, pod, port, path, scheme="http"):
    return (
        f"/api/v1/namespaces/{namespace}/pods/{scheme}:{pod}:{int(port)}/proxy/{path.lstrip('/')}"
    )


_REASON = re.compile(r"error from server \((\w+)\)")


def proxy_failure(argv, result):
    """The ProxyError for a failed `kubectl <verb> --raw` result."""
    stderr = (result.stderr or "").strip()
    text = stderr.lower()
    reason = (_REASON.search(text) or [None, ""])[1]
    shown = _shown(stderr)[:MESSAGE_LIMIT]
    msg = f"{argv[0]} {argv[1]} --raw exited {result.returncode}: {shown}"
    if (
        result.returncode == 124
        or reason in ("timeout", "gatewaytimeout")
        or "deadline exceeded" in text
    ):
        return ProxyError("timeout", msg, "timeout")
    if reason in ("toomanyrequests",) or "too many requests" in text:
        return ProxyError("rate_limited", msg, "rate_limited")
    if reason in ("forbidden", "unauthorized") or "forbidden" in text:
        if " cannot " in text or "is forbidden" in text or "you must be logged in" in text:
            return ProxyError("permission_denied", msg, "rbac")
        return ProxyError("permission_denied", msg, "upstream_denied")
    if reason == "notfound" or "not found" in text:
        if "could not find the requested resource" in text:
            return ProxyError("dependency", msg, "upstream_missing")
        return ProxyError("not_found", msg, "pod_missing")
    if (
        "error trying to reach service" in text
        or "unable to connect" in text
        or "connection refused" in text
        or "no such host" in text
        or "no route to host" in text
        or "x509" in text
        or "i/o timeout" in text
    ):
        return ProxyError("unreachable", msg, "unreachable")
    # 5xx: the API server's own and the workload's both come back as these reasons, so they are
    # typed errors, never an "unhealthy" the caller could read as the workload's answer
    if reason == "serviceunavailable":
        return ProxyError("unreachable", msg, "api_unavailable")
    if reason == "internalerror":
        return ProxyError("internal", msg, "api_error")
    if result.returncode == 127:
        return ProxyError("dependency", msg, "other")
    return ProxyError("internal", msg, "upstream_error")


def proxy_run(ctx, argv, timeout=PROXY_TIMEOUT, **run_kwargs):
    try:
        result = run_unchecked(ctx, argv, timeout=timeout, **run_kwargs)
    except RwError as exc:
        if (
            exc.code == "timeout"
        ):  # the command outlived its budget: the same kind as a gateway timeout
            raise ProxyError("timeout", exc.message, "timeout") from None
        raise
    if result.returncode:
        raise proxy_failure(argv, result)
    return result.stdout or ""


def proxy_get(ctx, namespace, pod, port, path, scheme="http", timeout=PROXY_TIMEOUT):
    """The body of a 2xx answer to GET <path> on the pod's port, as text. Anything else raises
    ProxyError."""
    return proxy_run(
        ctx,
        ["kubectl", "get", "--raw", proxy_path(namespace, pod, port, path, scheme)],
        timeout=timeout,
    )


def proxy_put(ctx, namespace, pod, port, path, body_file, scheme="http", timeout=PROXY_TIMEOUT):
    """PUT the content of `body_file` (a file name in ctx.workdir, which the command runs in, so the
    argv does not depend on where the workdir is) to <path>: `kubectl replace --raw`, the API
    server verb update."""
    argv = ["kubectl", "replace", "--raw", proxy_path(namespace, pod, port, path, scheme)]
    return proxy_run(ctx, [*argv, "-f", body_file], timeout=timeout, cwd=ctx.workdir)


def proxy_delete(ctx, namespace, pod, port, path, scheme="http", timeout=PROXY_TIMEOUT):
    """DELETE <path> (`kubectl delete --raw`: the API server verb is delete)."""
    argv = ["kubectl", "delete", "--raw", proxy_path(namespace, pod, port, path, scheme)]
    return proxy_run(ctx, argv, timeout=timeout)


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
