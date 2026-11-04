"""Log and error text that enters an output: scrubbed of credentials and cut to 1024 characters.

The same rules as the kubernetes log skills (k8s-workload-logs `logs.redact` / `logs.clip`), plus
the AWS credential shapes a Lambda log line can hold: temporary access key ids (ASIA...), secret
access keys and session tokens named in the line, SigV4 signatures, JWTs, connection strings with a
password and a printed Python `os.environ`. Keys and values may be quoted with ' or " (a JSON line,
a Python dict repr). The scrub is cheap and not complete."""

import re

MAX_TEXT_CHARS = 1024  # any text field is cut to this before it enters an output
MATCH_CHARS = 8192  # the scrub only reads this much of a text

_Q = r"""['"]?"""  # an optional quote around a key or a value
_SECRET_KEY = r"[A-Za-z0-9/+=]{40}"  # the shape of an AWS secret access key (40 base64 characters)
_KEY_START = r"(?<![A-Za-z0-9/+])"
_KEY_END = r"(?![A-Za-z0-9/+=])"
# `key`, an optional quote, a separator, an optional quote: what stays when the value is replaced
_ASSIGN = _Q + r"\s*[:=]\s*" + _Q
_VALUE = r"[^\s\"',;&]+"

_REDACT = [
    # print(os.environ): the whole mapping, to the closing "})" (or the end of the line when the
    # line was cut). First, so no other rule leaves half of it behind.
    (re.compile(r"environ\(\{[^\n]*?(?:\}\)|$)", re.M), "environ({[REDACTED]})"),
    # scheme://user:password@host: the user and the password
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]*:[^\s/@]*@"), r"\1[REDACTED]@"),
    # a JWT: header.payload.signature, each base64url; the header is {"... so it starts with eyJ
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED]"),
    # SigV4: `Signature=<hex>` of an Authorization header and `X-Amz-Signature=<hex>` of a
    # presigned URL
    (re.compile(r"(?i)(signature" + _ASSIGN + r")" + _VALUE), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)(authorization" + _ASSIGN + r"(?:bearer|basic|token)\s+)[^\s\"',;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?<![a-z])(?:password|passwd|secret|token|api[_-]?key)" + _ASSIGN + r")" + _VALUE
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:aws_)?(?:secret_access_key|session_token|security_token)"
            + _ASSIGN
            + r")"
            + _VALUE
        ),
        r"\1[REDACTED]",
    ),
    # a bare secret key next to an aws_secret-style name (`aws_secret_access_key wJalr...`, a CSV
    # row, an `aws configure` echo) or right after an access key id (`AKIA..., wJalr...`)
    (
        re.compile(
            r"(?i)(aws[_\s-]?secret[\w\s\"':=,>-]{0,24}?)" + _KEY_START + _SECRET_KEY + _KEY_END
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b([\s,;:\"'=>-]{1,4})"
            + _KEY_START
            + _SECRET_KEY
            + _KEY_END
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), "[REDACTED]"),
]


def redact(text):
    """A cheap scrub of credentials that logs commonly contain: bearer tokens, password / secret /
    token / api-key values (quoted or not), AWS secret keys and session tokens, bare secret keys
    beside an aws_secret name or an access key id, AWS access key ids (AKIA, ASIA), SigV4
    signatures, JWTs, `scheme://user:password@` connection strings, a printed Python
    `os.environ` and PEM private key headers."""
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
