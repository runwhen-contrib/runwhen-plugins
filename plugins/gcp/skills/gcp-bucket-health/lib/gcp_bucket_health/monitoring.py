"""Bucket sizes from Cloud Monitoring (the Monitoring REST API, `timeSeries.list`).

`gcloud` has no verb that reads a metric, so this is the one place a task calls Google over REST,
with `curl` through ctx.run (so the call is recorded and replayed like every other):

* the access token comes from `gcloud auth print-access-token` (the connection's own credential),
  is held in memory and written to a 0600 header file in ctx.workdir for the length of the
  call: curl's argv holds only the file's name (`-H @<file>`), never the token, and the file is
  deleted in `finally`. The name is relative (curl runs in ctx.workdir) so that an argv, and so a
  recorded fixture, is the same on every run;
* one query per project, not per bucket: every bucket's series for the metric in one go (a
  `bucket` argument narrows it to one bucket's);
* the metric is `storage.googleapis.com/storage/v2/total_bytes`: bytes per bucket, storage class
  and `type` (live-object, noncurrent-object, soft-deleted-object, multipart-upload). The older
  `storage/total_bytes` has no `type` label and leaves out soft-deleted objects. Google measures it
  once a day and repeats the value every 5 minutes (not visible for up to 690 s), and does not
  track buckets with no objects: a bucket without a series is unknown here, not 0 bytes.

Docs: https://cloud.google.com/monitoring/api/metrics_gcp_p_z (storage/v2/total_bytes) and
https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list."""

import json
import math
import os
import re
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

from .errors import RwError
from .gcloud import classify_error, error_text, gcloud_text, valid_bucket

METRIC = "storage.googleapis.com/storage/v2/total_bytes"
ENDPOINT = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
LOOKBACK = timedelta(hours=6)  # the newest point of this window is the size
ALIGNMENT = "3600s"  # one point per hour (the maximum in it), so a series has at most 6 points
PAGE_SIZE = 1000
MAX_PAGES = 50
HEADER_FILE = ".gcp-monitoring-header"  # relative to ctx.workdir, where curl runs
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{10,4096}")
TEXT = 1024
MAX_TYPES = 50

# curl exit codes that mean "no answer from the server" (resolve, connect, TLS, send/recv)
_NO_ANSWER = {5, 6, 7, 35, 51, 52, 53, 54, 55, 56, 58, 59, 60, 77, 83}
_STATUS = re.compile(r"returned error: (\d{3})")
_BY_HTTP = {400: "invalid_input", 401: "permission_denied", 403: "permission_denied",
            404: "not_found", 429: "rate_limited"}  # fmt: skip


def access_token(ctx, timeout):
    """The connection's access token, from gcloud. A failure is classified like any gcloud
    failure; a token of an unexpected shape is `internal` (and is not echoed)."""
    token = gcloud_text(ctx, "auth", "print-access-token", timeout=timeout).strip()
    if not TOKEN.fullmatch(token):
        raise RwError("internal", "gcloud auth print-access-token printed no usable token")
    return token


def window(now):
    """(start, end) of the query, RFC 3339 UTC to the second, from the run's reference time."""
    end = now.replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (end - LOOKBACK).strftime(fmt), end.strftime(fmt)


def url(project, now, bucket=None, page_token=None):
    start, end = window(now)
    flt = (
        f'metric.type = "{METRIC}" AND resource.type = "gcs_bucket"'
        f' AND resource.labels.project_id = "{project}"'
    )
    if bucket is not None:
        flt += f' AND resource.labels.bucket_name = "{bucket}"'
    params = [
        ("filter", flt),
        ("interval.endTime", end),
        ("interval.startTime", start),
        ("aggregation.alignmentPeriod", ALIGNMENT),
        ("aggregation.perSeriesAligner", "ALIGN_MAX"),
        ("pageSize", str(PAGE_SIZE)),
    ]
    if page_token:
        params.append(("pageToken", page_token))
    return ENDPOINT.format(project=project) + "?" + urlencode(params, quote_via=quote)


def _failure(returncode, stdout, stderr):
    """The RwError for a failed curl: the API's own error body (curl keeps it with
    --fail-with-body) names the status and reason; the exit code covers no answer at all."""
    if returncode == 28:
        return RwError("timeout", "Cloud Monitoring did not answer in time")
    if returncode in _NO_ANSWER:
        return RwError("unreachable", "Cloud Monitoring could not be reached")
    status = reason = ""
    try:
        body = json.loads(stdout)
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            status = f"{err.get('status') or ''} {err.get('message') or ''}"
            for detail in err.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason += f" {detail['reason']}"
    except ValueError:
        pass
    text = f"{reason} {status} {stderr}"
    code = classify_error(text, returncode)
    http = _STATUS.search(stderr or "")
    if code == "internal" and http:
        code = _BY_HTTP.get(int(http.group(1)), "unreachable" if http.group(1)[0] == "5" else code)
    shown = " ".join(f"{status.strip()} {stderr.strip()}".split())
    return RwError(
        code, error_text(f"ERROR: Cloud Monitoring: {shown or f'curl exited {returncode}'}")
    )


def _write_header(ctx, token, project):
    path = os.path.join(os.fspath(ctx.workdir), HEADER_FILE)
    if os.path.lexists(path):
        os.unlink(path)  # a stale file of an earlier, killed run
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"Authorization: Bearer {token}\nX-Goog-User-Project: {project}\n")
    return path


def fetch(ctx, token, project, bucket, timeout, deadline, clock=time.monotonic):
    """Every time series of the query, page by page. A page that cannot be read raises; when the
    pages stop at the deadline or at MAX_PAGES the series so far are returned and the reason is
    an error scoped `monitoring` (the buckets without a series are then reported as such)."""
    header = _write_header(ctx, token, project)
    series, page_token = [], None
    try:
        for _ in range(MAX_PAGES):
            remaining = deadline - clock()
            if remaining < 5:
                ctx.error("timeout", "Cloud Monitoring: the task's time budget ran out between pages",
                          scope="monitoring")  # fmt: skip
                return series
            argv = ["curl", "-sS", "--fail-with-body", "--max-redirs", "0", "-H",
                    f"@{HEADER_FILE}", url(project, ctx.now(), bucket, page_token)]  # fmt: skip
            try:
                done = ctx.run(argv, timeout=min(timeout, remaining), check=False)
            except FileNotFoundError:
                raise RwError("dependency", "curl is not installed in this runtime") from None
            if done.returncode != 0:
                raise _failure(done.returncode, done.stdout or "", done.stderr or "")
            try:
                doc = json.loads(done.stdout or "")
            except ValueError:
                raise RwError("internal", "Cloud Monitoring did not return a JSON value") from None
            if not isinstance(doc, dict):
                raise RwError("internal", "Cloud Monitoring did not return a JSON object")
            series.extend(s for s in doc.get("timeSeries") or [] if isinstance(s, dict))
            page_token = doc.get("nextPageToken")
            if not page_token:
                return series
        ctx.error("internal", f"Cloud Monitoring has more than {MAX_PAGES} pages of series; "
                  "the sizes of the buckets on later pages are not read", scope="monitoring")  # fmt: skip
        return series
    finally:
        if os.path.lexists(header):
            os.unlink(header)


def _latest_value(points):
    """The value of the newest point, or None."""
    best, best_end = None, ""
    for point in points if isinstance(points, list) else []:
        if not isinstance(point, dict):
            continue
        end = str((point.get("interval") or {}).get("endTime") or "")
        value = point.get("value") or {}
        raw = value.get("doubleValue", value.get("int64Value"))
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0 and (best is None or end >= best_end):
            best, best_end = number, end
    return best


def sizes_by_bucket(series, project):
    """{bucket: {type: bytes}} from the series of a query: the newest point of each series,
    summed over the storage classes of a bucket and type."""
    out = {}
    for item in series:
        if not isinstance(item, dict):
            continue
        resource, metric = item.get("resource"), item.get("metric")
        labels = resource.get("labels") if isinstance(resource, dict) else None
        labels = labels if isinstance(labels, dict) else {}
        bucket = labels.get("bucket_name")
        if labels.get("project_id") != project or not valid_bucket(bucket):
            continue
        value = _latest_value(item.get("points"))
        if value is None:
            continue
        metric_labels = metric.get("labels") if isinstance(metric, dict) else None
        kind = metric_labels.get("type") if isinstance(metric_labels, dict) else None
        kind = kind[:TEXT] if isinstance(kind, str) and kind else "unspecified"
        types = out.setdefault(bucket, {})
        types[kind] = types.get(kind, 0.0) + value
    return {
        bucket: {k: int(round(v)) for k, v in sorted(types.items())[:MAX_TYPES]}
        for bucket, types in out.items()
    }
