"""Free text that comes from outside the task (what a guest script printed, an extension's status,
a message in the Activity Log, a Resource Health summary) is cut to TEXT_LIMIT characters AFTER
the secrets and identities in it are redacted, so a cut can never leave half of one behind.

`common.clip` is a verbatim copy shared by every azure skill; the redaction belongs to the skills
that return such text, so it is here, in `safe_clip`."""

import re

from .common import TEXT_LIMIT, clip

REDACTED = "<redacted>"
# scheme://user:password@host and scheme://token@host
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
# name=value, name: value, "name": "value" where the name ends in a word that says it is a secret
_SECRET = re.compile(
    r"(?i)\b([a-z0-9_.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|account[_-]?key|private[_-]?key|connection[_-]?string|authorization|sig|sas)\"?\s*[=:]\s*)"
    r"((?:bearer|basic)\s+[^\s,;&\"']+|\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*)?")
# who an operation was done by or refused for: a person's or principal's identifiers
_WHO = re.compile(r"(?i)\b(client|object id|user|principal|caller)\s+'[^']*'")
_CALLER = re.compile(r"(?i)\b(caller)\s*:\s*\S+")
_ADDRESS = re.compile(r"[^\s'\"<>(),;]+@[^\s'\"<>(),;]+")


def redact_secrets(text):
    """`text` without URL credentials, `password=...`-style values, bearer tokens and JWTs."""
    if not isinstance(text, str):
        return text
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _JWT.sub(REDACTED, text)


def redact_who(text):
    """`text` without the identifiers of who did or was refused an operation: `client '...'`,
    `object id '...'`, `user '...'`, `principal '...'`, `Caller: ...` and e-mail addresses."""
    if not isinstance(text, str):
        return text
    text = _WHO.sub(lambda m: f"{m.group(1)} '{REDACTED}'", text)
    text = _CALLER.sub(lambda m: f"{m.group(1)}: {REDACTED}", text)
    return _ADDRESS.sub(REDACTED, text)


def safe_clip(text, limit=TEXT_LIMIT, who=False):
    """(redacted text cut to `limit` characters, whether it was cut). `who=True` also redacts the
    identifiers of people and principals (the Activity Log)."""
    text = redact_secrets(text)
    if who:
        text = redact_who(text)
    return clip(text, limit)
