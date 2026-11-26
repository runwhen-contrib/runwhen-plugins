"""Bounded readers for uploaded plugin trees and bundles (the input layer of `validate-upload`).

Ported from the registry's `tree_input.py`, minus the HTTP request readers and the work slot (the
task reads one upload from the relay and runs alone). Nothing here trusts the sender: a gzip layer
is inflated in bounded steps so a small bomb cannot allocate its whole output, and the tar is
parsed with `tarfile` but admits only plain files whose paths pass the archive's own rules. A bad
input raises `ArchiveError`.
"""

import io
import tarfile
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

from plugin_tools.archive import (
    DEFAULT_LIMITS,
    E_ARCHIVE_TOO_LARGE,
    E_INVALID_PLUGIN,
    ArchiveError,
    FileEntry,
    Limits,
    _check_collisions,
    _check_path,
)

_GZIP_MAGIC = b"\x1f\x8b"
_INFLATE_STEP = 1024 * 1024


def _too_large(limit: int) -> ArchiveError:
    return ArchiveError(E_ARCHIVE_TOO_LARGE, f"body is over the {limit} byte limit")


def _inflate(parts: Iterable[bytes], limit: int) -> Iterator[bytes]:
    """Inflate gzip `parts` in bounded steps, yielding at most `_INFLATE_STEP` bytes at a time.

    Raises E_ARCHIVE_TOO_LARGE once more than `limit` bytes have come out, and E_INVALID_PLUGIN
    for a corrupt or truncated stream. Nothing after the end of the first gzip member is read.
    """
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    total = 0
    try:
        for part in parts:
            pending = part
            while not inflater.eof:
                chunk = inflater.decompress(pending, _INFLATE_STEP)
                pending = inflater.unconsumed_tail
                total += len(chunk)
                if total > limit:
                    raise _too_large(limit)
                if chunk:
                    yield chunk
                # A full step may leave output inside zlib even with no input left.
                if not pending and len(chunk) < _INFLATE_STEP:
                    break
            if inflater.eof:
                return
    except zlib.error:
        raise ArchiveError(E_INVALID_PLUGIN, "not a valid gzip stream") from None
    if not inflater.eof:
        raise ArchiveError(E_INVALID_PLUGIN, "truncated gzip stream")


def maybe_gunzip(data: bytes, limit: int) -> bytes:
    """Inflate `data` if it is gzip, never producing more than `limit` bytes; else return it."""
    if data[:2] != _GZIP_MAGIC:
        return data
    return b"".join(_inflate([data], limit))


def is_gzip_file(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(2) == _GZIP_MAGIC


def gunzip_file(src: Path, dest: Path, limit: int) -> None:
    """Inflate the gzip file `src` into `dest`, never writing more than `limit` bytes.

    Neither file is held in memory.
    """

    def parts() -> Iterator[bytes]:
        with src.open("rb") as f:
            while chunk := f.read(_INFLATE_STEP):
                yield chunk

    with dest.open("wb") as out:
        for chunk in _inflate(parts(), limit):
            out.write(chunk)


def read_tree_tar(data: bytes, limits: Limits = DEFAULT_LIMITS) -> dict[str, FileEntry]:
    """Read a plain tar (`./`-prefixed names and directory members allowed) into a file tree.

    Only regular files are admitted. Paths are held to the archive rules, but the tree is
    returned as sent: excluded paths are dropped later by `normalize_tree`.
    """
    files: dict[str, FileEntry] = {}
    paths: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            for member in tf:
                if member.isdir():
                    continue
                path = member.name.removeprefix("./")
                if not member.isreg() or member.issparse():
                    raise ArchiveError(
                        E_INVALID_PLUGIN, f"{path[:80]!r}: only regular files are allowed"
                    )
                _check_path(path)
                if len(paths) >= limits.files:
                    raise ArchiveError(E_ARCHIVE_TOO_LARGE, f"more than {limits.files} files")
                if member.size > limits.file_bytes:
                    raise ArchiveError(
                        E_ARCHIVE_TOO_LARGE,
                        f"{path[:80]!r} is {member.size} bytes, over {limits.file_bytes}",
                    )
                fh = tf.extractfile(member)
                content = fh.read() if fh is not None else b""
                paths.append(path)
                files[path] = FileEntry(content, bool(member.mode & 0o111))
    except tarfile.TarError:
        raise ArchiveError(E_INVALID_PLUGIN, "not a tar archive") from None
    _check_collisions(paths)
    return files
