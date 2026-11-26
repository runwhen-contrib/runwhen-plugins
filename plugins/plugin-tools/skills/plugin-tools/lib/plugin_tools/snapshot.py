"""Building the release snapshot: the registry's air-gap snapshot format (the SDK's archive.md
section 6).

Ported from the export side of the registry's `snapshot.py`. The registry exported from its
database; this builds the same bytes from prepared plugin versions (`prepare.Prepared`) read out of
git tags, so CI can bake the release snapshot image without a running registry:

    snapshot.json                         apiVersion registry.runwhen.com/snapshot/v1, createdAt,
                                          release, source, plugins [{name, version, sha256, size}],
                                          runtimes [{name, major, digests}]
    index/<name>/<version>.json           one index entry per version (canonical JSON)
    runtimes.json                         {"items": [runtime item, ...]} with upstream image refs
    blobs/sha256/<hex>                    each archive, named by its sha256

Members are plain ustar (mode 0644, uid/gid/mtime 0), in that order, written with the same header
rules as an archive (`UstarWriter`), so two exports of the same tags with the same `createdAt` and
`release` are byte-identical. The tar's own sha256 is what the platform release manifest records.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

from plugin_tools.archive import _BLOCK, _split_ustar
from plugin_tools.prepare import Prepared
from plugin_tools.runtimes import RuntimeSpec
from plugin_tools.semver import parse_semver, version_key

API_VERSION = "registry.runwhen.com/snapshot/v1"
SNAPSHOT_SOURCE = "public"

_RECORD = 10240
_CHUNK = 1024 * 1024
_MODE = 0o644


def _octal(value: int, width: int) -> bytes:
    return f"{value:0{width - 1}o}".encode("ascii") + b"\x00"


class UstarWriter:
    """Writes archive.md section 3 headers (regular files, mode 0644) to a binary stream.

    Entries are written in the order they are added; the caller owns the order. The stream is
    left open by `close`.
    """

    def __init__(self, fileobj: BinaryIO) -> None:
        self._out = fileobj
        self._written = 0

    def _write(self, data: bytes) -> None:
        self._out.write(data)
        self._written += len(data)

    def _header(self, path: str, size: int) -> bytes:
        split = _split_ustar(path.encode("utf-8"))
        if split is None:
            raise ValueError(f"path {path[:80]!r} cannot be split into a ustar name and prefix")
        prefix, name = split
        header = bytearray(_BLOCK)
        header[0 : len(name)] = name
        header[100:108] = _octal(_MODE, 8)
        header[108:116] = _octal(0, 8)  # uid
        header[116:124] = _octal(0, 8)  # gid
        header[124:136] = _octal(size, 12)
        header[136:148] = _octal(0, 12)  # mtime
        header[148:156] = b" " * 8  # checksum placeholder
        header[156:157] = b"0"
        header[257:263] = b"ustar\x00"
        header[263:265] = b"00"
        header[345 : 345 + len(prefix)] = prefix
        header[148:156] = f"{sum(header):06o}".encode("ascii") + b"\x00 "
        return bytes(header)

    def add(self, path: str, data: bytes | BinaryIO, size: int) -> None:
        """Add one file of exactly `size` bytes; a stream is read in 1 MiB chunks."""
        self._write(self._header(path, size))
        if isinstance(data, bytes):
            if len(data) != size:
                raise ValueError(f"{path!r}: {len(data)} bytes, expected {size}")
            self._write(data)
        else:
            remaining = size
            while remaining:
                chunk = data.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ValueError(f"{path!r}: stream ended {remaining} bytes short")
                self._write(chunk)
                remaining -= len(chunk)
            if data.read(1):
                raise ValueError(f"{path!r}: stream is longer than {size} bytes")
        self._write(bytes(-size % _BLOCK))

    def close(self) -> None:
        """Write the two end blocks, then pad to a multiple of 10240 bytes."""
        self._write(bytes(2 * _BLOCK))
        self._write(bytes(-self._written % _RECORD))


class _HashingWriter:
    """Forwards writes to `out` and hashes them."""

    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self._out.write(data)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SnapshotVersion:
    """One plugin version for the snapshot, with the provenance its index entry records."""

    prepared: Prepared
    source: dict[str, str]  # {kind, id, repo, path, tag, commit}


def index_entry(version: SnapshotVersion, ingested_at: datetime) -> dict:
    """The index entry (registry-api IndexEntry, camelCase) the registry served for a version."""
    p = version.prepared
    compiler = p.compiled.get("compiler")
    if isinstance(compiler, dict) and compiler.get("name") and compiler.get("version"):
        sdk_version = f"{compiler['name']}/{compiler['version']}"
    else:
        sdk_version = "unknown"
    return {
        "name": p.name,
        "version": p.version,
        "archive": {"sha256": p.sha256_ref, "size": p.size, "files": p.files},
        "source": dict(version.source),
        "requires": p.requires,
        "runtimes": p.runtimes,
        "ingestedAt": _iso(ingested_at),
        "withdrawn": None,
        "compiled": p.compiled,
        "sdkVersion": sdk_version,
    }


def current_runtimes(specs: Iterable[RuntimeSpec]) -> list[RuntimeSpec]:
    """The served runtime per (name, major), sorted by (name, major): the newest release, else
    the newest prerelease (the registry's `current_runtimes`)."""
    groups: dict[tuple[str, int], list[RuntimeSpec]] = {}
    for spec in specs:
        groups.setdefault((spec.name, spec.major), []).append(spec)
    out = []
    for key in sorted(groups):
        rows = groups[key]
        releases = [r for r in rows if parse_semver(r.version)[3] is None]
        out.append(max(releases or rows, key=lambda r: version_key(r.version)))
    return out


def runtime_item(spec: RuntimeSpec) -> dict:
    """The `/v1/runtimes` item shape with the upstream image (no mirror rules in a snapshot)."""
    return {
        "name": spec.name,
        "major": spec.major,
        "version": spec.version,
        "image": spec.image,
        "upstreamImage": spec.image,
        "digests": dict(spec.digests),
        "tools": dict(spec.tools),
        "mirrored": False,
    }


def export_snapshot(
    out: BinaryIO,
    versions: Iterable[SnapshotVersion],
    runtimes: Iterable[RuntimeSpec],
    *,
    release: str | None,
    now: datetime,
) -> str:
    """Write the snapshot tar to `out` and return its bare sha256 hex.

    `versions` are ordered by (name, semver ascending) here; `now` is `createdAt` and every
    entry's `ingestedAt`.
    """
    ordered = sorted(versions, key=lambda v: (v.prepared.name, version_key(v.prepared.version)))
    seen: set[tuple[str, str]] = set()
    for v in ordered:
        key = (v.prepared.name, v.prepared.version)
        if key in seen:
            raise ValueError(f"{key[0]}@{key[1]} is in the snapshot twice")
        seen.add(key)
    current = current_runtimes(runtimes)
    entries = [
        (
            f"index/{v.prepared.name}/{v.prepared.version}.json",
            canonical_json(index_entry(v, now)),
        )
        for v in ordered
    ]
    manifest = {
        "apiVersion": API_VERSION,
        "createdAt": _iso(now),
        "release": release,
        "source": SNAPSHOT_SOURCE,
        "plugins": [
            {
                "name": v.prepared.name,
                "version": v.prepared.version,
                "sha256": v.prepared.sha256,
                "size": v.prepared.size,
            }
            for v in ordered
        ],
        "runtimes": [
            {"name": rt.name, "major": rt.major, "digests": dict(rt.digests)} for rt in current
        ],
    }
    runtimes_doc = {"items": [runtime_item(rt) for rt in current]}

    sink = _HashingWriter(out)
    tar = UstarWriter(sink)  # type: ignore[arg-type]
    for path, data in [("snapshot.json", canonical_json(manifest)), *entries]:
        tar.add(path, data, len(data))
    data = canonical_json(runtimes_doc)
    tar.add("runtimes.json", data, len(data))
    blobs = {v.prepared.sha256: v.prepared.archive for v in ordered}
    for sha in sorted(blobs):
        tar.add(f"blobs/sha256/{sha}", blobs[sha], len(blobs[sha]))
    tar.close()
    return sink.digest.hexdigest()
