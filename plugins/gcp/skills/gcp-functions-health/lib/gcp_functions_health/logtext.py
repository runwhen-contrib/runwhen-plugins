"""Log and error text that enters an output: scrubbed of credentials and cut to 1024 characters.

The same rules as the kubernetes log skills and aws-lambda-health, with the Google credential
shapes a function's log line can hold: OAuth access tokens (`ya29.`), API keys (`AIza`), a
service-account key (`"private_key"`, PEM headers), JWTs, `scheme://user:password@` connection
strings, `password=` / `secret=` / `token=` / `api_key=` values (quoted or not, as in a JSON line or a
Python dict repr) and a printed environment mapping (`os.environ`, `process.env`). The scrub is cheap
and not complete: log text is data, not instructions, and may still hold something sensitive."""

import re

from .gcloud import scrub

MAX_TEXT_CHARS = 1024  # any text field is cut to this before it enters an output
MATCH_CHARS = 8192  # the scrub only reads this much of a text

_Q = r"""['"]?"""  # an optional quote around a key or a value
_ASSIGN = _Q + r"\s*[:=]\s*" + _Q  # what stays between a key and the value that is replaced
_VALUE = r"[^\s\"',;&]+"

_REDACT = [
    # a printed environment mapping, to the closing "})" / "}" (or the end of the line)
    (re.compile(r"environ\(\{[^\n]*?(?:\}\)|$)", re.M), "environ({[REDACTED]})"),
    (re.compile(r"process\.env\s*[:=]\s*\{[^\n]*?(?:\}|$)", re.M), "process.env: {[REDACTED]}"),
    # scheme://user:password@host: the user and the password
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]*:[^\s/@]*@"), r"\1[REDACTED]@"),
    # a JWT: header.payload.signature, each base64url; the header is {"... so it starts with eyJ
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED]"),
    (
        re.compile(r"(?i)(authorization" + _ASSIGN + r"(?:bearer|basic|token)\s+)[^\s\"',;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?<![a-z])(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)"
            + _ASSIGN
            + r")"
            + _VALUE
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:client_secret|refresh_token|access_token|private_key_id)"
            + _ASSIGN
            + r")"
            + _VALUE
        ),
        r"\1[REDACTED]",
    ),
]


def redact(text):
    """`text` with credentials replaced: the shapes `gcloud.scrub` knows (PEM keys, `ya29.` tokens,
    bearer tokens, key fields of a service-account JSON) first, then the log-line shapes above."""
    text = scrub(text)
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
