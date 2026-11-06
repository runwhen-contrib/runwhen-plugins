"""The HTML ring status page (dskit's `/ring` and its siblings) as ring members.

`ctx.k8s_proxy_get` sends `Accept: */*`, and dskit serves the JSON form of the page only to a client
that asks for `application/json`, so through the S17 helper the page arrives as HTML. This module
reads the table of that page into the same member dicts the JSON form has (`id`, `state`,
`address`, `zone`, `timestamp`, `registered_timestamp`, `read_only`, and `token_count`). The page
carries a heartbeat as "3s ago (01:24:45)", a time of day in the server's zone, and the server's
own clock as "Current time: 2026-11-04 01:24:48.57 +0000 UTC"; the heartbeat is placed on the day
of that clock (the day before when it would be in the future). Anything that does not look like
that page is `None`, and the caller reports it as not a ring page.
"""

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

_NOW = re.compile(
    r"Current time:\s*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*([+-]\d{4})"
)
_BEAT = re.compile(r"\((\d{2}):(\d{2}):(\d{2})\)")
_COLUMNS = {
    "Instance ID": "id",
    "Availability Zone": "zone",
    "State": "state",
    "Address": "address",
    "Registered At": "registered_timestamp",
    "Read-Only": "read_only",
    "Last Heartbeat": "heartbeat",
    "Tokens": "tokens",
}


class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.header, self.rows = [], []
        self._cell = None
        self._row = None
        self._in_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "thead":
            self._in_head = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_head = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._in_head or (self._row and not self.header):
                self.header = self.header or self._row
            elif self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def server_now(text):
    """The page's "Current time" as an aware datetime, or None."""
    found = _NOW.search(text)
    if not found:
        return None
    day, clock, offset = found.groups()
    sign = 1 if offset[0] == "+" else -1
    zone = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])))
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=zone)


def heartbeat(text, now):
    """(timestamp text, ok): the heartbeat of a "3s ago (01:24:45)" cell as an RFC 3339 UTC
    text. A blank cell is ("", True) (never); one that does not parse is (text, False)."""
    if not text.strip():
        return "", True
    found = _BEAT.search(text)
    if not found or now is None:
        return text, False
    hour, minute, second = (int(x) for x in found.groups())
    beat = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if beat > now + timedelta(seconds=1):
        beat -= timedelta(days=1)
    return beat.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), True


def members(html):
    """(members, the server's "now" as RFC 3339 UTC text or None) of a ring page, or None when
    the text is not a ring page (no table with Instance ID and State columns)."""
    parser = _Table()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    fields = [_COLUMNS.get(h) for h in parser.header]
    if "id" not in fields or "state" not in fields:
        return None
    now = server_now("".join(parser.text))
    found = []
    for row in parser.rows:
        cells = {f: row[i] for i, f in enumerate(fields) if f and i < len(row)}
        beat, _ok = heartbeat(cells.get("heartbeat", ""), now)
        tokens = cells.get("tokens", "")
        member = {
            "id": cells.get("id"),
            "state": cells.get("state"),
            "address": cells.get("address"),
            "zone": cells.get("zone", ""),
            "timestamp": beat,
            "registered_timestamp": cells.get("registered_timestamp", ""),
            "token_count": int(tokens) if tokens.isdigit() else None,
        }
        if "read_only" in cells:
            member["read_only"] = cells["read_only"].strip().lower() in ("true", "yes")
        found.append(member)
    when = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if now else None
    return found, when
