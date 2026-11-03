"""A cheap scrub of credentials that command output commonly contains. Not complete:
the SKILL tells the agent never to repeat credentials it sees. The pattern set is the
one k8s-workload-logs applies, kept here so the skill has no import across skills,
plus whole PEM private-key blocks and a bare `Bearer <token>`."""

import re

REDACTED = "[REDACTED]"

_PATTERNS = [
    (
        re.compile(r"(?i)(authorization[ \t]*[:=][ \t]*(?:bearer|basic|token)[ \t]+)[^\s\"',;]+"),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer[ \t]+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
    (
        re.compile(
            r"(?i)(?<![a-z])(password|passwd|secret|token|api[_-]?key)(\"?[ \t]*[:=][ \t]*\"?)[^\s\"',;&]+"
        ),
        r"\1\2" + REDACTED,
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (
        # the whole block; a block cut off before its END line goes to the end of the text
        re.compile(
            r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        REDACTED,
    ),
]


def redact_counted(text):
    """(text with credentials replaced by [REDACTED], how many replacements)."""
    total = 0
    for rx, repl in _PATTERNS:
        text, n = rx.subn(repl, text)
        total += n
    return text, total


def redact_text(text):
    return redact_counted(text)[0]
