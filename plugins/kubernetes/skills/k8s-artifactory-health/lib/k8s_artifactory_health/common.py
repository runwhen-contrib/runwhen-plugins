"""Bounds shared by the views."""

MESSAGE_LIMIT = 1024
NESTED_LIMIT = 50


def cap(items, limit):
    return items[:limit]


def truncate(text, limit=MESSAGE_LIMIT):
    """(text cut to `limit` characters, whether it was cut); (None, False) for None."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:limit], True) if len(text) > limit else (text, False)
