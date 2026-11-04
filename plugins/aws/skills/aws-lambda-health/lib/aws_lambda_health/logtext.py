"""Log and error text that enters an output: scrubbed of credentials and cut to 1024 characters.

The same rules as the kubernetes log skills (k8s-workload-logs `logs.redact` / `logs.clip`), plus
the AWS credential shapes a Lambda log line can hold: temporary access key ids (ASIA...), secret
access keys and session tokens named in the line. The scrub is cheap and not complete."""

import re

MAX_TEXT_CHARS = 1024  # any text field is cut to this before it enters an output
MATCH_CHARS = 8192  # the scrub only reads this much of a text

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
    (
        re.compile(
            r"(?i)((?:aws_)?(?:secret_access_key|session_token|security_token)\"?\s*[:=]\s*\"?)[^\s\"',;&]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), "[REDACTED]"),
]


def redact(text):
    """A cheap scrub of credentials that logs commonly contain: bearer tokens, password / secret /
    token / api-key values, AWS secret keys and session tokens, AWS access key ids (AKIA, ASIA),
    PEM private key headers."""
    for rx, repl in _REDACT:
        text = rx.sub(repl, text)
    return text


def clip(text):
    """(text redacted and cut to MAX_TEXT_CHARS, whether it was cut)."""
    text = str(text)
    cut = len(text) > MAX_TEXT_CHARS
    text = redact(text[:MATCH_CHARS])
    if len(text) > MAX_TEXT_CHARS:
        return text[: MAX_TEXT_CHARS - 3] + "...", True
    return text, cut
