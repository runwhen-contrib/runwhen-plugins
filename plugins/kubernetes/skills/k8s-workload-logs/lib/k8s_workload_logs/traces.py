"""Stack trace recognition and grouping (pure functions over log lines)."""

import re

from k8s_workload_logs.logs import CHECK_EVERY, MAX_NESTED, clip

TRACE_SAMPLE_LINES = 40  # lines of a sample trace
MAX_TRACE_PODS = 10
MAX_TRACE_LINES = 400  # a single trace longer than this is cut where it is read
MAX_GROUPS = 500  # distinct traces tracked; later new ones are counted in `ungrouped` only
MAX_SAMPLES = 50  # groups that carry a sample (most frequent first): 50 x 40 = 2000 sample lines

_PY_HEADER = re.compile(r"Traceback \(most recent call last\):\s*$")
_PY_EXC = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
_JAVA_HEADER = re.compile(
    r"(?:^|[\s:\]])((?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*(?:Exception|Error|Throwable))(?::.*)?$"
)
_JAVA_FRAME = re.compile(r"^\s+at [\w$.<>/]+\([^)]*\)\s*$")
_JAVA_EVIDENCE = re.compile(
    r"\((?:[\w$]+\.(?:java|kt|scala|groovy):\d+|Native Method|Unknown Source)\)"
)
_JAVA_TRAILER = re.compile(
    r"^(?:Caused by: |Suppressed: |\s+\.\.\. \d+ (?:more|common frames omitted))"
)
_NODE_HEADER = re.compile(r"^(?:Uncaught\s+)?([A-Za-z_$][\w$]*(?:Error|Exception))(?::.*)?$")
_NODE_FRAME = re.compile(r"^\s+at .*:\d+:\d+\)?\s*$")
_DOTNET_HEADER = re.compile(
    r"(?:^|\s)(?:Unhandled exception\.\s+)?((?:[A-Z][\w]*\.)+[A-Z][\w]*Exception)(?::.*)?$"
)
# linear: no nested quantifiers, the parameter list cannot contain parentheses
_DOTNET_FRAME = re.compile(r"^\s+at \S[^()]*\([^()]*\)(?: in \S.*:line \d+)?\s*$")
_JAVA_OR_DOTNET_FRAME = re.compile(_JAVA_FRAME.pattern + "|" + _DOTNET_FRAME.pattern)
_DOTNET_EVIDENCE = re.compile(r" in \S.*:line \d+\s*$")
_GO_HEADER = re.compile(r"^(panic|fatal error): (.*)$")
_GO_GOROUTINE = re.compile(r"^goroutine \d+ \[[^\]]*\]:")
_GO_CALL = re.compile(r"^[^\s(]+\(.*\)$")


def _is_go_body(line):
    return (
        not line.strip()
        or line.startswith(("\t", " "))
        or bool(_GO_GOROUTINE.match(line))
        or line.startswith(("created by ", "exit status", "..."))
        or bool(_GO_CALL.match(line))
    )


def _python(lines, i):
    """Traceback at lines[i]: the header, indented frames, then the exception line."""
    j = i + 1
    while (
        j < len(lines)
        and j - i < MAX_TRACE_LINES
        and (lines[j].startswith((" ", "\t")) or not lines[j].strip())
    ):
        j += 1
    block = lines[i:j]
    exc = _PY_EXC.match(lines[j]) if j < len(lines) else None
    if exc:
        block = lines[i : j + 1]
        j += 1
        return j, "python", exc.group(1), lines[j - 1], block
    return j, "python", "(unknown)", lines[i], block


def _frames(lines, i, frame, trailer=None):
    """Index after the frame (and trailer) lines starting at lines[i]."""
    j = i
    while (
        j < len(lines)
        and j - i < MAX_TRACE_LINES
        and (frame.match(lines[j]) or (trailer and trailer.match(lines[j])))
    ):
        j += 1
    return j


def _java_like(lines, i):
    """Java or .NET trace whose header is lines[i], or None (frames decide the language)."""
    if not _JAVA_FRAME.match(
        lines[i + 1] if i + 1 < len(lines) else ""
    ) and not _DOTNET_FRAME.match(lines[i + 1] if i + 1 < len(lines) else ""):
        return None
    j = _frames(lines, i + 1, _JAVA_OR_DOTNET_FRAME, _JAVA_TRAILER)
    block = lines[i:j]
    text = "\n".join(block)
    if _JAVA_EVIDENCE.search(text):
        m = _JAVA_HEADER.search(lines[i])
        return j, "java", (m.group(1) if m else "(unknown)"), lines[i], block
    m = _DOTNET_HEADER.search(lines[i])
    if m and (
        _DOTNET_EVIDENCE.search(text)
        or "Unhandled exception." in lines[i]
        or m.group(1).startswith(("System.", "Microsoft."))
    ):
        return j, "dotnet", m.group(1), lines[i], block
    m = _JAVA_HEADER.search(lines[i])
    if m:
        return j, "java", m.group(1), lines[i], block
    return None


def _node(lines, i):
    m = _NODE_HEADER.match(lines[i])
    if not m or i + 1 >= len(lines) or not _NODE_FRAME.match(lines[i + 1]):
        return None
    j = _frames(lines, i + 1, _NODE_FRAME)
    return j, "node", m.group(1), lines[i], lines[i:j]


def _go(lines, i):
    m = _GO_HEADER.match(lines[i])
    if not m:
        return None
    j = i + 1
    while j < len(lines) and j - i < MAX_TRACE_LINES and _is_go_body(lines[j]):
        j += 1
    block = lines[i:j]
    while block and not block[-1].strip():
        block.pop()
    if not any(_GO_GOROUTINE.match(b) for b in block):
        return None
    return j, "go", m.group(1), lines[i], block


def find_traces(lines, budget=None):
    """Stack traces in a log as (language, exception, first line, lines) tuples.

    Python (`Traceback (most recent call last):`), Java (exception line plus
    `at` frames), Go (`panic:`/`fatal error:` plus a goroutine dump), Node.js
    (`SomethingError` plus `at file:line:col` frames) and .NET (exception line
    plus `at ... in file:line N` frames). Traces are recognised when the frames
    are on their own lines; a logger that prefixes every line, or JSON-embedded
    traces (zap, pino, logrus `"stacktrace"` fields), are not recognised.
    Stops early when `budget` is exhausted: returns (traces, complete).
    """
    found, i = [], 0
    while i < len(lines):
        if budget is not None and i % CHECK_EVERY == 0 and budget.expired():
            return found, False
        line = lines[i]
        hit = None
        if _PY_HEADER.search(line):
            hit = _python(lines, i)
        elif _GO_HEADER.match(line):
            hit = _go(lines, i)
        if hit is None and (_JAVA_HEADER.search(line) or _DOTNET_HEADER.search(line)):
            hit = _java_like(lines, i)
        if hit is None:
            hit = _node(lines, i)
        if hit is None:
            i += 1
            continue
        end, language, exception, first, block = hit
        found.append((language, exception, first, block))
        i = max(end, i + 1)
    return found, True


_NUM = re.compile(r"\d+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b")


def _group_key(language, exception, first):
    text = _NUM.sub("#", _HEX.sub("<hex>", _UUID.sub("<uuid>", first.strip().lower())))
    return language, exception, text


class TraceCollector:
    """Groups traces by language, exception type and first line (digit runs, UUIDs
    and hex ids of 8+ characters ignored) as streams are added, so a stream's
    text can be dropped before the next is read."""

    def __init__(self):
        self.groups = {}
        self.ungrouped = 0

    def add(self, stream, lines, budget=None):
        """Add the traces of one stream; False when `budget` ran out before the end."""
        found, complete = find_traces(lines, budget)
        for language, exception, first, block in found:
            key = _group_key(language, exception, first)
            g = self.groups.get(key)
            if g is None:
                if len(self.groups) >= MAX_GROUPS:
                    self.ungrouped += 1
                    continue
                head, head_cut = clip(first)
                clipped = [clip(b) for b in block[:TRACE_SAMPLE_LINES]]  # a kept sample is small
                g = self.groups[key] = {
                    "language": language,
                    "exception": clip(exception)[0],
                    "firstLine": head,
                    "count": 0,
                    "pods": [],
                    "containers": [],
                    "inPrevious": False,
                    "_sample": [t for t, _c in clipped],
                    "_cut": head_cut
                    or len(block) > TRACE_SAMPLE_LINES
                    or any(c for _t, c in clipped),
                }
            g["count"] += 1
            if stream["pod"] not in g["pods"] and len(g["pods"]) < MAX_TRACE_PODS:
                g["pods"].append(stream["pod"])
            if stream["container"] not in g["containers"] and len(g["containers"]) < MAX_NESTED:
                g["containers"].append(stream["container"])
            g["inPrevious"] = g["inPrevious"] or stream["previous"]
        return complete

    def result(self):
        """Groups, most frequent first. Only the first MAX_SAMPLES carry a sample
        (already clipped and redacted); the rest have an empty one and sampleTruncated true."""
        ordered = sorted(
            self.groups.values(), key=lambda g: (-g["count"], g["language"], g["exception"])
        )
        out = []
        for n, g in enumerate(ordered):
            sample, cut = (g["_sample"], g["_cut"]) if n < MAX_SAMPLES else ([], True)
            out.append(
                {
                    **{k: v for k, v in g.items() if not k.startswith("_")},
                    "sample": sample,
                    "sampleTruncated": cut,
                }
            )
        return out


def stacktraces(streams_lines, budget=None):
    """Convenience: group the traces of [(stream, lines)]; returns the groups."""
    collector = TraceCollector()
    for stream, lines in streams_lines:
        collector.add(stream, lines, budget)
    return collector.result()
