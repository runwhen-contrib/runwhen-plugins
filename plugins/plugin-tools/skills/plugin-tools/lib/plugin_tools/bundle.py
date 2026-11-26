"""Reading a multi-plugin bundle: the registry's snapshot tar format (the SDK's archive.md
section 6).

    snapshot.json                         the manifest: plugins [{name, version, sha256, size}]
    index/<name>/<version>.json           index entries (ignored here: untrusted, rebuilt from the tree)
    runtimes.json                         runtimes (ignored here: runtimes come with the snapshot image)
    blobs/sha256/<hex>                    each archive, named by its sha256

`read_bundle` returns the listed plugins and their archive bytes. It trusts nothing: the tar is
walked header by header first (`prescan`: plain ustar members only, so no pax or GNU header can
make `tarfile` allocate what the header claims), every blob is hashed against its name and against
`snapshot.json`, and the caller rebuilds and validates each plugin from its tree like a git tag.
The format is the registry's, so a bundle an old `plugin-registry snapshot export` wrote and one
`plugin_tools.cli snapshot export` writes are both read.
"""

import io
import json
import re
import tarfile
from dataclasses import dataclass
from typing import Any

from plugin_tools.archive import (
    _BLOCK,
    E_HASH_MISMATCH,
    E_INVALID_PLUGIN,
    MAX_ARCHIVE_BYTES,
    ArchiveError,
    sha256_hex,
)
from plugin_tools.semver import parse_semver

API_VERSION = "registry.runwhen.com/snapshot/v1"
E_INVALID_SNAPSHOT = "E_INVALID_SNAPSHOT"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PLUGINS = 5000
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_INDEX = re.compile(r"^index/[a-z][a-z0-9-]{0,62}/[^/]+\.json$")


@dataclass(frozen=True)
class Listed:
    name: str
    version: str
    sha256: str  # bare hex
    size: int


def _invalid(detail: str) -> ArchiveError:
    return ArchiveError(E_INVALID_SNAPSHOT, detail)


def _chk_ok(header: bytes) -> bool:
    """The header checksum, as `tarfile` computes it (unsigned or signed sum, field as spaces)."""
    field = header[148:156].split(b"\0")[0].strip()
    try:
        stored = int(field or b"0", 8)
    except ValueError:
        return False
    rest = header[:148] + b" " * 8 + header[156:]
    return stored in (sum(rest), sum(b - 256 if b > 127 else b for b in rest))


def prescan(data: bytes) -> None:
    """Walk the raw tar's 512-byte headers before `tarfile` sees it.

    Anything but a regular file (typeflag `0` or NUL) is refused, as is a header whose checksum is
    wrong or whose size is not a plain octal number. Data blocks are skipped, never read. The walk
    ends at the first all-zero block, where `tarfile` stops reading too.
    """
    total, pos = len(data), 0
    while True:
        header = data[pos : pos + _BLOCK]
        if not header or header == bytes(_BLOCK):
            return
        if len(header) < _BLOCK:
            raise _invalid("the tar archive is truncated")
        if not _chk_ok(header):
            raise _invalid("not a readable tar archive")
        typeflag = header[156:157]
        if typeflag not in (b"0", b"\0"):
            raise _invalid(f"unsupported tar member type {typeflag!r}")
        field = header[124:136]
        try:
            if field[0] & 0x80:
                raise ValueError("base-256 size")
            size = int(field.split(b"\0")[0].strip() or b"0", 8)
        except ValueError:
            raise _invalid("not a readable tar archive") from None
        data_start = pos + _BLOCK
        if data_start + size > total:
            raise _invalid("the tar archive is truncated")
        pos = data_start + -(-size // _BLOCK) * _BLOCK


def is_bundle(tar_bytes: bytes) -> bool:
    """Whether the tar has `snapshot.json` at its root (a plugin tree never does)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
            for member in tf:
                if member.name.removeprefix("./") == "snapshot.json":
                    return True
    except tarfile.TarError:
        return False
    return False


def _parse_manifest(data: bytes) -> list[Listed]:
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        raise _invalid("snapshot.json is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("apiVersion") != API_VERSION:
        raise _invalid(f"snapshot.json apiVersion must be {API_VERSION!r}")
    plugins = doc.get("plugins")
    if not isinstance(plugins, list) or len(plugins) > MAX_PLUGINS:
        raise _invalid("snapshot.json plugins must be a list")
    listed: list[Listed] = []
    seen: set[tuple[str, str]] = set()
    for item in plugins:
        if not isinstance(item, dict):
            raise _invalid("snapshot.json plugins entries must be objects")
        name, version, sha, size = (item.get(k) for k in ("name", "version", "sha256", "size"))
        if not (isinstance(name, str) and _NAME.fullmatch(name)):
            raise _invalid(f"snapshot.json lists an invalid plugin name {str(name)[:80]!r}")
        try:
            parse_semver(version if isinstance(version, str) else "")
        except ValueError:
            raise _invalid(f"snapshot.json lists an invalid version for {name}") from None
        if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
            raise _invalid(f"snapshot.json lists an invalid sha256 for {name}@{version}")
        if not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
            raise _invalid(f"snapshot.json lists an invalid size for {name}@{version}")
        if (name, version) in seen:
            raise _invalid(f"snapshot.json lists {name}@{version} twice")
        seen.add((name, version))
        listed.append(Listed(name, version, sha, size))
    return listed


def read_bundle(tar_bytes: bytes) -> tuple[list[Listed], dict[str, bytes]]:
    """The listed plugins and the archive bytes by bare sha256, every blob verified.

    Raises ArchiveError (E_INVALID_SNAPSHOT, E_HASH_MISMATCH) for anything off. A listed plugin
    whose blob is missing is left for the caller to reject by name (it is not in `blobs`).
    """
    prescan(tar_bytes)
    manifest: bytes | None = None
    blobs: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
            for member in tf:
                if member.isdir():
                    continue
                path = member.name.removeprefix("./")
                if not member.isreg():
                    raise _invalid(f"{path[:80]!r}: only regular files are allowed")
                if path == "snapshot.json":
                    if member.size > MAX_MANIFEST_BYTES:
                        raise _invalid("snapshot.json is too large")
                    manifest = _read(tf, member)
                elif m := _BLOB.fullmatch(path):
                    if member.size > MAX_ARCHIVE_BYTES:
                        raise _invalid(
                            f"blob {m[1][:12]} is over the {MAX_ARCHIVE_BYTES} byte limit"
                        )
                    data = _read(tf, member)
                    if sha256_hex(data) != m[1]:
                        raise ArchiveError(
                            E_HASH_MISMATCH, f"blob {m[1][:12]} does not hash to its name"
                        )
                    blobs[m[1]] = data
                elif path == "runtimes.json" or _INDEX.fullmatch(path):
                    continue  # not used: rebuilt from the trees
                else:
                    raise _invalid(f"unexpected member {path[:80]!r}")
    except tarfile.TarError:
        raise _invalid("not a readable tar archive") from None
    if manifest is None:
        raise _invalid("snapshot.json is missing")
    return _parse_manifest(manifest), blobs


def _read(tf: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    fh = tf.extractfile(member)
    return fh.read() if fh is not None else b""


__all__: list[Any] = ["E_INVALID_PLUGIN", "Listed", "is_bundle", "prescan", "read_bundle"]
