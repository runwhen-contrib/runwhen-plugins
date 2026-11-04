"""Function metrics from Cloud Monitoring (the Monitoring REST API, `timeSeries.list`).

`gcloud` has no verb that reads a metric, so this is the one place a task calls Google over REST,
with `curl` through ctx.run (so the call is recorded and replayed like every other), the same
pattern as gcp-bucket-health's bucket sizes:

* the access token comes from `gcloud auth print-access-token` (the connection's own credential),
  is held in memory and written to a 0600 header file in ctx.workdir for the length of the call:
  curl's argv holds only the file's name (`-H @<file>`), never the token, and the file is deleted
  in `finally`. The name is relative (curl runs in ctx.workdir) so that an argv, and so a recorded
  fixture, is the same on every run;
* each query is one function's series in one window, aggregated by Monitoring itself
  (`ALIGN_DELTA` over the whole window, then a cross-series reducer), never read point by point.

Docs: https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list and
https://cloud.google.com/monitoring/api/metrics_gcp_c (cloudfunctions.googleapis.com/function/...),
https://cloud.google.com/monitoring/api/metrics_gcp_p_z (run.googleapis.com/...)."""

import json
import os
import re
import time
from urllib.parse import quote, urlencode

from .errors import RwError
from .gcloud import classify_error, error_text, gcloud_text

ENDPOINT = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
PAGE_SIZE = 1000
MAX_PAGES = 10
HEADER_FILE = ".gcp-monitoring-header"  # relative to ctx.workdir, where curl runs
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{10,4096}")

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


def url(project, start, end, flt, aggregation, page_token=None):
    """The timeSeries.list URL: `start` and `end` are RFC 3339 UTC strings, `aggregation` a list
    of (field, value) pairs."""
    params = [
        ("filter", flt),
        ("interval.endTime", end),
        ("interval.startTime", start),
        *[(f"aggregation.{k}", v) for k, v in aggregation],
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


def write_header(ctx, token, project):
    path = os.path.join(os.fspath(ctx.workdir), HEADER_FILE)
    if os.path.lexists(path):
        os.unlink(path)  # a stale file of an earlier, killed run
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"Authorization: Bearer {token}\nX-Goog-User-Project: {project}\n")
    return path


class Reader:
    """Reads queries with one token and one header file for the length of the run; `close()`
    deletes the file."""

    def __init__(self, ctx, token, project, timeout, deadline, clock=time.monotonic):
        self.ctx, self.project, self.timeout = ctx, project, timeout
        self.deadline, self.clock = deadline, clock
        self.header = write_header(ctx, token, project)

    def close(self):
        if os.path.lexists(self.header):
            os.unlink(self.header)

    def series(self, start, end, flt, aggregation):
        """Every time series of the query, page by page. A page that cannot be read raises, and
        so does a budget that runs out before the first page; one that runs out later, or more
        than MAX_PAGES pages, would return a part as if it were the whole, so it raises too."""
        out, page_token = [], None
        for _ in range(MAX_PAGES):
            remaining = self.deadline - self.clock()
            if remaining < 5:
                raise RwError("timeout", "Cloud Monitoring: the task's time budget ran out")
            argv = ["curl", "-sS", "--fail-with-body", "--max-redirs", "0", "-H",
                    f"@{HEADER_FILE}", url(self.project, start, end, flt, aggregation, page_token)]  # fmt: skip
            try:
                done = self.ctx.run(argv, timeout=min(self.timeout, remaining), check=False)
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
            out.extend(s for s in doc.get("timeSeries") or [] if isinstance(s, dict))
            page_token = doc.get("nextPageToken")
            if not page_token:
                return out
        raise RwError("internal", f"Cloud Monitoring has more than {MAX_PAGES} pages of series")
