"""A small parser for the Prometheus / OpenMetrics text exposition format (pure).

Only samples are returned: `# HELP`, `# TYPE`, `# UNIT` and `# EOF` lines are skipped. A line that
is not a sample is an `internal` RwError naming its number: a metrics page this code does not
understand must not read as "no problems".
"""

import math
import re
from collections import namedtuple

from k8s_otelcollector_health.errors import RwError

Sample = namedtuple("Sample", "name labels value")

MAX_BYTES = 20_000_000
_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r"\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*\"")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


def _bad(number, line, why):
    raise RwError("internal", f"metrics line {number} is not a sample ({why}): {line[:80]!r}")


def _label_value(text, i, number, line):
    """(value, index after the closing quote) of a label value starting after its opening quote."""
    out = []
    while i < len(text):
        c = text[i]
        if c == "\\":
            if i + 1 >= len(text) or text[i + 1] not in _ESCAPES:
                _bad(number, line, "bad escape in a label value")
            out.append(_ESCAPES[text[i + 1]])
            i += 2
        elif c == '"':
            return "".join(out), i + 1
        else:
            out.append(c)
            i += 1
    _bad(number, line, "unterminated label value")


def _labels(text, i, number, line):
    """(labels, index after the closing brace) of a `{a="b",c="d"}` block starting at `i` (after `{`)."""
    labels = {}
    while True:
        while i < len(text) and text[i] in " \t,":
            i += 1
        if i >= len(text):
            _bad(number, line, "unterminated label set")
        if text[i] == "}":
            return labels, i + 1
        m = _LABEL.match(text, i)
        if not m:
            _bad(number, line, "bad label name")
        value, i = _label_value(text, m.end(), number, line)
        labels[m.group(1)] = value


def _value(token, number, line):
    try:
        return float(token)
    except ValueError:
        _bad(number, line, f"value {token!r} is not a number")


def parse(text):
    """The samples of a text exposition page, in order."""
    if len(text) > MAX_BYTES:
        raise RwError("internal", f"metrics page is larger than {MAX_BYTES} bytes")
    samples = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAME.match(line)
        if not m:
            _bad(number, line, "no metric name")
        name, i, labels = m.group(0), m.end(), {}
        if i < len(line) and line[i] == "{":
            labels, i = _labels(line, i + 1, number, line)
        rest = line[i:]
        if " # " in rest:  # an OpenMetrics exemplar follows the sample
            rest = rest.split(" # ", 1)[0]
        fields = rest.split()
        if not 1 <= len(fields) <= 2:
            _bad(number, line, "expected a value and an optional timestamp")
        if len(fields) == 2:
            _value(fields[1], number, line)
        value = _value(fields[0], number, line)
        samples.append(Sample(name, labels, value if math.isfinite(value) else None))
    return samples
