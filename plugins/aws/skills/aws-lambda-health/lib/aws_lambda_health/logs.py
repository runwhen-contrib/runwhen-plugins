"""function-errors: the log events of one function that match the error pattern.

CloudWatch Logs filter-log-events returns the oldest matches first, so a result cut at
`maxLogEvents` shows the start of the window and says `truncated`; narrow `since` to see the end."""

from datetime import datetime, timezone

from .awscli import aws_paged
from .errors import RwError
from .logtext import clip

# NextToken must stay in the projection (awscli.aws_paged requires it); ingestionTime, eventId and
# the per-call searchedLogStreams list are not needed.
LOGS_QUERY = (
    "{events: events[].{timestamp: timestamp, logStreamName: logStreamName, message: message}, "
    "NextToken: NextToken}"
)
# an OR of terms: a line with any of them. The default catches Python/Node/Java errors written by
# the runtime and the platform's own timeout and crash lines.
DEFAULT_PATTERN = '?ERROR ?Exception ?"Task timed out" ?"Runtime.ExitError"'


def millis(moment):
    return int(moment.timestamp() * 1000)


def read_events(ctx, region, log_group, start, end, pattern, limit):
    """(raw events, truncated) matching `pattern` in the window, at most `limit` of them."""
    return aws_paged(
        ctx,
        [
            "logs",
            "filter-log-events",
            "--log-group-name",
            log_group,
            "--start-time",
            millis(start),
            "--end-time",
            millis(end),
            "--filter-pattern",
            pattern,
            "--query",
            LOGS_QUERY,
        ],
        region=region,
        items_key="events",
        limit=limit,
    )


def event_facts(raw):
    """One event as an output row: ISO time, stream, scrubbed text cut to 1024 characters."""
    if not isinstance(raw, dict) or not isinstance(raw.get("timestamp"), int):
        raise RwError("internal", "a log event has no millisecond timestamp")
    message, cut = clip(raw.get("message") or "")
    when = datetime.fromtimestamp(raw["timestamp"] / 1000, tz=timezone.utc)
    return {
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z",
        "logStream": raw.get("logStreamName"),
        "message": message,
        "truncated": cut,
    }


def log_facts(log_group, found, start, end, pattern, events, truncated):
    """The `logs` output. `found` is False when the log group does not exist (no events then)."""
    rows = [event_facts(e) for e in events]
    return {
        "logGroup": log_group,
        "logGroupFound": found,
        "window": {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "pattern": pattern,
        "events": rows,
        "matched": len(rows),
        "truncated": truncated,
        "messagesTruncated": any(r["truncated"] for r in rows),
    }
