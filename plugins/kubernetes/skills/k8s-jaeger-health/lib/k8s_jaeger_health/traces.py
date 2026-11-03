"""Jaeger query API replies and the HTTP status facts in a service's traces (pure functions)."""

import json
from urllib.parse import quote, urlsplit

from k8s_jaeger_health.common import truncate
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.proxy import expect_200

# The span attribute that carries the HTTP status: the one OpenTelemetry's older HTTP semantic
# conventions name, and the one the stable conventions name. Jaeger tag search is exact match on
# one key and one value, so each is asked separately.
STATUS_KEYS = ("http.status_code", "http.response.status_code")
# Where a span's URL path may be, best first. Only the path is read from a value, never its query.
PATH_KEYS = ("url.path", "http.target", "url.full", "http.url", "http.route")
SAMPLE_TRACES = 5
MAX_PATHS = 10


def query_json(reply, what):
    """The parsed body of a Jaeger query API reply. Anything else (an HTML page, text, a JSON
    without `data`, a body that lists `errors`) is `internal`: never read as an empty result."""
    body = expect_200(reply, what)
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or "data" not in doc:
        raise RwError("internal", f"{what} did not return Jaeger query JSON: {body.strip()[:80]!r}")
    if doc.get("errors"):
        raise RwError("internal", f"{what} returned errors: {json.dumps(doc['errors'])[:200]}")
    return doc


def service_names(reply, what):
    """The service names of `/api/services`: a list of strings (null is an empty store)."""
    data = query_json(reply, what)["data"]
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
        raise RwError("internal", f"{what} did not return a list of service names")
    return sorted(set(data))


def traces_path(service, start_us, end_us, limit, key, code):
    """`/api/traces` for the traces of `service` in [start_us, end_us] (microseconds since the
    epoch) with a span whose tag `key` is exactly `code`. The tag filter is JSON and exact: one
    key and one value per query."""
    tags = json.dumps({key: str(code)}, separators=(",", ":"))
    return (
        f"/api/traces?service={quote(service, safe='')}&start={start_us}&end={end_us}"
        f"&limit={limit}&tags={quote(tags, safe='')}"
    )


def trace_list(reply, what):
    """The traces of an `/api/traces` reply: a list of objects with `spans` (a list) and
    `processes` (an object)."""
    data = query_json(reply, what)["data"]
    if not isinstance(data, list):
        raise RwError("internal", f"{what} did not return a list of traces")
    for trace in data:
        if (
            not isinstance(trace, dict)
            or not isinstance(trace.get("spans"), list)
            or not isinstance(trace.get("processes"), dict)
        ):
            raise RwError("internal", f"{what} returned a trace without spans and processes")
    return data


def tag_values(span):
    """{key: value} of a span's tags (a tag without a key is skipped)."""
    return {
        t["key"]: t.get("value")
        for t in span.get("tags") or []
        if isinstance(t, dict) and isinstance(t.get("key"), str)
    }


def status_of(tags):
    """The HTTP status of a span (an int tag, or a string of digits), None when it has none."""
    for key in STATUS_KEYS:
        value = tags.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def path_of(tags):
    """The URL path of a span, without query string or fragment; None when it has no URL tag."""
    for key in PATH_KEYS:
        value = tags.get(key)
        if not isinstance(value, str) or not value:
            continue
        if "://" in value:
            try:
                return urlsplit(value).path or "/"
            except ValueError:
                continue
        return value.split("?", 1)[0].split("#", 1)[0] or "/"
    return None


class Tally:
    """HTTP error spans of one service, deduplicated across the queries that found them."""

    def __init__(self, service, codes):
        self.service = service
        self.codes = set(codes)
        self.seen_spans = set()
        self.trace_ids = set()
        self.ops = {}

    def add(self, traces, start_us, end_us):
        for trace in traces:
            self.trace_ids.add(trace.get("traceID"))
            for span in trace["spans"]:
                process = trace["processes"].get(span.get("processID")) or {}
                if process.get("serviceName") != self.service:
                    continue
                begun = span.get("startTime")
                if not isinstance(begun, int) or not start_us <= begun <= end_us:
                    continue
                tags = tag_values(span)
                status = status_of(tags)
                if status not in self.codes:
                    continue
                ident = (span.get("traceID"), span.get("spanID"))
                if ident in self.seen_spans:
                    continue
                self.seen_spans.add(ident)
                op = self.ops.setdefault(
                    span.get("operationName") or "",
                    {"by": {}, "paths": [], "server": [], "client": []},
                )
                op["by"][status] = op["by"].get(status, 0) + 1
                path = path_of(tags)
                if path is not None and path not in op["paths"]:
                    op["paths"].append(path)
                (op["server"] if status >= 500 else op["client"]).append(span.get("traceID"))

    def rows(self):
        rows = []
        for name, op in self.ops.items():
            text, cut_name = truncate(name)
            paths = [truncate(p) for p in op["paths"][:MAX_PATHS]]
            rows.append(
                {
                    "operation": text,
                    "serverErrorSpans": len(op["server"]),
                    "clientErrorSpans": len(op["client"]),
                    "byStatus": {str(c): n for c, n in sorted(op["by"].items())},
                    "urlPaths": [p for p, _ in paths],
                    "urlPathsTruncated": len(op["paths"]) > MAX_PATHS,
                    "sampleServerTraceIds": list(dict.fromkeys(op["server"]))[:SAMPLE_TRACES],
                    "sampleClientTraceIds": list(dict.fromkeys(op["client"]))[:SAMPLE_TRACES],
                    "truncated": cut_name or any(cut for _, cut in paths),
                }
            )
        rows.sort(key=lambda r: (-r["serverErrorSpans"], -r["clientErrorSpans"], r["operation"]))
        return rows

    def totals(self):
        rows = self.rows()
        return (
            sum(r["serverErrorSpans"] for r in rows),
            sum(r["clientErrorSpans"] for r in rows),
            rows,
        )
