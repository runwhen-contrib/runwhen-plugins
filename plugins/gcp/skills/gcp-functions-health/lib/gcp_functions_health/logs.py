"""function-errors: the error-level log entries of one function, newest first.

`gcloud logging read` takes a Logging query language filter, `--limit` and `--order` (default
desc, newest first: a result cut at the limit shows the end of the window, where the problem is
now). The window is two `timestamp` clauses in the filter, from the run's reference time: gcloud
adds its own `--freshness` (default 1d) only when the filter does not mention "timestamp", so
relying on it would be wrong for a window other than a day and would depend on the wall clock
(and a function named `timestamp-sync` would switch it off by accident). The filter names the function's own resource: a 1st gen
function logs as `cloud_function` (labels function_name, region), a 2nd gen one as the
`cloud_run_revision` of its Cloud Run service (labels service_name, location). Names are
validated before they enter the filter.

Only five facts of an entry are returned, the time, severity, revision / execution, HTTP status
and one message, taken from `textPayload`, from a well-known string key of `jsonPayload`, or from
`protoPayload.status.message`; a structured payload is never dumped (it may hold environment
values). The message is scrubbed of credentials and cut to 1024 characters (logtext.py)."""

from datetime import timedelta, timezone

from .errors import RwError
from .fnlist import json_call
from .logtext import clip

JSON_KEYS = ("message", "msg", "error", "err", "text", "log")
MAX_ID = 128


def window(now, since_seconds):
    """(start, end) datetimes, UTC; `end` is the run's reference time to the second."""
    end = now.astimezone(timezone.utc).replace(microsecond=0)
    return end - timedelta(seconds=since_seconds), end


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def log_filter(generation, name, region, service, start, end):
    """The Logging filter for the ERROR-and-above entries of the function between `start` and
    `end` (datetimes from `window()`)."""
    if generation == 2:
        resource = (
            'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service or name.lower()}" '
            f'AND resource.labels.location="{region}"'
        )
    else:
        resource = (
            'resource.type="cloud_function" '
            f'AND resource.labels.function_name="{name}" '
            f'AND resource.labels.region="{region}"'
        )
    return f'{resource} AND severity>=ERROR AND timestamp>="{stamp(start)}" AND timestamp<="{stamp(end)}"'


def read_entries(ctx, project, flt, limit, timeout, deadline=None):
    """Up to `limit` + 1 entries (the extra one tells the caller the list was cut), newest first."""
    rows, _ = json_call(
        ctx,
        [
            "logging",
            "read",
            flt,
            f"--project={project}",
            f"--limit={limit + 1}",
            "--order=desc",
        ],
        project,
        timeout=timeout,
        deadline=deadline,
    )
    if not isinstance(rows, list):
        raise RwError("internal", "gcloud logging read returned no list")
    return [r for r in rows if isinstance(r, dict)]


def _message(entry):
    """(text or None, which payload it came from)."""
    if isinstance(entry.get("textPayload"), str):
        return entry["textPayload"], "textPayload"
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        for key in JSON_KEYS:
            if isinstance(payload.get(key), str):
                return payload[key], "jsonPayload"
        return None, "jsonPayload"
    proto = entry.get("protoPayload")
    if isinstance(proto, dict):
        status = proto.get("status")
        if isinstance(status, dict) and isinstance(status.get("message"), str):
            return status["message"], "protoPayload"
        return None, "protoPayload"
    return None, None


def entry_facts(entry):
    """One entry as an output row."""
    text, source = _message(entry)
    message, cut = clip(text) if text is not None else (None, False)
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
    resource_labels = resource.get("labels") if isinstance(resource.get("labels"), dict) else {}
    request = entry.get("httpRequest") if isinstance(entry.get("httpRequest"), dict) else {}
    status = request.get("status")
    execution = labels.get("execution_id")
    revision = resource_labels.get("revision_name")
    return {
        "timestamp": entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None,
        "severity": entry.get("severity") if isinstance(entry.get("severity"), str) else None,
        "message": message,
        "messageTruncated": cut,
        "payload": source,
        "revision": revision[:MAX_ID] if isinstance(revision, str) else None,
        "executionId": execution[:MAX_ID] if isinstance(execution, str) else None,
        "httpStatus": status if isinstance(status, int) and not isinstance(status, bool) else None,
    }


def log_facts(flt, start, end, rows, limit):
    """The `logs` output from the entries read (at most limit + 1)."""
    truncated = len(rows) > limit
    entries = [entry_facts(r) for r in rows[:limit]]
    return {
        "window": {
            "start": stamp(start),
            "end": stamp(end),
            "seconds": int((end - start).total_seconds()),
        },
        "filter": flt,
        "order": "newest-first",
        "entries": entries,
        "returned": len(entries),
        "truncated": truncated,
        "messagesTruncated": any(e["messageTruncated"] for e in entries),
    }
