"""The sync/validate-upload result: schema `plugins.runwhen.com/sync-result/v1` (DESIGN 2.3).

The result is capped at 1 MiB. `Manifest` serialises exactly what the task would emit, so the cap
is checked on the real bytes: an item that would push it over is not added, `more` is set, and
the caller stops (the next run continues, the tags already in the manifest are `known` by then).
"""

import json
from datetime import datetime
from typing import Any

API_VERSION = "plugins.runwhen.com/sync-result/v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 500


def _size(doc: dict) -> int:
    return len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def now_string(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


class Manifest:
    def __init__(
        self,
        *,
        tools_version: str,
        listed_at: str,
        source_id: str | None = None,
        upload_id: str | None = None,
    ) -> None:
        if (source_id is None) == (upload_id is None):
            raise ValueError("exactly one of source_id and upload_id")
        self.doc: dict[str, Any] = {"apiVersion": API_VERSION}
        if source_id is not None:
            self.doc["sourceId"] = source_id
        else:
            self.doc["uploadId"] = upload_id
        self.doc.update(
            listedAt=listed_at,
            toolsVersion=tools_version,
            versions=[],
            rejected=[],
            runtimes=[],
            more=False,
        )

    def fits(self, key: str, item: dict) -> bool:
        """Whether `item` can be added to `key` without the result passing the cap."""
        self.doc[key].append(item)
        try:
            return _size(self.doc) <= MAX_MANIFEST_BYTES
        finally:
            self.doc[key].pop()

    def add(self, key: str, item: dict) -> bool:
        """Add `item` to `versions`, `rejected` or `runtimes`; False (and `more` set) when it
        would not fit. The caller stops on False."""
        if not self.fits(key, item):
            self.doc["more"] = True
            return False
        self.doc[key].append(item)
        return True

    def set_more(self) -> None:
        self.doc["more"] = True

    @property
    def more(self) -> bool:
        return self.doc["more"]

    def finish(self) -> dict:
        if _size(self.doc) > MAX_MANIFEST_BYTES:  # unreachable by construction; refuse anyway
            raise ValueError("the manifest is over the 1 MiB cap")
        return self.doc


def short(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"


def rejected_item(code: str, message: str, *, tag: str | None, commit: str | None) -> dict:
    return {"tag": tag, "commit": commit, "code": code, "message": short(message)}


def version_item(prepared, *, tag: str | None, commit: str | None) -> dict:
    return {
        "name": prepared.name,
        "version": prepared.version,
        "tag": tag,
        "commit": commit,
        "archive": {
            "sha256": prepared.sha256_ref,
            "size": prepared.size,
            "files": prepared.files,
        },
        "compiled": prepared.compiled,
        "requires": prepared.requires,
        "runtimes": prepared.runtimes,
        "sdkVersion": prepared.sdk_version,
    }


def runtime_item(spec) -> dict:
    return {
        "name": spec.name,
        "major": spec.major,
        "version": spec.version,
        "image": spec.image,
        "digests": dict(spec.digests),
        "tools": dict(spec.tools),
    }
