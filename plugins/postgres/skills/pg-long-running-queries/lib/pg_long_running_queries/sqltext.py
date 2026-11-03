"""Query text that is safe to return: string and numeric literals replaced with `?`.

A statement in pg_stat_activity holds whatever values the client sent: names, e-mail addresses,
tokens. `normalize_query` removes them, and `clip` cuts the result to a byte budget. The task
returns query text only through `safe_query_text`, so no path hands back the raw text.

Replaced with `?`: '...' strings (with '' escapes), E'...' strings (with backslash escapes),
$tag$...$tag$ strings, and numbers (decimal, fraction, exponent, hex, octal and binary, with
underscores). Comments are dropped, since a comment may carry a value as well. Quoted
identifiers ("...") and parameter markers ($1) are kept. Text the scanner cannot close (a string,
comment or quoted identifier cut off by track_activity_query_size or by the task) is replaced to
its end, so a cut-off literal never leaks its beginning.

The scan assumes standard_conforming_strings = on (the default since PostgreSQL 9.1), where a
backslash in a plain '...' string is an ordinary character."""

import re

MAX_QUERY_BYTES = 1024
PLACEHOLDER = "?"

_TOKEN = re.compile(
    r"""
      (?P<line>--[^\n]*)
    | (?P<block>/\*)
    | (?P<quoted>"(?:[^"]|"")*")
    | (?P<estring>[eE]'(?:[^'\\]|\\.|'')*')
    | (?P<unclosed_estring>[eE]'.*)
    | (?P<string>'(?:[^']|'')*')
    | (?P<dollar>\$(?:[^\W\d]\w*)?\$)
    | (?P<number>
        (?<![\w$])
        (?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+
          |(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?)
      )
    | (?P<word>[^\W\d][\w$]*)
    | (?P<unclosed_quote>["'].*)
    | (?P<other>.)
    """,
    re.X | re.S,
)


def _skip_block_comment(text, start):
    """Index just past the (nestable) block comment that opens at `start`, or len(text)."""
    depth, i = 0, start
    while i < len(text):
        if text.startswith("/*", i):
            depth += 1
            i += 2
        elif text.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


def normalize_query(text):
    """`text` with literals replaced by `?`, comments dropped and whitespace collapsed."""
    text = str(text)
    out = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        kind = match.lastgroup
        if kind == "block":
            out.append(" ")
            pos = _skip_block_comment(text, pos)
        elif kind == "line":
            out.append(" ")
            pos = match.end()
        elif kind == "dollar":
            tag = match.group()
            close = text.find(tag, match.end())
            out.append(PLACEHOLDER)
            if close < 0:
                break
            pos = close + len(tag)
        elif kind in ("string", "estring", "number"):
            out.append(PLACEHOLDER)
            pos = match.end()
        elif kind in ("unclosed_quote", "unclosed_estring"):
            out.append(PLACEHOLDER)
            break
        else:  # quoted identifier, word, other
            out.append(match.group())
            pos = match.end()
    return " ".join("".join(out).split())


def clip(text, limit=MAX_QUERY_BYTES):
    """(`text` cut to `limit` UTF-8 bytes on a character boundary, whether it was cut)."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", "ignore"), True


def safe_query_text(raw, limit=MAX_QUERY_BYTES):
    """(normalized text cut to `limit` bytes, cut?) for a statement as pg_stat_activity holds it;
    (None, False) when there is no text."""
    if raw is None:
        return None, False
    return clip(normalize_query(raw), limit)
