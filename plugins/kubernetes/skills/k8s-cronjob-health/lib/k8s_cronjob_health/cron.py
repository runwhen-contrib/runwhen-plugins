"""Cron schedules as the Kubernetes CronJob controller reads them.

The controller parses `spec.schedule` with robfig/cron's standard parser: five
fields (minute hour day-of-month month day-of-week), each a list of `*`, `?`,
numbers, names (`jan`, `mon`), ranges (`1-5`), and steps (`*/15`, `10-40/5`,
`5/10` meaning `5-max/10`); or a descriptor (`@hourly`, `@daily`, `@midnight`,
`@weekly`, `@monthly`, `@yearly`, `@annually`, `@every <duration>`). An optional
leading `TZ=<zone>` or `CRON_TZ=<zone>` token sets the zone; `spec.timeZone`
sets it otherwise (the API allows only one of them).

Pure functions: no clock, no kubectl. `Schedule.prev` and `Schedule.next` take
the instant to look from. Fires happen at second 0 of a minute, in the schedule's
zone, evaluated on the wall clock: a wall time that does not exist (spring
forward) does not fire, and a repeated wall time (fall back) fires once, at its
first occurrence.
"""

import re
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
# The furthest the day-by-day search goes: enough for a Feb 29 schedule across a century skip.
SEARCH_DAYS = 366 * 9

MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
WEEKDAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
DESCRIPTORS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class TimeZoneUnavailable(ValueError):
    """The zone name is not known to this machine's time zone database."""


def load_zone(name):
    """tzinfo for an IANA zone name; None or '' or 'UTC' is UTC."""
    if not name or name in ("UTC", "Etc/UTC"):
        return UTC
    if name == "Local":
        raise ValueError("time zone 'Local' is not allowed")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimeZoneUnavailable(f"time zone {name!r} is not in the time zone database") from exc


def _number(text, names, lo, hi, field):
    key = text.lower()
    if key in names:
        value = names[key]
    elif text.isdigit():
        value = int(text)
    else:
        raise ValueError(f"{field}: {text!r} is not a number or name")
    if not lo <= value <= hi:
        raise ValueError(f"{field}: {value} is outside {lo}-{hi}")
    return value


def parse_field(expr, lo, hi, field, names=None):
    """(set of values, star) for one cron field. `star` is robfig's star bit: the
    field is `*` or `?` without a step above 1 (it decides day-of-month versus
    day-of-week combination)."""
    names = names or {}
    if not expr:
        raise ValueError(f"{field}: empty")
    values, star = set(), False
    for part in expr.split(","):
        range_part, slash, step_text = part.partition("/")
        if "/" in step_text:
            raise ValueError(f"{field}: too many slashes in {part!r}")
        bounds = range_part.split("-")
        if len(bounds) > 2:
            raise ValueError(f"{field}: too many hyphens in {part!r}")
        part_star = False
        if bounds[0] in ("*", "?"):
            if len(bounds) > 1:
                raise ValueError(f"{field}: {part!r} is not a range")
            start, end, part_star = lo, hi, True
        else:
            start = _number(bounds[0], names, lo, hi, field)
            end = _number(bounds[1], names, lo, hi, field) if len(bounds) == 2 else start
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"{field}: step {step_text!r} must be a positive integer")
            step = int(step_text)
            if len(bounds) == 1 and not part_star:
                end = hi  # "5/10" is "5-max/10"
            if step > 1:
                part_star = False
        if start > end:
            raise ValueError(f"{field}: range {part!r} goes backwards")
        values.update(range(start, end + 1, step))
        star = star or part_star
    return values, star


_GO_DURATION = re.compile(r"(\d+(?:\.\d+)?|\.\d+)(ns|us|µs|ms|s|m|h)")
_GO_UNIT = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_go_duration(text):
    """'5m', '1h30m', '1.5h' (Go's time.ParseDuration, positive only) -> seconds."""
    parts = _GO_DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a duration: {text!r}")
    return sum(float(n) * _GO_UNIT[u] for n, u in parts)


def _wall_to_utc(wall, zone):
    """The UTC instant of a naive wall-clock time in `zone`, or None when that
    wall time does not exist (skipped by a spring-forward change)."""
    aware = wall.replace(tzinfo=zone)
    utc = aware.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != wall:
        return None
    return utc


class Every:
    """`@every <duration>`: a fixed delay (whole seconds, at least 1) from the
    previous run, so its fire times depend on where the chain started."""

    def __init__(self, seconds):
        self.delay = timedelta(seconds=max(1, int(seconds)))
        self.zone = UTC
        self.zone_name = "UTC"

    def next(self, after):
        return after.astimezone(UTC).replace(microsecond=0) + self.delay

    def prev(self, now, anchor):
        """Latest anchor + k*delay (k >= 1) at or before `now`; None without an anchor or before the first."""
        if anchor is None:
            return None
        k = int((now - anchor) / self.delay)
        return anchor + k * self.delay if k >= 1 else None


class Cron:
    def __init__(self, fields, zone, zone_name):
        (
            self.minutes,
            self.hours,
            (self.doms, self.dom_star),
            self.months,
            (self.dows, self.dow_star),
        ) = fields
        self.zone, self.zone_name = zone, zone_name

    def _day_matches(self, day):
        if day.month not in self.months:
            return False
        dom = day.day in self.doms
        dow = (day.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom and dow
        return dom or dow

    def _times(self, descending=False):
        times = [(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]
        return times[::-1] if descending else times

    def next(self, after):
        """First fire strictly after `after` (aware datetime), or None."""
        floor = after.astimezone(self.zone).replace(tzinfo=None)
        day = floor.date()
        for offset in range(SEARCH_DAYS):
            d = day + timedelta(days=offset)
            if not self._day_matches(d):
                continue
            for h, m in self._times():
                wall = datetime(d.year, d.month, d.day, h, m)
                if wall <= floor:
                    continue
                utc = _wall_to_utc(wall, self.zone)
                if utc is not None and utc > after:
                    return utc
        return None

    def prev(self, now, anchor=None):
        """Last fire at or before `now`, or None."""
        local = now.astimezone(self.zone).replace(tzinfo=None)
        day = local.date()
        for offset in range(SEARCH_DAYS):
            d = day - timedelta(days=offset)
            if not self._day_matches(d):
                continue
            for h, m in self._times(descending=True):
                wall = datetime(d.year, d.month, d.day, h, m)
                if wall > local:
                    continue
                utc = _wall_to_utc(wall, self.zone)
                if utc is not None and utc <= now:
                    return utc
        return None


def parse_schedule(text, time_zone=None):
    """A `Cron` or `Every` for `spec.schedule`, in `spec.timeZone` (or the schedule's
    own TZ=/CRON_TZ= prefix; UTC when neither). Raises ValueError (TimeZoneUnavailable
    for a zone this machine does not know) on anything the controller would refuse."""
    spec = (text or "").strip()
    if not spec:
        raise ValueError("empty schedule")
    zone_name = time_zone or None
    if spec.startswith(("TZ=", "CRON_TZ=")):
        prefix, _, spec = spec.partition(" ")
        zone_name = zone_name or prefix.split("=", 1)[1]
        spec = spec.strip()
    if spec.startswith("@every"):
        _, _, duration = spec.partition(" ")
        return Every(parse_go_duration(duration.strip()))
    if spec.startswith("@"):
        if spec not in DESCRIPTORS:
            raise ValueError(f"unknown descriptor {spec!r}")
        spec = DESCRIPTORS[spec]
    words = spec.split()
    if len(words) != 5:
        raise ValueError(f"expected 5 fields, got {len(words)}")
    minute, hour, dom, month, dow = words
    fields = (
        parse_field(minute, 0, 59, "minute")[0],
        parse_field(hour, 0, 23, "hour")[0],
        parse_field(dom, 1, 31, "day-of-month"),
        parse_field(month, 1, 12, "month", MONTHS)[0],
        parse_field(dow, 0, 6, "day-of-week", WEEKDAYS),
    )
    return Cron(fields, load_zone(zone_name), zone_name or "UTC")
