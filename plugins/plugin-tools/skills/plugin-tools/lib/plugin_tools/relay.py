"""The task's side of the runner relay's plugin routes (BUILD.md "Wire contract", DESIGN 2.5).

    PUT {relay}/v1/tasks/{id}/plugin-archives/{sha}      body = the archive bytes
        -> 200 {"exists": true}                          papi already stores that sha: nothing was sent
        -> 200 {"sha256", "size", "state": "stored"}     stored (the runner hashed it and papi completed it)
        -> 409 {"code": "E_ARCHIVE_CAP"}                 the run's per-run archive cap is reached
        -> 422 {"code": "E_HASH_MISMATCH"}               the bytes are not the declared sha
    GET {relay}/v1/tasks/{id}/plugin-uploads/{uploadId}  -> 200 the upload's bytes (size, sha256 checked
                                                            by the runner while streaming)

The base URL, the task id and the auth (bearer token, `X-RW-Dispatch`) are those of the SDK's
`RelayClient` (runwhen_capability/relay.py), which already builds the artifact routes: `base_url`,
`request_id` and `session` are public, `_url(suffix)` makes `{base}/v1/tasks/{id}/{suffix}` and
`_auth()` the headers (`task_headers`). This module adds the two plugin routes on top of the same
object and never touches a token itself. It is the only module that knows the relay exists:
tests hand it a real `RelayClient` pointed at a local fake relay.

NOTE (SDK 0.6.0): a task script runs in a child process that "never has the relay"
(_bundle_entrypoint.py), so `ctx.relay` is None there. `relay_from_ctx` says so with a `dependency`
error; the upload only works once the SDK hands the relay (or a host-side upload spool) to V13
task scripts. See the report that came with this plugin.
"""

import hashlib
import re
from typing import Any

import requests

from plugin_tools.archive import MAX_ARCHIVE_BYTES

_SHA_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Sending a body is bounded per send, waiting for the answer separately (the runner re-reads the
# object to hash it, which takes a while at 64 MiB): the SDK's artifact upload uses the same shape.
_SEND_TIMEOUT = 150
_RESPONSE_TIMEOUT = 600
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 150
_CHUNK = 1024 * 1024
_BODY_SNIPPET = 200

E_ARCHIVE_CAP = "E_ARCHIVE_CAP"
E_TRANSPORT = "E_RELAY_TRANSPORT"


class RelayError(Exception):
    """The relay refused or could not be reached. `code` is the relay's own code when it sent one,
    else `HTTP_<status>` or `E_RELAY_TRANSPORT`. The message never holds a token or a URL query."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def relay_from_ctx(ctx: Any) -> "PluginRelay":
    relay = getattr(ctx, "relay", None)
    if relay is None:
        from runwhen_capability.rwerrors import RwError

        raise RwError(
            "dependency",
            "this task has no runner relay to upload plugin archives to "
            "(the executor did not hand one to the task process)",
        )
    return PluginRelay(relay)


def _error_for(resp: requests.Response) -> RelayError:
    status = resp.status_code
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    body = parsed if isinstance(parsed, dict) else {}
    code = body.get("code") if isinstance(body.get("code"), str) else None
    message = body.get("message") if isinstance(body.get("message"), str) else None
    if message is None:
        message = "unexpected relay response"
        if parsed is None and resp.text.strip():
            message += f": {resp.text.strip()[:_BODY_SNIPPET]}"
    return RelayError(code or f"HTTP_{status}", f"{message} (HTTP {status})", status)


class PluginRelay:
    """`relay` is an SDK `RelayClient` (or anything with its `session`, `_url` and `_auth`)."""

    def __init__(self, relay: Any) -> None:
        self._relay = relay

    def put_archive(self, sha_ref: str, data: bytes) -> dict:
        """Upload `data` as the archive `sha_ref` (`sha256:<hex>`); returns the relay's answer.

        The hash is checked here first, so a bug that pairs the wrong bytes with a sha fails
        before anything is sent. `{"exists": True}` means nothing needed sending.
        """
        if not _SHA_REF.fullmatch(sha_ref):
            raise ValueError(f"not a sha256 reference: {sha_ref!r}")
        if not 1 <= len(data) <= MAX_ARCHIVE_BYTES:
            raise ValueError("an archive is between 1 byte and 64 MiB")
        if "sha256:" + hashlib.sha256(data).hexdigest() != sha_ref:
            raise ValueError("archive bytes do not match their sha256")
        try:
            resp = self._relay.session.put(
                self._relay._url(f"plugin-archives/{sha_ref}"),
                data=data,
                headers={**self._relay._auth(), "Content-Type": "application/x-tar"},
                timeout=(_SEND_TIMEOUT, _RESPONSE_TIMEOUT),
            )
        except (requests.RequestException, OSError) as exc:
            raise RelayError(
                E_TRANSPORT, f"uploading archive {sha_ref} failed: {type(exc).__name__}"
            ) from exc
        with resp:
            if resp.status_code not in (200, 201):
                raise _error_for(resp)
            try:
                out = resp.json()
            except ValueError:
                raise RelayError(
                    E_TRANSPORT, "the relay's answer is not JSON", resp.status_code
                ) from None
        if not isinstance(out, dict):
            raise RelayError(E_TRANSPORT, "the relay's answer is not an object", resp.status_code)
        return out

    def read_upload(self, upload_id: str, max_bytes: int) -> bytes:
        """The bytes of upload `upload_id`, refusing (before reading) one larger than `max_bytes`."""
        if not _UPLOAD_ID.fullmatch(upload_id):
            raise ValueError("uploadId has characters a route cannot carry")
        try:
            resp = self._relay.session.get(
                self._relay._url(f"plugin-uploads/{upload_id}"),
                headers=self._relay._auth(),
                stream=True,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except (requests.RequestException, OSError) as exc:
            raise RelayError(E_TRANSPORT, f"reading upload failed: {type(exc).__name__}") from exc
        with resp:
            if resp.status_code != 200:
                raise _error_for(resp)
            declared = resp.headers.get("Content-Length", "")
            if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
                raise RelayError("E_UPLOAD_TOO_LARGE", f"the upload is over {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in resp.iter_content(_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise RelayError(
                            "E_UPLOAD_TOO_LARGE", f"the upload is over {max_bytes} bytes"
                        )
                    chunks.append(chunk)
            except (requests.RequestException, OSError) as exc:
                raise RelayError(
                    E_TRANSPORT, f"reading upload failed: {type(exc).__name__}"
                ) from exc
            if declared.isascii() and declared.isdigit() and int(declared) != total:
                raise RelayError(E_TRANSPORT, "the upload ended short of its length")
            data = b"".join(chunks)
            expected = resp.headers.get("X-RW-Sha256")
            if expected and expected.removeprefix("sha256:") != hashlib.sha256(data).hexdigest():
                raise RelayError(
                    "E_HASH_MISMATCH", "the upload does not match the sha the relay sent"
                )
        return data
