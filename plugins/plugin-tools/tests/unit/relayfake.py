"""A local HTTP server standing in for the runner relay's plugin routes (BUILD.md wire contract).

It speaks what the executor-facing relay answers: PUT plugin-archives/{sha} (200 exists | 200 stored
| 409 E_ARCHIVE_CAP | 422 E_HASH_MISMATCH) and GET plugin-uploads/{id} (200 bytes | 404). The client
under test is the SDK's real `RelayClient`, so the URL, the bearer token and X-RW-Dispatch are the
real ones. Loopback only: no network.
"""

import hashlib
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from runwhen_capability.relay import RelayClient

REQUEST_ID = "req1"
TOKEN = "relay-token"
DISPATCH = "42"
# `X-RW-Dispatch` (SDK >= 0.5) is checked only when the installed RelayClient can send it: the SDK
# CI pins (0.4.x) has none.
SENDS_DISPATCH = "dispatch" in inspect.signature(RelayClient.__init__).parameters


class FakeRelay:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}  # "sha256:<hex>" -> bytes
        self.uploads: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.cap: int | None = None  # E_ARCHIVE_CAP once this many archives were stored
        self.mismatch: set[str] = set()  # shas answered 422 E_HASH_MISMATCH
        self.status_for: dict[str, tuple[int, dict]] = {}  # sha -> forced (status, json)
        self.upload_headers: dict[str, dict[str, str]] = {}
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth(self):
                ok = self.headers.get("Authorization") == f"Bearer {TOKEN}" and (
                    not SENDS_DISPATCH or self.headers.get("X-RW-Dispatch") == DISPATCH
                )
                if not ok:
                    self._json(401, {"code": "E_UNAUTHORIZED"})
                return ok

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                relay.requests.append({"method": "PUT", "path": self.path, "size": len(body)})
                if not self._auth():
                    return
                prefix = f"/v1/tasks/{REQUEST_ID}/plugin-archives/"
                if not self.path.startswith(prefix):
                    return self._json(404, {})
                sha = self.path[len(prefix) :]
                if sha in relay.status_for:
                    status, body_json = relay.status_for[sha]
                    return self._json(status, body_json)
                if sha in relay.stored:
                    return self._json(200, {"exists": True})
                if sha in relay.mismatch or "sha256:" + hashlib.sha256(body).hexdigest() != sha:
                    return self._json(422, {"code": "E_HASH_MISMATCH", "message": "bad bytes"})
                if relay.cap is not None and len(relay.stored) >= relay.cap:
                    return self._json(409, {"code": "E_ARCHIVE_CAP", "message": "per-run cap"})
                relay.stored[sha] = body
                self._json(200, {"sha256": sha, "size": len(body), "state": "stored"})

            def do_GET(self):
                relay.requests.append({"method": "GET", "path": self.path})
                if not self._auth():
                    return
                prefix = f"/v1/tasks/{REQUEST_ID}/plugin-uploads/"
                upload = self.path[len(prefix) :] if self.path.startswith(prefix) else None
                if upload not in relay.uploads:
                    return self._json(404, {"code": "E_PLUGIN_NOT_FOUND"})
                data = relay.uploads[upload]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-RW-Sha256", "sha256:" + hashlib.sha256(data).hexdigest())
                for k, v in relay.upload_headers.get(upload, {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeRelay":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def client(self) -> RelayClient:
        extra = {"dispatch": DISPATCH} if SENDS_DISPATCH else {}
        return RelayClient(self.url, None, REQUEST_ID, requests.Session(), token=TOKEN, **extra)

    def shas(self) -> list[str]:
        return sorted(self.stored)
