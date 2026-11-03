"""Pure functions behind the k8s-workload-logs tasks.

Everything here takes already-parsed Kubernetes JSON or log text and returns
plain facts: no kubectl, no environment. Scripts fetch, call these, and return.
Stack traces are in traces.py.
"""

import re
import time

from k8s_workload_logs.errors import RwError
from k8s_workload_logs.k8s import LIMIT_BYTES

MAX_LINE_CHARS = 1024  # longer log lines are cut to this before they enter any output
MATCH_CHARS = 8192  # patterns and trace finders only look at this much of a line
SAMPLES = 3  # sample lines kept per category
MAX_NESTED = 50  # nested lists in any output
MAX_PATTERNS = 20  # entries in the patterns and ignore settings
MAX_PATTERN_CHARS = 200  # length of one pattern
CHECK_EVERY = 256  # lines between wall-clock budget checks

PREVIOUS_GONE = "previous container log no longer exists"
LOG_GONE = "container log file no longer exists on the node"
SKIPPED_BUDGET = "not read: scanBudgetSeconds exhausted"
SKIPPED_BYTES = "not read: maxBytesTotal reached"


class Budget:
    """A wall-clock budget shared by everything a task does after it starts."""

    def __init__(self, seconds, clock=None):
        self.seconds, self.clock = seconds, clock or time.monotonic
        self.deadline = self.clock() + seconds

    def expired(self):
        return self.clock() >= self.deadline


def bound(items, limit):
    """(items cut to `limit`, how many there were, how many were left out)."""
    items = list(items)
    return items[: int(limit)], len(items), max(len(items) - int(limit), 0)


# ------------------------------------------------------------ planning ----


def _statuses(pod, init):
    key = "initContainerStatuses" if init else "containerStatuses"
    return {s["name"]: s for s in (pod.get("status", {}).get(key) or [])}


def _restart_total(pod):
    both = list(_statuses(pod, False).values()) + list(_statuses(pod, True).values())
    return sum(s.get("restartCount", 0) for s in both)


def _ready(pod):
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions") or []
    )


def order_pods(pods):
    """Pods most worth reading first: most restarts, then not ready, then name."""
    return sorted(pods, key=lambda p: (-_restart_total(p), _ready(p), p["metadata"]["name"]))


def plan_streams(pods, only=None, exclude=(), max_pods=20):
    """Which log streams to read.

    A stream is one container's current log or its `--previous` log. Returns a
    dict: `streams` (pod, container, init, previous), `unavailable` (containers
    whose log cannot be read right now, with why), `excluded` (container names
    skipped through `exclude`), `pods` (owned pods) and `podsSelected` (read;
    the rest are over `max_pods`).
    """
    names = {
        c["name"]
        for p in pods
        for k in ("initContainers", "containers")
        for c in p.get("spec", {}).get(k) or []
    }
    if only and pods and only not in names:
        raise RwError(
            "invalid_input",
            f"no container named {only[:64]!r}; pods have: {', '.join(sorted(names)[:MAX_NESTED])}",
        )
    ordered = order_pods(pods)
    selected = ordered[: int(max_pods)]
    streams, unavailable, excluded = [], [], set()
    for pod in selected:
        pname = pod["metadata"]["name"]
        for init in (True, False):
            spec_key = "initContainers" if init else "containers"
            statuses = _statuses(pod, init)
            for c in pod.get("spec", {}).get(spec_key) or []:
                name = c["name"]
                if only and name != only:
                    continue
                if name in exclude:
                    excluded.add(name)
                    continue
                status = statuses.get(name)
                base = {"pod": pname, "container": name, "init": init}
                if status is None:
                    unavailable.append(
                        {
                            **base,
                            "previous": False,
                            "reason": f"no container status yet (pod phase {pod.get('status', {}).get('phase')})",
                        }
                    )
                    continue
                state = status.get("state") or {}
                if "waiting" in state:
                    reason = state["waiting"].get("reason") or "Waiting"
                    unavailable.append(
                        {**base, "previous": False, "reason": f"container is waiting: {reason}"}
                    )
                else:
                    streams.append({**base, "previous": False})
                if status.get("restartCount", 0) > 0 and (status.get("lastState") or {}).get(
                    "terminated"
                ):
                    streams.append({**base, "previous": True})
    return {
        "streams": streams,
        "unavailable": unavailable,
        "excluded": sorted(excluded),
        "pods": len(pods),
        "podsSelected": len(selected),
    }


# --------------------------------------------------------------- lines ----


# --------------------------------------------------------------- lines ----


def split_lines(text):
    """Log text -> lines (split on \\n only, a trailing \\r dropped, each cut to MATCH_CHARS;
    a final empty line is dropped)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.rstrip("\r")[:MATCH_CHARS] for line in lines]


_REDACT = [
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[^\s\"',;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(?<![a-z])(password|passwd|secret|token|api[_-]?key)(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), "[REDACTED]"),
]


def redact(line):
    """A cheap scrub of credentials that logs commonly contain: bearer tokens,
    password/secret/token/api-key values, AWS access key ids, PEM private key headers."""
    for rx, repl in _REDACT:
        line = rx.sub(repl, line)
    return line


def clip(line):
    """(line redacted and cut to MAX_LINE_CHARS, whether it was cut). Matching sees
    the line as read; every output text gets this."""
    cut = len(line) > MAX_LINE_CHARS
    line = redact(line[:MATCH_CHARS])
    if len(line) > MAX_LINE_CHARS:
        return line[: MAX_LINE_CHARS - 3] + "...", True
    return line, cut


def allocate(sizes, budget):
    """Split `budget` lines over streams of the given sizes: every stream gets
    its size if it fits, else an equal share of what is left (small streams
    first, so what they do not use goes to the big ones)."""
    out = [0] * len(sizes)
    remaining = max(int(budget), 0)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for pos, i in enumerate(order):
        share = remaining // (len(order) - pos)
        out[i] = min(sizes[i], share)
        remaining -= out[i]
    return out


def stream_index(
    stream,
    lines_kept,
    lines_fetched,
    truncated,
    bytes_cut=False,
    line_truncated=False,
):
    return {
        "pod": stream["pod"],
        "container": stream["container"],
        "init": stream["init"],
        "previous": stream["previous"],
        "lines": lines_kept,
        "linesFetched": lines_fetched,
        "truncated": truncated,
        "bytesCut": bytes_cut,
        "lineTruncated": line_truncated,
    }


def bytes_cut(size):
    """True when kubectl logs stopped at --limit-bytes (the newest lines are missing)."""
    return size >= LIMIT_BYTES


def scanned_entry(stream, lines, size, scan_lines):
    """Stream index entry for the analysis tasks: truncated when the stream reached
    `scan_lines` (older lines in the window were not read)."""
    long = any(len(line) > MAX_LINE_CHARS for line in lines)
    return stream_index(
        stream, len(lines), len(lines), len(lines) >= scan_lines, bytes_cut(size), long
    )


def reduce_stream(stream, text, size):
    """One stream for recent-logs: its lines redacted and clipped, so the raw text can be dropped."""
    lines = split_lines(text)
    clipped = [clip(line) for line in lines]
    return {
        "stream": stream,
        "lines": [t for t, _c in clipped],
        "fetched": len(lines),
        "bytesCut": bytes_cut(size),
        "lineTruncated": any(c for _t, c in clipped),
    }


def recent_logs(reduced, max_lines):
    """[reduce_stream results] -> (sections, index). `sections` is [(stream, kept lines)];
    when the streams together exceed `max_lines`, each is cut to its newest lines."""
    keep = allocate([len(r["lines"]) for r in reduced], max_lines)
    sections, index = [], []
    for r, n in zip(reduced, keep):
        kept = r["lines"][len(r["lines"]) - n :] if n else []
        sections.append((r["stream"], kept))
        index.append(
            stream_index(
                r["stream"],
                len(kept),
                r["fetched"],
                n < r["fetched"],
                r["bytesCut"],
                r["lineTruncated"],
            )
        )
    return sections, index


def render_section(stream, lines):
    """Artifact text for one stream: a header line, then its lines."""
    kind = "previous" if stream["previous"] else "current"
    init = " init" if stream["init"] else ""
    header = f"==> {stream['pod']}/{stream['container']}{init} [{kind}] {len(lines)} lines"
    return header + "\n" + "".join(line + "\n" for line in lines)


# ----------------------------------------------------- error patterns ----


def compile_patterns(patterns, what="pattern"):
    """{name: regex} -> {name: compiled}. Searched case-insensitively. More than
    MAX_PATTERNS entries, a pattern over MAX_PATTERN_CHARS or an invalid regex
    raises RwError invalid_input naming the entry."""
    if not isinstance(patterns, dict) or not all(isinstance(v, str) for v in patterns.values()):
        raise RwError("invalid_input", f"{what}s must be an object of name to regular expression")
    if len(patterns) > MAX_PATTERNS:
        raise RwError(
            "invalid_input", f"at most {MAX_PATTERNS} {what}s are allowed, got {len(patterns)}"
        )
    out = {}
    for name, rx in patterns.items():
        if len(rx) > MAX_PATTERN_CHARS:
            raise RwError(
                "invalid_input", f"{what} {name!r} is longer than {MAX_PATTERN_CHARS} characters"
            )
        try:
            out[name] = re.compile(rx, re.IGNORECASE)
        except re.error as exc:
            raise RwError("invalid_input", f"{what} {name!r} is not a valid regex: {exc}") from None
    return out


def compile_ignore(ignore):
    if not isinstance(ignore, (list, tuple)):
        raise RwError("invalid_input", "ignore must be a list of regular expressions")
    return list(
        compile_patterns(
            {f"ignore[{i}]": rx for i, rx in enumerate(ignore)}, "ignore pattern"
        ).values()
    )


class PatternScanner:
    """Counts lines per (stream, category), one stream at a time.

    A line matching any `ignore` regex (e.g. `error: 0`) is counted nowhere. A
    line matching several categories counts in each. Each stream yields one row
    per category that matched: count and up to SAMPLES sample lines (first seen,
    redacted and clipped). Patterns see at most MATCH_CHARS of a line.
    """

    def __init__(self, patterns, ignore):
        self.pats = compile_patterns(patterns)
        self.skips = compile_ignore(ignore)
        self.ignored = 0

    def scan(self, stream, lines, budget=None):
        """(rows, complete); complete is False when `budget` ran out mid-stream (counts are partial)."""
        counts, samples, cut, complete = {}, {}, {}, True
        for n, line in enumerate(lines):
            if budget is not None and n % CHECK_EVERY == 0 and budget.expired():
                complete = False
                break
            if any(s.search(line) for s in self.skips):
                self.ignored += 1
                continue
            for name, rx in self.pats.items():
                if rx.search(line):
                    counts[name] = counts.get(name, 0) + 1
                    if len(samples.setdefault(name, [])) < SAMPLES:
                        text, was_cut = clip(line)
                        samples[name].append(text)
                        cut[name] = cut.get(name, False) or was_cut
        rows = [
            {
                "pod": stream["pod"],
                "container": stream["container"],
                "previous": stream["previous"],
                "category": name,
                "count": counts[name],
                "samples": samples[name],
                "truncated": cut[name],
            }
            for name in self.pats
            if name in counts
        ]
        return rows, complete


def sort_rows(rows):
    return sorted(rows, key=lambda r: (-r["count"], r["pod"], r["container"], r["category"]))


def error_patterns(streams_lines, patterns, ignore, budget=None):
    """Convenience over PatternScanner: (rows most frequent first, lines ignored)."""
    scanner = PatternScanner(patterns, ignore)
    rows = []
    for stream, lines in streams_lines:
        rows += scanner.scan(stream, lines, budget)[0]
    return sort_rows(rows), scanner.ignored


# ------------------------------------------------------------- shared ----


def unavailable_extra(streams, reason_for):
    return [
        {k: s[k] for k in ("pod", "container", "init", "previous")} | {"reason": reason_for(s)}
        for s in streams
    ]


def inspected(plan, **extra):
    return {
        "pods": plan["pods"],
        "podsRead": plan["podsSelected"],
        "containersExcluded": plan["excluded"][:MAX_NESTED],
        **extra,
    }
