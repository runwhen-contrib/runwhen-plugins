"""Bounds shared by the views."""

MESSAGE_LIMIT = 1024
NESTED_LIMIT = 50
# a backend's body in an error message: its first line only, this long
BODY_LIMIT = 256


def cap(items, limit):
    return items[:limit]


def truncate(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); (None, False) for None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)


def cap_text(value, limit=MESSAGE_LIMIT):
    """A name or id cut to `limit` characters. None and numbers stay as they are; a string is cut;
    anything else (a foreign document's id may be an object) is shown as its text, cut."""
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return value
    return truncate(value, limit)[0]
