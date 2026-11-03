"""proxy-errors: count error and warning lines and failed requests in istio-proxy logs."""

import re

from k8s_istio_health.common import MATCH_CHARS, MAX_NESTED, clip

SAMPLES = 3  # sample lines kept per category per pod
LIMIT_BYTES = 4 * 1024 * 1024  # `kubectl logs --limit-bytes`

# Istio's own log format: `2026-11-03T20:00:00.000000Z<TAB>warning<TAB>envoy config<TAB>...`;
# envoy's native format: `[2026-11-03 20:00:00.000][14][warning][config] ...`; JSON logs: "level":"error".
_LEVELS = (
    re.compile(r"^\S+\s+(?P<level>error|critical|warn|warning)\s", re.I),
    re.compile(r"\]\[(?P<level>error|critical|warn|warning)\]\[", re.I),
    re.compile(r'"(?:level|severity)"\s*:\s*"(?P<level>error|critical|warn|warning)"', re.I),
)
# Envoy access log, text format: `... "GET /x HTTP/1.1" 503 UF details ...` (flags follow the code).
_ACCESS_TEXT = re.compile(
    r'"[^"]*"\s+(?P<code>\d{1,3})\s+(?P<flags>-|[A-Z]{2,5}(?:,[A-Z]{2,5})*)(?:\s|$)'
)
_ACCESS_JSON_CODE = re.compile(r'"response_code"\s*:\s*"?(?P<code>\d{1,3})"?')
_ACCESS_JSON_FLAGS = re.compile(r'"response_flags"\s*:\s*"(?P<flags>[^"]*)"')


def classify_level(line):
    """'error' (error, critical), 'warn' (warn, warning) or None for the log level of a line."""
    for rx in _LEVELS:
        m = rx.search(line)
        if m:
            return "error" if m.group("level").lower() in ("error", "critical") else "warn"
    return None


def parse_access(line):
    """(status code, [response flags]) of an access log line, or None when it is not one."""
    m = _ACCESS_TEXT.search(line)
    if m:
        flags = m.group("flags")
        return int(m.group("code")), [] if flags == "-" else flags.split(",")
    m = _ACCESS_JSON_CODE.search(line)
    if m:
        f = _ACCESS_JSON_FLAGS.search(line)
        flags = (f.group("flags") if f else "") or "-"
        return int(m.group("code")), [] if flags == "-" else flags.split(",")
    return None


def split_lines(text):
    """Log text -> lines (split on \\n, trailing \\r dropped, each cut to MATCH_CHARS)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.rstrip("\r")[:MATCH_CHARS] for line in lines]


def scan_pod(pod, container, native, lines, size, scan_lines):
    """Counts and samples for one pod's istio-proxy log."""
    errors = warns = access = server_errors = 0
    flags = {}
    samples = []
    taken = {}

    def sample(category, line):
        if taken.get(category, 0) < SAMPLES:
            taken[category] = taken.get(category, 0) + 1
            text, cut = clip(line)
            samples.append({"category": category, "line": text, "truncated": cut})

    for line in lines:
        parsed = parse_access(line)
        if parsed:
            access += 1
            code, fl = parsed
            for f in fl:
                flags[f] = flags.get(f, 0) + 1
            if 500 <= code <= 599:
                server_errors += 1
                sample("http5xx", line)
            elif fl:
                sample("flagged", line)
            continue
        level = classify_level(line)
        if level == "error":
            errors += 1
            sample("error", line)
        elif level == "warn":
            warns += 1
            sample("warn", line)
    ordered = sorted(flags.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "pod": pod,
        "container": container,
        "native": native,
        "linesRead": len(lines),
        "truncated": len(lines) >= scan_lines,
        "bytesCut": size >= LIMIT_BYTES,
        "errorLines": errors,
        "warnLines": warns,
        "accessLines": access,
        "status5xx": server_errors,
        "responseFlags": [{"flag": f, "count": n} for f, n in ordered[:MAX_NESTED]],
        "samples": samples,
    }


def order_pods(pods):
    """Pods with the most restarts first, then not ready, then name (reading is capped by maxPods)."""

    def key(p):
        statuses = (p.get("status") or {}).get("containerStatuses") or []
        restarts = sum(s.get("restartCount", 0) for s in statuses)
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (p.get("status") or {}).get("conditions") or []
        )
        return (-restarts, ready, p["metadata"]["name"])

    return sorted(pods, key=key)


def log_file_missing(text):
    """kubectl exits 0 and prints this single line as the log when the node no longer has the file."""
    t = text.strip()
    return t.startswith("unable to retrieve container logs for ") and "\n" not in t


def sort_results(rows):
    """Pods with the most failed requests, errors, then warnings first."""
    return sorted(
        rows, key=lambda r: (-r["status5xx"], -r["errorLines"], -r["warnLines"], r["pod"])
    )
