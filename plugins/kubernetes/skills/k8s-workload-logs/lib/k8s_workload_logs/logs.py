"""Pure functions behind the k8s-workload-logs tasks.

Everything here takes already-parsed Kubernetes JSON or log text and returns
plain facts: no kubectl, no clock, no environment. Scripts fetch, call these,
and return.
"""

import re

from k8s_workload_logs.errors import RwError
from k8s_workload_logs.k8s import LIMIT_BYTES

MAX_LINE_CHARS = 1024  # longer log lines are cut to this before they enter any output
SAMPLES = 3  # sample lines kept per category
TRACE_SAMPLE_LINES = 40  # lines of the sample trace
MAX_NESTED = 50  # nested lists in any output
MAX_TRACE_PODS = 10
MAX_TRACE_LINES = 400  # a single trace longer than this is cut where it is read


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


def split_lines(text):
    """Log text -> lines without trailing newline characters (a final empty line is dropped)."""
    return text.splitlines()


def clip(line):
    """(line cut to MAX_LINE_CHARS, whether it was cut). Matching sees whole lines; outputs get this."""
    if len(line) <= MAX_LINE_CHARS:
        return line, False
    return line[: MAX_LINE_CHARS - 3] + "...", True


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
    stream, lines_kept, lines_fetched, truncated, bytes_cut=False, line_truncated=False
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


def bytes_cut(text):
    """True when kubectl logs stopped at --limit-bytes (the newest lines may be missing)."""
    return len(text.encode("utf-8", "replace")) >= LIMIT_BYTES


def recent_logs(fetched, max_lines):
    """[(stream, text)] -> (sections, index). `sections` is [(stream, kept lines)];
    when the streams together exceed `max_lines`, each is cut to its newest lines."""
    split = [(s, split_lines(t), bytes_cut(t)) for s, t in fetched]
    keep = allocate([len(lines) for _s, lines, _b in split], max_lines)
    sections, index = [], []
    for (s, lines, cut), n in zip(split, keep):
        kept = [clip(line) for line in (lines[len(lines) - n :] if n else [])]
        sections.append((s, [text for text, _c in kept]))
        index.append(
            stream_index(s, len(kept), len(lines), n < len(lines), cut, any(c for _t, c in kept))
        )
    return sections, index


def render_section(stream, lines):
    """Artifact text for one stream: a header line, then its lines."""
    kind = "previous" if stream["previous"] else "current"
    init = " init" if stream["init"] else ""
    header = f"==> {stream['pod']}/{stream['container']}{init} [{kind}] {len(lines)} lines"
    return header + "\n" + "".join(line + "\n" for line in lines)


# ----------------------------------------------------- error patterns ----


def compile_patterns(patterns):
    """{category: regex} -> {category: compiled}. Searched case-insensitively.
    An invalid regex raises RwError invalid_input naming the category."""
    out = {}
    for name, rx in patterns.items():
        try:
            out[name] = re.compile(rx, re.IGNORECASE)
        except re.error as exc:
            raise RwError(
                "invalid_input", f"pattern {name!r} is not a valid regex: {exc}"
            ) from None
    return out


def error_patterns(streams_lines, patterns, ignore):
    """Count lines per (stream, category).

    `streams_lines` is [(stream, lines)]. A line matching any `ignore` regex
    (e.g. `errors: 0`) is counted nowhere. A line matching several categories
    counts in each. Returns (rows, ignored) with one row per stream and
    category that matched: count and up to SAMPLES sample lines (first seen).
    """
    pats = compile_patterns(patterns)
    skips = list(compile_patterns({f"ignore[{i}]": rx for i, rx in enumerate(ignore)}).values())
    rows, ignored = [], 0
    for stream, lines in streams_lines:
        counts, samples, cut = {}, {}, {}
        for line in lines:
            if any(s.search(line) for s in skips):
                ignored += 1
                continue
            for name, rx in pats.items():
                if rx.search(line):
                    counts[name] = counts.get(name, 0) + 1
                    if len(samples.setdefault(name, [])) < SAMPLES:
                        text, was_cut = clip(line)
                        samples[name].append(text)
                        cut[name] = cut.get(name, False) or was_cut
        for name in pats:
            if name in counts:
                rows.append(
                    {
                        "pod": stream["pod"],
                        "container": stream["container"],
                        "previous": stream["previous"],
                        "category": name,
                        "count": counts[name],
                        "samples": samples[name],
                        "truncated": cut[name],
                    }
                )
    rows.sort(key=lambda r: (-r["count"], r["pod"], r["container"], r["category"]))
    return rows, ignored


# ---------------------------------------------------------- stacktraces ----

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
_DOTNET_FRAME = re.compile(r"^\s+at [^\s].*\(.*\)(?: in .+:line \d+)?\s*$")
_JAVA_OR_DOTNET_FRAME = re.compile(_JAVA_FRAME.pattern + "|" + _DOTNET_FRAME.pattern)
_DOTNET_EVIDENCE = re.compile(r" in .+:line \d+\s*$")
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


def find_traces(lines):
    """Stack traces in a log as (language, exception, first line, lines) tuples.

    Python (`Traceback (most recent call last):`), Java (exception line plus
    `at` frames), Go (`panic:`/`fatal error:` plus a goroutine dump), Node.js
    (`SomethingError` plus `at file:line:col` frames) and .NET (exception line
    plus `at ... in file:line N` frames). Traces are recognised when the frames
    are on their own lines; a logger that prefixes every line hides them.
    """
    found, i = [], 0
    while i < len(lines):
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
    return found


_NUM = re.compile(r"\d+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _group_key(language, exception, first):
    text = _NUM.sub("#", _UUID.sub("<uuid>", first.strip().lower()))
    return language, exception, text


def stacktraces(streams_lines):
    """Group the traces in [(stream, lines)] by language, exception type and first
    line (digit runs and UUIDs ignored). Returns the groups, most frequent first:
    count, the pods and containers they came from, whether any came from a
    `--previous` log, and one sample trace cut to TRACE_SAMPLE_LINES lines."""
    groups = {}
    for stream, lines in streams_lines:
        for language, exception, first, block in find_traces(lines):
            key = _group_key(language, exception, first)
            if key not in groups:
                head, head_cut = clip(first)
                sample = [clip(b) for b in block[:TRACE_SAMPLE_LINES]]
                groups[key] = {
                    "language": language,
                    "exception": clip(exception)[0],
                    "firstLine": head,
                    "count": 0,
                    "pods": [],
                    "containers": [],
                    "inPrevious": False,
                    "sample": [t for t, _c in sample],
                    "sampleTruncated": head_cut
                    or len(block) > TRACE_SAMPLE_LINES
                    or any(c for _t, c in sample),
                }
            g = groups[key]
            g["count"] += 1
            if stream["pod"] not in g["pods"] and len(g["pods"]) < MAX_TRACE_PODS:
                g["pods"].append(stream["pod"])
            if stream["container"] not in g["containers"] and len(g["containers"]) < MAX_NESTED:
                g["containers"].append(stream["container"])
            g["inPrevious"] = g["inPrevious"] or stream["previous"]
    out = sorted(groups.values(), key=lambda g: (-g["count"], g["language"], g["exception"]))
    return out


# ------------------------------------------------------------- shared ----


def scanned_index(fetched, scan_lines):
    """Stream index for the analysis tasks from [(stream, text)]: truncated when the
    stream reached `scan_lines` (older lines in the window were not read)."""
    out = []
    for stream, text in fetched:
        lines = split_lines(text)
        long = any(len(line) > MAX_LINE_CHARS for line in lines)
        out.append(
            stream_index(
                stream, len(lines), len(lines), len(lines) >= scan_lines, bytes_cut(text), long
            )
        )
    return out


PREVIOUS_GONE = "previous container log no longer exists"
LOG_GONE = "container log file no longer exists on the node"


def unavailable_with_gone(plan, gone):
    """The plan's unavailable containers plus the `--previous` streams whose log was gone."""
    extra = [
        {k: s[k] for k in ("pod", "container", "init", "previous")}
        | {"reason": PREVIOUS_GONE if s["previous"] else LOG_GONE}
        for s in gone
    ]
    return plan["unavailable"] + extra


def inspected(plan):
    return {
        "pods": plan["pods"],
        "podsRead": plan["podsSelected"],
        "containersExcluded": plan["excluded"][:MAX_NESTED],
    }
