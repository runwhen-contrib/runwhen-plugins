"""A stand-in for `ctx.k8s_proxy_get` in the unit tests of the skills that read backend HTTP through
the API server's proxy (S17). It answers like the SDK helper does: the backend's own status and
body (a 404 or 503 is data), an `RwError` for what the API server answers itself, and it records
every request so a test can assert what was asked (and that a redirect was not chased)."""

from runwhen_capability import RwError


def reply(status=200, body="", **extra):
    """What the helper returns: {status, headers, body, truncated, location}."""
    return {
        "status": status,
        "headers": {},
        "body": body,
        "truncated": False,
        "location": None,
        **extra,
    }


def cross_host_redirect(host="example.invalid", status=302):
    """The helper's answer to a backend redirect to another host: no location, the host dropped."""
    return reply(status, "", headers={"x-rw-redirect-dropped": host})


class ProxyFake:
    """`answer(request)` returns a reply dict, a body string (a 200), or an exception to raise (or
    `answer` is one such value, given for every request). A request is {namespace, kind, name,
    port, path, scheme, timeout}, plus `headers` when the call passed some."""

    def __init__(self, answer):
        self.answer = answer if callable(answer) else (lambda request: answer)
        self.requests = []

    def __call__(
        self, namespace, kind, name, port, path, *, headers=None, scheme="http", timeout=30
    ):
        request = {
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "port": port,
            "path": path,
            "scheme": scheme,
            "timeout": timeout,
        }
        if headers is not None:
            request["headers"] = dict(headers)
        self.requests.append(request)
        out = self.answer(request)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, str):
            return reply(200, out)
        return {**reply(), **out}

    def paths(self):
        return [r["path"] for r in self.requests]


def api_error(stderr, classify):
    """The RwError the helper raises for an API-server error text (code from `classify`)."""
    return RwError(classify(1, stderr), stderr)
