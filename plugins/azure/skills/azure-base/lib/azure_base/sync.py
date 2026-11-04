"""Push a recorded rw.discovery.v1 sync to the platform with the per-run `runwhen/sync`
credential: open, item batches, commit; abort when anything after open fails.

The credential is JSON, either inline in $RW_SYNC_CREDENTIAL (first non-space character `{`) or
in the file that variable names: {apiBaseUrl, token, workspace, expiresAt, scopes}. The token is
only ever sent as the Authorization header and never appears in an error message, a log line or
an output. The API is https only (RW_SYNC_ALLOW_HTTP=1 lifts that for tests), a redirect is an
error (it would carry the token elsewhere), and the sync API calls are open, items, commit and
abort, nothing else.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from azure_base.errors import RwError

TIMEOUT = 30
COMMIT_TIMEOUT = 300  # papi sweeps under the commit; it can take minutes on a large scope
BACKOFF = (1, 4, 10)  # seconds before retry 1, 2, 3
ATTEMPTS = len(BACKOFF) + 1
MAX_RETRY_AFTER = 60
_RETRYABLE = {429, 500, 502, 503, 504}
ALLOW_HTTP_ENV = "RW_SYNC_ALLOW_HTTP"


def load_credential(value):
    """The credential from $RW_SYNC_CREDENTIAL: inline JSON when the value starts with `{`
    (after whitespace), else the path of a JSON file. Neither the value nor the path nor the
    file content is ever put in the error."""
    try:
        text = value if value.lstrip().startswith("{") else Path(value).read_text()
        data = json.loads(text)
        return {k: data[k] for k in ("apiBaseUrl", "token", "workspace")}
    except (OSError, ValueError, KeyError, TypeError):
        raise RwError(
            "invalid_input", "RW_SYNC_CREDENTIAL is not a runwhen/sync credential"
        ) from None


def _code_for_status(status):
    if status in (401, 403):
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status in (400, 413, 422):
        return "invalid_input"
    if status == 408 or status == 504:
        return "timeout"
    return "internal"  # 409 included: D64 has no conflict code, the message says so


def _status_message(path, status, detail):
    if status == 409:
        return f"POST {path}: conflict (HTTP 409) {detail}"
    if 300 <= status < 400:
        return f"POST {path}: unexpected redirect (HTTP {status}); redirects are not followed"
    return f"POST {path}: HTTP {status} {detail}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx surfaces as an HTTPError


def default_open(request, timeout=None):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _retry_after(exc):
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
    try:
        return min(max(int(str(raw).strip()), 0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return 0


def _is_timeout(exc):
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        getattr(exc, "reason", None), (TimeoutError, socket.timeout)
    )


class SyncClient:
    def __init__(self, credential, opener=None, sleep=time.sleep):
        base = str(credential["apiBaseUrl"]).rstrip("/")
        if not base.startswith("https://") and os.environ.get(ALLOW_HTTP_ENV) != "1":
            raise RwError("invalid_input", "the sync credential's apiBaseUrl must be https")
        workspace = urllib.parse.quote(str(credential["workspace"]), safe="")
        self.base = f"{base}/api/v4/workspaces/{workspace}"
        self._token = credential["token"]
        self._open = opener
        self._sleep = sleep
        self.sent_partitions = None

    def _post(self, path, body, timeout=TIMEOUT, retry_timeout=True):
        data = json.dumps(body).encode("utf-8")
        last = None
        for attempt in range(ATTEMPTS):
            request = urllib.request.Request(
                f"{self.base}{path}",
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
            delay = BACKOFF[attempt] if attempt < len(BACKOFF) else 0
            try:
                with (self._open or default_open)(request, timeout=timeout) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", errors="replace")
                last = RwError(_code_for_status(exc.code), _status_message(path, exc.code, detail))
                if exc.code not in _RETRYABLE:
                    raise last from None
                delay = max(delay, _retry_after(exc))
            except (TimeoutError, socket.timeout):
                last = RwError("timeout", f"POST {path}: timed out after {timeout}s")
                if not retry_timeout:
                    last.no_abort = True
                    raise last from None
            except (urllib.error.URLError, OSError) as exc:
                if _is_timeout(exc):
                    last = RwError("timeout", f"POST {path}: timed out after {timeout}s")
                    if not retry_timeout:
                        last.no_abort = True
                        raise last from None
                else:
                    last = RwError("unreachable", f"POST {path}: {getattr(exc, 'reason', exc)}")
            except ValueError:
                raise RwError(
                    "internal", f"POST {path}: the platform answered with invalid JSON"
                ) from None
            if attempt < ATTEMPTS - 1:
                self._sleep(delay)
        raise last

    def push(self, doc):
        """Run the whole sync and return (commit result, rejected items). Items the platform
        rejected (unknown type, missing parent...) and batches it did not account for turn their
        (type, namespace) partitions into `failed` before commit, so nothing is swept for them.
        The partitions actually committed are kept in `sent_partitions`."""
        opened = self._post("/resource-syncs", doc["open"])
        sync_id = opened["syncId"]
        rejected = []
        short = {}
        try:
            for batch in doc["batches"]:
                items = batch["items"]
                result = self._post(f"/resource-syncs/{sync_id}/items", batch)
                refused = result.get("rejected") or []
                for entry in refused:
                    rejected.append((*_key(items[entry["index"]]), entry))
                accepted = result.get("accepted")
                if not isinstance(accepted, int) or accepted + len(refused) != len(items):
                    for item in items:
                        short[_key(item)] = "batch not accounted for"
            commit = dict(doc["commit"])
            commit["partitions"] = fail_rejected(commit["partitions"], rejected, short)
            self.sent_partitions = commit["partitions"]
            result = self._post(
                f"/resource-syncs/{sync_id}/commit",
                commit,
                timeout=COMMIT_TIMEOUT,
                retry_timeout=False,
            )
            return result, [r[2] for r in rejected]
        except Exception as exc:
            if not getattr(exc, "no_abort", False):
                # a commit that timed out may have succeeded: aborting it would be wrong
                try:
                    self._post(f"/resource-syncs/{sync_id}/abort", {"reason": str(exc)[:500]})
                except Exception:
                    pass  # the original failure is the one to report; the lease expires on its own
            raise


def _key(item):
    """(type, namespace or None) of the partition an item belongs to."""
    chain = item["identity"]["chain"]
    return chain[-1]["type"], chain[1]["name"] if len(chain) > 2 else None


def fail_rejected(partitions, rejected, short=None):
    """partitions with every complete one that had a rejected item, or an item in a batch the
    platform did not account for, marked failed."""
    reasons = {
        (t, ns): f"items rejected: {entry.get('code', 'rejected')}" for t, ns, entry in rejected
    }
    for key, why in (short or {}).items():
        reasons.setdefault(key, why)
    out = []
    for p in partitions:
        path = p.get("parentPath", "")
        ns = path.split("/namespaces/", 1)[1] if "/namespaces/" in path else None
        reason = reasons.get((p["type"], ns))
        if reason and p["status"] == "complete":
            p = {k: v for k, v in p.items() if k != "count"}
            p.update(status="failed", reason=reason)
        out.append(p)
    return out
