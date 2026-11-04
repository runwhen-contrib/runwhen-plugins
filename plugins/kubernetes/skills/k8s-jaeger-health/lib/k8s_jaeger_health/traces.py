"""Jaeger query API replies and the HTTP status facts in a service's traces (pure functions)."""

import json
import re
from urllib.parse import quote, urlsplit

from k8s_jaeger_health.common import truncate
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.proxy import expect_200

# The span attribute that carries the HTTP status: the one OpenTelemetry's older HTTP semantic
# conventions name, and the one the stable conventions name. Jaeger tag search is exact match on
# one key and one value, so each is asked separately.
STATUS_KEYS = ("http.status_code", "http.response.status_code")
# Where a span's URL path may be, best first: the route is a template with no ids in it; the rest
# are raw paths and go through redact_path. Only the path is read from a value, never its query.
ROUTE_KEYS = ("http.route",)
PATH_KEYS = ("url.path", "http.target", "url.full", "http.url")
# Statuses that count as an HTTP error, whatever the statusCodes setting searched for.
ERROR_RANGE = range(400, 600)
# Pass 1 of the search: OpenTelemetry marks a 5xx server span as an error (status ERROR, which
# Jaeger shows as the tag error=true), whatever the code; a 4xx is not an error to it.
ERROR_TAG = ("error", "true")
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


def traces_path(service, start_us, end_us, limit, key, value):
    """`/api/traces` for the traces of `service` in [start_us, end_us] (microseconds since the
    epoch) with a span whose tag `key` is exactly `value`. The tag filter is JSON and exact: one
    key and one value per query."""
    tags = json.dumps({key: str(value)}, separators=(",", ":"))
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


_HEX = re.compile(r"[0-9a-fA-F]{12,}")
_TOKEN = re.compile(r"[A-Za-z0-9+_=:~\-]{12,}")
_SECRET_PREFIX = re.compile(r"(?i)((sk|pk|rk|ghp|gho|ghs|ghu|glpat)[-_]|xox[a-z]-|akia[a-z0-9])")


def is_id_segment(segment):
    """Whether a path segment looks like an identifier or a secret rather than a name: all digits,
    a JWT, 12 or more hex characters, or 12 or more base64/uuid-like characters that hold a digit
    (or are 20 or more mixed-case letters), or one that starts like an API key (`sk-`, `ghp_`,
    `xoxb-`); Telegram's `bot<id>:<token>` holds digits. Over-redacting a long word is harmless; leaking a
    token is not."""
    if segment.isdigit():
        return True
    if len(segment) < 12:
        return False
    if segment.startswith("eyJ") or _HEX.fullmatch(segment):
        return True
    if not _TOKEN.fullmatch(segment):
        return False
    if any(c.isdigit() for c in segment) or _SECRET_PREFIX.match(segment):
        return True
    return (
        len(segment) >= 20
        and "-" not in segment
        and "_" not in segment
        and segment != segment.lower()
        and segment != segment.upper()
    )


def redact_path(path):
    """A raw URL path with the query, the fragment and `;params` removed and every id-like segment
    replaced by `{id}`: `/reset/9f86d081884c/confirm?x=1` -> `/reset/{id}/confirm`."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = []
    for segment in path.split("/"):
        segment = segment.split(";", 1)[0]
        segments.append("{id}" if is_id_segment(segment) else segment)
    return "/".join(segments) or "/"


def clean_operation(name):
    """An operation name without query strings or fragments, with ids in its paths replaced: each
    word that starts with `/` or `http(s)://` is cut at `?`/`#` and redacted (`GET /cart?token=x`
    -> `GET /cart`); the rest of the name is untouched."""
    words = []
    for word in str(name).split(" "):
        if "://" in word and word.lower().startswith(("http://", "https://")):
            try:
                word = urlsplit(word).path or "/"
            except ValueError:
                word = "/"
        if word.startswith("/"):
            word = redact_path(word)
        words.append(word)
    return " ".join(words)


def path_of(tags):
    """The URL path of a span, without query string or fragment; None when it has no URL tag. The
    route (`http.route`, a template) is preferred; a raw path has `;params` removed and id-like
    segments replaced by `{id}`."""
    for key in ROUTE_KEYS:
        value = tags.get(key)
        if isinstance(value, str) and value:
            return value.split("?", 1)[0].split("#", 1)[0] or "/"
    for key in PATH_KEYS:
        value = tags.get(key)
        if not isinstance(value, str) or not value:
            continue
        if "://" in value:
            try:
                value = urlsplit(value).path or "/"
            except ValueError:
                continue
        return redact_path(value)
    return None


def span_kind(tags):
    """'client' for an outbound call (a span.kind of client), else 'inbound' (server, or no kind)."""
    return "client" if str(tags.get("span.kind") or "").lower() == "client" else "inbound"


class Tally:
    """HTTP error spans of one service, deduplicated across the queries that found them.

    Every span of the service with a status from 400 to 599 in a returned trace counts, whether or
    not the query that returned its trace searched for that status. Spans of kind client (the
    service's own outbound calls) are counted apart from the inbound ones."""

    def __init__(self, service):
        self.service = service
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
                if status not in ERROR_RANGE:
                    continue
                ident = (span.get("traceID"), span.get("spanID"))
                if ident in self.seen_spans:
                    continue
                self.seen_spans.add(ident)
                name = clean_operation(span.get("operationName") or "")
                op = self.ops.setdefault(
                    name,
                    {"by": {}, "out_by": {}, "paths": [], "server": [], "client": [], "out": []},
                )
                if span_kind(tags) == "client":
                    op["out_by"][status] = op["out_by"].get(status, 0) + 1
                    op["out"].append(span.get("traceID"))
                    continue
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
                    "outboundErrorSpans": len(op["out"]),
                    "byStatus": {str(c): n for c, n in sorted(op["by"].items())},
                    "outboundByStatus": {str(c): n for c, n in sorted(op["out_by"].items())},
                    "urlPaths": [p for p, _ in paths],
                    "urlPathsTruncated": len(op["paths"]) > MAX_PATHS,
                    "sampleServerTraceIds": list(dict.fromkeys(op["server"]))[:SAMPLE_TRACES],
                    "sampleClientTraceIds": list(dict.fromkeys(op["client"]))[:SAMPLE_TRACES],
                    "sampleOutboundTraceIds": list(dict.fromkeys(op["out"]))[:SAMPLE_TRACES],
                    "truncated": cut_name or any(cut for _, cut in paths),
                }
            )
        rows.sort(
            key=lambda r: (
                -r["serverErrorSpans"],
                -r["clientErrorSpans"],
                -r["outboundErrorSpans"],
                r["operation"],
            )
        )
        return rows

    def totals(self):
        """(inbound 5xx spans, inbound 4xx spans, outbound spans with a 4xx or 5xx, rows)."""
        rows = self.rows()
        return (
            sum(r["serverErrorSpans"] for r in rows),
            sum(r["clientErrorSpans"] for r in rows),
            sum(r["outboundErrorSpans"] for r in rows),
            rows,
        )
