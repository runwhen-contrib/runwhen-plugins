"""Push a recorded rw.discovery.v1 sync to the platform with the per-run `runwhen/sync`
credential: open, item batches, commit; abort when anything after open fails.

The credential is JSON in the file at $RW_SYNC_CREDENTIAL ({apiBaseUrl, token, workspace,
expiresAt, scopes}). The token is only ever sent as the Authorization header and never appears
in an error message, a log line or an output.
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from k8s_base.errors import RwError

TIMEOUT = 30
ATTEMPTS = 3
_RETRYABLE = {500, 502, 503, 504}


def load_credential(path):
    try:
        data = json.loads(Path(path).read_text())
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
    if status in (400, 422):
        return "invalid_input"
    if status == 408 or status == 504:
        return "timeout"
    return "internal"


class SyncClient:
    def __init__(self, credential, opener=None, sleep=time.sleep):
        self.base = (
            f"{credential['apiBaseUrl'].rstrip('/')}/api/v4/workspaces/{credential['workspace']}"
        )
        self._token = credential["token"]
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    def _post(self, path, body):
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
            try:
                with self._open(request, timeout=TIMEOUT) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", errors="replace")
                last = RwError(_code_for_status(exc.code), f"POST {path}: HTTP {exc.code} {detail}")
                if exc.code not in _RETRYABLE:
                    raise last from None
            except TimeoutError:
                last = RwError("timeout", f"POST {path}: timed out after {TIMEOUT}s")
            except (urllib.error.URLError, OSError) as exc:
                last = RwError("unreachable", f"POST {path}: {getattr(exc, 'reason', exc)}")
            except ValueError:
                raise RwError(
                    "internal", f"POST {path}: the platform answered with invalid JSON"
                ) from None
            if attempt < ATTEMPTS - 1:
                self._sleep(2**attempt)
        raise last

    def push(self, doc):
        """Run the whole sync and return (commit result, rejected items). Items the platform
        rejected (unknown type, missing parent...) turn their (type, namespace) partition into
        `failed` before commit, so nothing is swept for it."""
        opened = self._post("/resource-syncs", doc["open"])
        sync_id = opened["syncId"]
        rejected = []
        try:
            for batch in doc["batches"]:
                result = self._post(f"/resource-syncs/{sync_id}/items", batch)
                for entry in result.get("rejected") or []:
                    chain = batch["items"][entry["index"]]["identity"]["chain"]
                    rejected.append(
                        (chain[-1]["type"], chain[1]["name"] if len(chain) > 2 else None, entry)
                    )
            commit = dict(doc["commit"])
            commit["partitions"] = fail_rejected(commit["partitions"], rejected)
            return self._post(f"/resource-syncs/{sync_id}/commit", commit), [r[2] for r in rejected]
        except RwError as exc:
            try:
                self._post(f"/resource-syncs/{sync_id}/abort", {"reason": exc.message[:500]})
            except RwError:
                pass  # the original failure is the one to report; the lease expires on its own
            raise


def fail_rejected(partitions, rejected):
    """partitions with every complete one that had a rejected item marked failed."""
    keys = {(t, ns): entry.get("code", "rejected") for t, ns, entry in rejected}
    out = []
    for p in partitions:
        path = p.get("parentPath", "")
        ns = path.split("/namespaces/", 1)[1] if "/namespaces/" in path else None
        code = keys.get((p["type"], ns))
        if code and p["status"] == "complete":
            p = {k: v for k, v in p.items() if k != "count"}
            p.update(status="failed", reason=f"items rejected: {code}")
        out.append(p)
    return out
