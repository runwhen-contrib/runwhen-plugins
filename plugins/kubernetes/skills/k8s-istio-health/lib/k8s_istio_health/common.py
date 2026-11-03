"""Small helpers shared by the three tasks: bounds, durations, label selectors, ownership, redaction."""

import math
import re
import time
from datetime import timedelta

from k8s_istio_health.errors import RwError

MESSAGE_LIMIT = 1024  # every free-text field in an output
MAX_NESTED = 50  # nested lists in any output
MATCH_CHARS = 8192  # log scanning only looks at this much of a line

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def trunc(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); None stays None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def bound(items, limit):
    """(items cut to `limit`, how many there were, how many were left out)."""
    items = list(items)
    return items[: int(limit)], len(items), max(0, len(items) - int(limit))


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta; anything else raises invalid_input."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError(
            "invalid_input",
            f"not a duration: {str(text)[:64]!r} (use ms, s, m, h, d, w: 90s, 1h30m)",
        )
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))


def since_seconds(text):
    """The `--since` value for kubectl logs (Go durations have no 'd'/'w'): whole seconds, at least 1."""
    return max(1, math.ceil(parse_duration(text).total_seconds()))


def check_int_setting(name, value, low, high):
    """Settings arrive already typed, but the bounds are enforced here too: bad value -> invalid_input."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RwError(
            "invalid_input", f"setting {name} must be an integer {low}..{high}, got {value!r}"
        )
    return value


def selector_string(selector):
    """Label selector (matchLabels + matchExpressions) -> kubectl -l string.

    An empty selector would select every pod in the namespace, so it raises.
    """
    selector = selector or {}
    terms = [f"{k}={v}" for k, v in sorted((selector.get("matchLabels") or {}).items())]
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], ",".join(expr.get("values") or [])
        if op == "In":
            terms.append(f"{key} in ({values})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({values})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            raise RwError("invalid_input", f"unknown selector operator {op!r}")
    if not terms:
        raise RwError("invalid_input", "workload has an empty selector")
    return ",".join(terms)


def matches_selector(labels, selector):
    labels = labels or {}
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def controller_uids(obj):
    return {
        ref.get("uid")
        for ref in (obj.get("metadata", {}).get("ownerReferences") or [])
        if ref.get("controller")
    }


def deployment_pods(deployment, replicasets, pods):
    """Pods that match the Deployment's selector AND hang off it through the owner chain
    (Deployment -> ReplicaSet -> Pod). With `replicasets` None (not readable) the owner
    chain cannot be checked and the selector alone decides."""
    selector = deployment["spec"]["selector"]
    chosen = [p for p in pods if matches_selector(p["metadata"].get("labels"), selector)]
    if replicasets is None:
        return chosen
    uid = deployment["metadata"]["uid"]
    owners = {r["metadata"]["uid"] for r in replicasets if uid in controller_uids(r)}
    return [p for p in chosen if controller_uids(p) & owners]


_VALUE = r"[^\s\"',;}&]+"
_REDACT = [
    # Authorization header or JSON field, with or without a scheme: the value is dropped.
    (
        re.compile(
            r"(?i)(authorization\"?\s*[:=]\s*\"?)(?:(?:bearer|basic|token|digest|negotiate)\s+)?"
            + _VALUE
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\b(bearer)\s+" + _VALUE), r"\1 [REDACTED]"),
    # Cookie and Set-Cookie: a header runs to the end of the line, a JSON field to its closing quote.
    (re.compile(r"(?i)\b((?:set-)?cookie\"?\s*[:=]\s*\")[^\"]*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n\"]+"), r"\1[REDACTED]"),
    # JSON Web Tokens (the signature may be empty).
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*"), "[REDACTED]"),
    # user:password@ in a URL or scheme-less //authority.
    (re.compile(r"//[^/\s:@\"']+:[^/\s@\"']+@"), "//[REDACTED]@"),
    # Secrets carried in a query string.
    (
        re.compile(
            r"(?i)([?&;](?:code|state|sig|signature|x-amz-signature|x-amz-credential|"
            r"x-amz-security-token|access_token|id_token|refresh_token|client_secret|api[_-]?key|"
            r"token|key)=)[^&\s\"']+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(?<![a-z])(password|passwd|secret|token|api[_-]?key|signature)(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), "[REDACTED]"),
]

# The request path of an access-log line loses its query string: `"GET /x?y=1 HTTP/1.1"` and
# JSON `"path":"/x?y=1"`.
_QUERY = [
    re.compile(r'("[A-Z]+ [^"?\s]*)\?[^"\s]*'),
    re.compile(
        r'("(?:path|url|request_path|x_envoy_original_path|original_path)"\s*:\s*"[^"?]*)\?[^"]*'
    ),
]


def strip_query(line):
    for rx in _QUERY:
        line = rx.sub(r"\1", line)
    return line


def redact(line):
    """A cheap scrub of credentials that logs commonly contain: Authorization values, bare
    bearer tokens, cookies, JWTs, user:password@ in URLs, secret query parameters,
    password/secret/token/api-key values, AWS access key ids, PEM private key headers."""
    for rx, repl in _REDACT:
        line = rx.sub(repl, line)
    return line


class Budget:
    """A wall-clock allowance (seconds) for reading logs; `expired()` once it is used up."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.start = time.monotonic()

    def expired(self):
        return time.monotonic() - self.start >= self.seconds


def clip(line):
    """(line redacted and cut to MESSAGE_LIMIT, whether it was cut). Every sample text gets this."""
    cut = len(line) > MESSAGE_LIMIT
    line = redact(strip_query(line[:MATCH_CHARS]))
    if len(line) > MESSAGE_LIMIT:
        return line[: MESSAGE_LIMIT - 3] + "...", True
    return line, cut
