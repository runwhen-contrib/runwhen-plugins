"""The query-text normalizer as a SQL expression, so raw text never leaves the database.

`normalized_query_expr(column)` builds an expression that reads `column` (the first
`RAW_QUERY_CHARS` characters of a statement) and returns the text with comments, string literals
(plain, E'...' and dollar-quoted) and numbers replaced by `?`. The statement then cuts it to
`QUERY_CHARS` characters. The Python normalizer (`sqltext.py`) runs on what the database returns
as a second pass, so a miss here is not the only barrier; this one is the first.

It is a chain of `regexp_replace` passes, each `g`lobal:

  1. from the FIRST dollar-quote opener (`$$` or `$tag$`) to the end of the text, everything is
     replaced by one `?`. A dollar quote cannot be told from a `$$` inside a string, comment or
     quoted identifier before those are lexed, and pairing openers first would pair a `$$` that
     is only text with a real one and leave what lies between them. So the text is not
     scanned past an opener at all;
  2. in ONE pass, so a `--` or `/*` inside a string is not a comment and a quote inside a comment
     is not a string: closed block comments (nested to three levels), `--` comments, E'...',
     '...'; a quoted identifier "..." is matched too and put back unchanged (the `\\1`), which
     leaves a stray `?` after it that pass 2b removes;
  3. what is still open at the end of the text (a string, comment or quoted identifier cut off by
     track_activity_query_size or by the left() above) is replaced to its end;
  4. numbers, with quoted identifiers put back unchanged as in 2.

Over-redaction is the failure direction: a `$$` inside a plain string, comment or identifier
hides the rest of the statement, and a comment nested more than three levels hides more text than
needed; neither ever hides less. The text before the first opener holds no dollar quote, so
passes 2 to 4 lex it exactly. The scan assumes standard_conforming_strings = on (the default
since PostgreSQL 9.1), like the Python normalizer: a client that runs with it off writes
backslash escapes in plain '...' strings, which this scan would read as ordinary characters and
could close a string early. The text is prefixed with a space so that the look-behind that tells
an E-string prefix from an identifier ending in `e` has a character to look at."""

RAW_QUERY_CHARS = 8192  # characters of query text the statement reads; the rest is never read
QUERY_CHARS = 1024  # characters of normalized text the statement returns

# A dollar-quote tag is an identifier without `$`: PostgreSQL's lexer takes letters, `_` and every
# non-ASCII character (bytes \200-\377) as tag characters, so `$é$` and `$日本$` open a quote.
# \uXXXX and \UXXXXXXXX are the regex engine's own character escapes, valid in a bracket.
_TAG_START = r"[A-Za-z_\u0080-\U0010FFFF]"
_TAG_CHAR = r"[A-Za-z0-9_\u0080-\U0010FFFF]"
_DOLLAR_TO_END = rf"\$(?:{_TAG_START}{_TAG_CHAR}*)?\$.*$"

# A closing quote that another quote follows is not a closing quote: `''` inside a string is an
# escaped quote. Without the `(?!')` the regex engine backtracks from an unclosed E'''\'secret to
# the shorter, closed E'' and reads the rest as a new string that the backslash closes, leaving
# `secret` in the clear.
_QUOTED_IDENT = r'("(?:[^"]|"")*")'
_PLAIN_CHAR = r"(?:[^*/]|\*(?!/)|/(?!\*))"
_BLOCK1 = rf"/\*{_PLAIN_CHAR}*\*/"
_BLOCK2 = rf"/\*(?:{_PLAIN_CHAR}|{_BLOCK1})*\*/"
_BLOCK3 = rf"/\*(?:{_PLAIN_CHAR}|{_BLOCK2})*\*/"
_LITERALS = "|".join(
    (
        _QUOTED_IDENT,
        _BLOCK3,
        r"--[^\n]*",
        r"(?<![A-Za-z0-9_$])[eE]'(?:[^'\\]|\\.|'')*'(?!')",
        r"(?<![^A-Za-z0-9_$][eE])'(?:[^']|'')*'(?!')",
    )
)
_UNCLOSED = "|".join((_QUOTED_IDENT, r"'.*$", r"/\*.*$", r'"[^"]*$'))
_NUMBER = "|".join(
    (
        _QUOTED_IDENT,
        r"(?<![A-Za-z0-9_$])(?:0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+"
        r"|(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9]+)?)",
    )
)


def _lit(pattern):
    return "'" + pattern.replace("'", "''") + "'"


def _replace(expr, pattern, replacement):
    return f"regexp_replace({expr}, {_lit(pattern)}, {_lit(replacement)}, 'g')"


def normalized_query_expr(column):
    """SQL for `column`'s text with literals and comments replaced by `?` (NULL stays NULL),
    not yet cut to QUERY_CHARS."""
    expr = f"(' ' || {column})"
    expr = _replace(expr, _DOLLAR_TO_END, "?")
    expr = _replace(expr, _LITERALS, "\\1?")
    expr = _replace(expr, '(")\\?', "\\1")
    expr = _replace(expr, _UNCLOSED, "\\1?")
    expr = _replace(expr, '(")\\?', "\\1")
    expr = _replace(expr, _NUMBER, "\\1?")
    expr = _replace(expr, '(")\\?', "\\1")
    return f"substr({expr}, 2)"  # drop the guard space
