"""Unit tests for k8s-cronjob-health's cron evaluation (lib/k8s_cronjob_health/cron.py):
a five-field parser and a previous/next fire search that follow robfig/cron, which is
what the Kubernetes CronJob controller uses."""

from datetime import datetime, timedelta, timezone

import pytest
from k8s_cronjob_health import cron

UTC = timezone.utc


def at(text):
    """'2026-11-03T12:00:30' -> aware UTC datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def prev(expr, now, zone=None):
    got = cron.parse_schedule(expr, zone).prev(at(now))
    return got and got.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def nxt(expr, after, zone=None):
    got = cron.parse_schedule(expr, zone).next(at(after))
    return got and got.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------- fields ----


def test_every_minute_previous_is_at_or_before_now_and_next_is_strictly_after():
    assert prev("* * * * *", "2026-11-03T12:00:30") == "2026-11-03T12:00:00"
    assert prev("* * * * *", "2026-11-03T12:00:00") == "2026-11-03T12:00:00"
    assert nxt("* * * * *", "2026-11-03T12:00:00") == "2026-11-03T12:01:00"
    assert nxt("* * * * *", "2026-11-03T12:00:30") == "2026-11-03T12:01:00"
    assert nxt("* * * * *", "2026-11-03T11:59:59") == "2026-11-03T12:00:00"


@pytest.mark.parametrize(
    ("expr", "now", "expected"),
    [
        ("*/15 * * * *", "2026-11-03T12:44:00", "2026-11-03T12:30:00"),
        ("5/20 * * * *", "2026-11-03T12:30:00", "2026-11-03T12:25:00"),  # 5,25,45
        ("10-40/10 * * * *", "2026-11-03T12:35:00", "2026-11-03T12:30:00"),  # 10,20,30,40
        ("1,5,10 * * * *", "2026-11-03T12:07:00", "2026-11-03T12:05:00"),
        ("0 9-17/4 * * *", "2026-11-03T16:00:00", "2026-11-03T13:00:00"),  # 9,13,17
        ("0 9 * jan-mar mon-fri", "2026-02-14T12:00:00", "2026-02-13T09:00:00"),  # Sat -> Fri
        ("30 4 1,15 * *", "2026-11-03T12:00:00", "2026-11-01T04:30:00"),
        ("0 0 ? * SUN", "2026-11-03T12:00:00", "2026-11-01T00:00:00"),
        ("0 0 * * 0", "2026-11-03T12:00:00", "2026-11-01T00:00:00"),
        ("0 12 * * *", "2026-11-03T12:00:00", "2026-11-03T12:00:00"),
    ],
)
def test_previous_fire(expr, now, expected):
    assert prev(expr, now) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("@hourly", "2026-11-03T12:00:00"),
        ("@daily", "2026-11-03T00:00:00"),
        ("@midnight", "2026-11-03T00:00:00"),
        ("@weekly", "2026-11-01T00:00:00"),  # Sunday
        ("@monthly", "2026-11-01T00:00:00"),
        ("@yearly", "2026-01-01T00:00:00"),
        ("@annually", "2026-01-01T00:00:00"),
    ],
)
def test_descriptors(expr, expected):
    assert prev(expr, "2026-11-03T12:34:00") == expected


def test_names_are_case_insensitive():
    assert prev("0 0 1 NOV Tue", "2026-11-03T12:00:00") == "2026-11-03T00:00:00"


def test_day_of_month_and_day_of_week_are_ored_unless_one_is_a_star():
    # 2026-03-13 is a Friday, 2026-03-06 the Friday before
    assert nxt("0 0 13 * 5", "2026-03-01T00:00:00") == "2026-03-06T00:00:00"  # 13th OR Friday
    assert nxt("0 0 13 * *", "2026-03-01T00:00:00") == "2026-03-13T00:00:00"
    assert nxt("0 0 * * 5", "2026-03-01T00:00:00") == "2026-03-06T00:00:00"  # star: both must match
    # a step above 1 is not a star: odd days OR Friday -> the 3rd comes before the 6th
    assert nxt("0 0 */2 * 5", "2026-03-01T00:00:00") == "2026-03-03T00:00:00"
    # "*/1" keeps the star: every day AND Friday
    assert nxt("0 0 */1 * 5", "2026-03-01T00:00:00") == "2026-03-06T00:00:00"


def test_month_end_and_leap_day_schedules_skip_to_the_next_match():
    assert nxt("0 0 31 * *", "2026-04-01T00:00:00") == "2026-05-31T00:00:00"
    assert nxt("0 0 29 2 *", "2026-01-01T00:00:00") == "2028-02-29T00:00:00"
    assert prev("0 0 29 2 *", "2026-11-03T00:00:00") == "2024-02-29T00:00:00"


def test_a_schedule_that_never_fires_gives_none():
    assert nxt("0 0 31 2 *", "2026-01-01T00:00:00") is None
    assert prev("0 0 31 2 *", "2026-01-01T00:00:00") is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 7",
        "a * * * *",
        "*/0 * * * *",
        "*/x * * * *",
        "5-1 * * * *",
        "1-2-3 * * * *",
        "*-5 * * * *",
        "*/2/3 * * * *",
        "@sometimes",
        "@every",
        "@every x",
        "@every 5",
        "1,,2 * * * *",
        "* * * foo *",
    ],
)
def test_what_the_controller_would_refuse_is_refused(bad):
    with pytest.raises(ValueError):
        cron.parse_schedule(bad)


# ------------------------------------------------------------- zones ----


def test_time_zone_moves_the_fire_with_daylight_saving():
    # Europe/London is UTC+1 in summer, UTC+0 in winter
    assert prev("0 3 * * *", "2026-07-01T12:00:00", "Europe/London") == "2026-07-01T02:00:00"
    assert prev("0 3 * * *", "2026-12-01T12:00:00", "Europe/London") == "2026-12-01T03:00:00"
    assert prev("0 9 * * *", "2026-11-03T12:00:00", "Asia/Kolkata") == "2026-11-03T03:30:00"


def test_a_wall_time_skipped_by_spring_forward_does_not_fire():
    # America/New_York 2026-03-08: 02:00 -> 03:00, so 02:30 does not exist that day
    assert prev("30 2 * * *", "2026-03-08T12:00:00", "America/New_York") == "2026-03-07T07:30:00"
    assert nxt("30 2 * * *", "2026-03-07T07:30:00", "America/New_York") == "2026-03-09T06:30:00"


def test_a_repeated_wall_time_fires_at_both_occurrences():
    # America/New_York 2026-11-01: 01:30 happens twice (05:30Z in EDT, 06:30Z in EST)
    ny = "America/New_York"
    assert prev("30 1 * * *", "2026-11-01T12:00:00", ny) == "2026-11-01T06:30:00"
    assert prev("30 1 * * *", "2026-11-01T06:00:00", ny) == "2026-11-01T05:30:00"
    assert nxt("30 1 * * *", "2026-11-01T05:30:00", ny) == "2026-11-01T06:30:00"
    assert nxt("30 1 * * *", "2026-11-01T06:30:00", ny) == "2026-11-02T06:30:00"
    assert nxt("30 1 * * *", "2026-10-31T12:00:00", ny) == "2026-11-01T05:30:00"
    # an ordinary day fires once, and the 01:45 repeat sorts after 01:30's
    assert prev("30 1 * * *", "2026-11-03T12:00:00", ny) == "2026-11-03T06:30:00"
    assert nxt("*/15 1 * * *", "2026-11-01T05:45:00", ny) == "2026-11-01T06:00:00"


def test_tz_prefix_in_the_schedule_sets_the_zone():
    assert prev("CRON_TZ=Asia/Kolkata 0 9 * * *", "2026-11-03T12:00:00") == "2026-11-03T03:30:00"
    assert prev("TZ=Asia/Kolkata 0 9 * * *", "2026-11-03T12:00:00") == "2026-11-03T03:30:00"
    # a prefix wins over spec.timeZone, as in the controller's formatSchedule (the API
    # accepts only one of them)
    assert cron.parse_schedule("TZ=Asia/Tokyo 0 9 * * *", "UTC").zone_name == "Asia/Tokyo"


def test_unknown_zone_is_its_own_error():
    with pytest.raises(cron.TimeZoneUnavailable):
        cron.parse_schedule("* * * * *", "Mars/Olympus")
    with pytest.raises(ValueError):
        cron.parse_schedule("* * * * *", "Local")


# ------------------------------------------------------------- @every ----


def test_every_counts_from_the_anchor():
    s = cron.parse_schedule("@every 90s")
    assert s.next(at("2026-11-03T12:00:00")) == at("2026-11-03T12:01:30")
    anchor = at("2026-11-03T12:00:00")
    assert s.prev(at("2026-11-03T12:05:00"), anchor) == at("2026-11-03T12:04:30")
    assert s.prev(at("2026-11-03T12:01:00"), anchor) is None  # not yet fired once
    assert s.prev(at("2026-11-03T12:05:00"), None) is None


def test_every_next_chains_from_the_anchor_not_from_the_instant_asked_about():
    hourly = cron.parse_schedule("@every 1h")
    anchor = at("2026-11-03T00:00:00")
    assert hourly.next(at("2026-11-03T03:30:00"), anchor) == at("2026-11-03T04:00:00")
    assert hourly.next(at("2026-11-03T04:00:00"), anchor) == at("2026-11-03T05:00:00")
    assert hourly.next(at("2026-11-02T20:00:00"), anchor) == at("2026-11-03T01:00:00")  # first fire
    assert hourly.next(at("2026-11-03T03:30:00")) == at("2026-11-03T04:30:00")  # no anchor: +delay
    assert cron.parse_schedule("0 * * * *").next(at("2026-11-03T03:30:00"), anchor) == at(
        "2026-11-03T04:00:00"
    )


def test_go_durations():
    assert cron.parse_go_duration("1h30m") == 5400
    assert cron.parse_go_duration("1.5h") == 5400
    assert cron.parse_go_duration("250ms") == 0.25
    for bad in ("", "5", "m", "5 m", "5x"):
        with pytest.raises(ValueError):
            cron.parse_go_duration(bad)
    assert cron.parse_schedule("@every 0s").delay == timedelta(
        seconds=1
    )  # whole seconds, at least 1
