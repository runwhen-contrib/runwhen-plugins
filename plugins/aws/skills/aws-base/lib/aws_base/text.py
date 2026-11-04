"""Text that enters an output: cut to 1024 characters, and tags whose value looks like a secret.

EC2 writes the reasons and event descriptions itself, so they are cut, not scrubbed. A tag is
free text written by anyone, and people put passwords and keys in tags: a tag whose key names a
secret, or whose value has the shape of one, is reported by key with `redacted: true` and no
value. The check is cheap and not complete: do not repeat a value that looks like a credential."""

import re

MAX_TEXT_CHARS = 1024  # any free-text field is cut to this before it enters an output
MAX_TAG_VALUE_CHARS = 256  # a tag value is at most 256 characters in EC2
MAX_LIST = 50  # nested lists

# a tag key that names a secret
_SECRET_KEY_NAME = re.compile(
    r"(?i)pass(?:word|wd|phrase)?|secret|token|credential|api[-_ .]?key|access[-_ .]?key"
    r"|private[-_ .]?key|auth|cert|signature|\bsas\b|connection[-_ .]?string|\bpwd\b"
)
# a value that has the shape of one
_SECRET_VALUE = (
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE|PGP)[A-Z ]*-----"),  # PEM
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."),  # JWT
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@"),  # scheme://user:password@host
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|glpat|xox[abprs]|sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
)
# a long unbroken run of base64 / hex-like characters (a key, a token, a digest used as one);
# `-` and `_` are not in it, so a long hyphenated name is not taken for a key
_OPAQUE = re.compile(r"[A-Za-z0-9/+=]{32,}")


def clip(text, limit=MAX_TEXT_CHARS):
    """(text cut to `limit` characters with a `...` tail, whether it was cut)."""
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 3] + "...", True
    return text, False


def looks_secret(key, value):
    """Whether a tag (key, value) should not be shown: the key names a secret, or the value has a
    credential's shape."""
    if _SECRET_KEY_NAME.search(str(key)):
        return True
    text = str(value)
    if any(rx.search(text) for rx in _SECRET_VALUE):
        return True
    for run in _OPAQUE.findall(text):
        # a path or a name with separators is not opaque: needs letters and digits mixed
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return True
    return False


def tag_facts(key, value):
    """{key, value, redacted}: the value is None when it looks like a secret (redacted true)."""
    shown_key, _ = clip(key, 128)
    if value is None:
        return {"key": shown_key, "value": None, "redacted": False}
    if looks_secret(key, value):
        return {"key": shown_key, "value": None, "redacted": True}
    shown, _ = clip(value, MAX_TAG_VALUE_CHARS)
    return {"key": shown_key, "value": shown, "redacted": False}


def display_name(tags):
    """The value of the `Name` tag when it is shown (not a secret-looking one), else None."""
    for tag in tags:
        if tag["key"] == "Name" and not tag["redacted"] and tag["value"]:
            return tag["value"]
    return None
