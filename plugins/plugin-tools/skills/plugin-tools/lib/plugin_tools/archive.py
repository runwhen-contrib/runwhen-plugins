"""Deterministic plugin archive (docs/archive.md section 2-5).

Ported from the plugin registry's `archive.py`; the one addition is `Limits`, so a sync task's
`limits` input can lower the 64 MiB / 10 000 files / 16 MiB caps for one run.

One plugin version is one uncompressed POSIX ustar tar whose bytes are fully determined by the
file tree. The sha256 of those bytes is the identity every consumer verifies, so the writer must be
byte-for-byte reproducible and the reader must not trust the tar even when the hash matched.
"""

import hashlib
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from runwhen_capability.contracts import archive as sdk_archive

E_INVALID_PLUGIN = "E_INVALID_PLUGIN"
E_BAD_REQUEST = "E_BAD_REQUEST"
E_ARCHIVE_TOO_LARGE = "E_ARCHIVE_TOO_LARGE"
E_HASH_MISMATCH = "E_HASH_MISMATCH"

# archive.md section 4.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10_000
MAX_FILE_BYTES = 16 * 1024 * 1024

_TESTS_DIR = "tests" + "/"
MAX_PATH_BYTES = 255
_USTAR_NAME_BYTES = 100
_USTAR_PREFIX_BYTES = 155
_BLOCK = 512
_MODE_PLAIN = 0o644
_MODE_EXECUTABLE = 0o755
_SHA256_PREFIX = "sha256:"


@dataclass(frozen=True)
class Limits:
    """The caps one run applies; the defaults are archive.md section 4 and are also the maximum."""

    archive_bytes: int = MAX_ARCHIVE_BYTES
    files: int = MAX_FILES
    file_bytes: int = MAX_FILE_BYTES

    def __post_init__(self) -> None:
        for name, value, ceiling in (
            ("archiveBytes", self.archive_bytes, MAX_ARCHIVE_BYTES),
            ("files", self.files, MAX_FILES),
            ("fileBytes", self.file_bytes, MAX_FILE_BYTES),
        ):
            if not 1 <= value <= ceiling:
                raise ValueError(f"limits.{name} must be between 1 and {ceiling}")


DEFAULT_LIMITS = Limits()


class ArchiveError(Exception):
    """A tree or archive was rejected. `code` is one of the E_* constants above."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class FileEntry:
    data: bytes
    executable: bool = False


def sha256_hex(data: bytes) -> str:
    """Bare 64 lowercase hex: the form used in the DB, blob paths and URL path params."""
    return hashlib.sha256(data).hexdigest()


def sha256_ref(hex_: str) -> str:
    """The `sha256:<hex>` form used in every JSON document."""
    return _SHA256_PREFIX + hex_


def _from_sdk(exc: sdk_archive.ArchiveError) -> ArchiveError:
    # The SDK prefixes its message with the code; ours adds it back in __str__.
    return ArchiveError(exc.code, str(exc).removeprefix(f"{exc.code}: "))


def _invalid(detail: str) -> ArchiveError:
    return ArchiveError(E_INVALID_PLUGIN, detail)


def _too_large(detail: str) -> ArchiveError:
    return ArchiveError(E_ARCHIVE_TOO_LARGE, detail)


def _split_ustar(raw: bytes) -> tuple[bytes, bytes] | None:
    """Split a path into (prefix, name) per archive.md section 3.2, or None if it cannot be.

    A path of at most 100 bytes is not split. Longer ones are split at the last `/` whose prefix
    fits in 155 bytes (the longest prefix that fits), which leaves the shortest possible name.
    """
    if len(raw) <= _USTAR_NAME_BYTES:
        return b"", raw
    i = raw.rfind(b"/", 0, _USTAR_PREFIX_BYTES + 1)
    if i <= 0:
        return None
    prefix, name = raw[:i], raw[i + 1 :]
    if not name or len(name) > _USTAR_NAME_BYTES:
        return None
    return prefix, name


def _check_path(path: object) -> None:
    """Enforce the per-path rules of archive.md section 2 (raises E_INVALID_PLUGIN)."""
    if not isinstance(path, str) or not path:
        raise _invalid("empty or non-string path")
    shown = repr(path[:80])
    try:
        raw = path.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"path {shown} is not valid UTF-8") from None
    if len(raw) > MAX_PATH_BYTES:
        raise _invalid(f"path {shown} exceeds {MAX_PATH_BYTES} bytes")
    if "\\" in path:
        raise _invalid(f"path {shown} contains a backslash")
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        raise _invalid(f"path {shown} contains a NUL or control character")
    if not unicodedata.is_normalized("NFC", path):
        raise _invalid(f"path {shown} is not NFC-normalised")
    if path.startswith("/"):
        raise _invalid(f"path {shown} is absolute")
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        raise _invalid(f"path {shown} has an empty, '.' or '..' segment")
    if _split_ustar(raw) is None:
        raise _invalid(f"path {shown} cannot be split into a ustar name and prefix")


def _check_collisions(paths: list[str]) -> None:
    """No two paths may differ only by case; no path may be both a file and a directory."""
    seen: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        if key in seen:
            raise _invalid(
                f"paths {seen[path.casefold()]!r} and {path!r} collide case-insensitively"
            )
        seen[key] = path
    for path in paths:
        parts = path.casefold().split("/")
        for n in range(1, len(parts)):
            parent = "/".join(parts[:n])
            if parent in seen:
                raise _invalid(f"{seen[parent]!r} is both a file and a directory")


def is_excluded(path: str) -> bool:
    """True for the paths archive.md section 2 leaves out.

    These are the root tests directory, root `runwhen.lock`, root `.runwhen/` and each skill's tests directory
    (recorded fixtures), each with everything under it. `skills/x/runwhen.lock`,
    `skills/x/.runwhen/`, a file named `skills/x/tests` and anything in a deeper `tests` directory
    are ordinary files.
    """
    if path.startswith((_TESTS_DIR, ".runwhen/")) or path == "runwhen.lock":
        return True
    parts = path.split("/", 3)
    return (
        len(parts) == 4
        and parts[0] == "skills"
        and parts[1] not in ("", ".", "..")
        and parts[2] == "tests"
        and parts[3] != ""
    )


def normalize_tree(
    files: Mapping[str, FileEntry], limits: Limits = DEFAULT_LIMITS
) -> dict[str, FileEntry]:
    """Apply archive.md section 2 and 4 to a file tree; returns it sorted by UTF-8 path bytes.

    The root tests directory, `runwhen.lock`, `.runwhen/` and each skill's tests directory are dropped (not
    rejected) before anything else is checked, so a recorded fixture with an odd name never blocks
    a plugin whose archive would not contain it.
    """
    kept = {p: e for p, e in files.items() if not (isinstance(p, str) and is_excluded(p))}
    if len(kept) > limits.files:
        raise _too_large(f"{len(kept)} files exceeds the {limits.files} file limit")
    for path, entry in kept.items():
        _check_path(path)
        if len(entry.data) > limits.file_bytes:
            raise _too_large(f"{path!r} is {len(entry.data)} bytes, over {limits.file_bytes}")
    _check_collisions(list(kept))
    return {p: kept[p] for p in sorted(kept, key=lambda p: p.encode("utf-8"))}


def build_archive(files: Mapping[str, FileEntry], limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Build the archive.md section 3 bytes for a tree (normalising it first).

    Normalisation and the limits are this module's; the byte production is the SDK's reference
    writer (`runwhen_capability.contracts.archive.build`), pinned by the contracts-v1 vectors.
    """
    tree = normalize_tree(files, limits)
    entries = [sdk_archive.TreeEntry(p, e.data, e.executable) for p, e in tree.items()]
    try:
        out = sdk_archive.build(entries)
    except sdk_archive.ArchiveError as exc:
        raise _from_sdk(exc) from exc
    if len(out) > limits.archive_bytes:
        raise _too_large(f"archive is {len(out)} bytes, over {limits.archive_bytes}")
    return out


def _verify_hash(data: bytes, expected_sha256: str) -> None:
    expected = expected_sha256.removeprefix(_SHA256_PREFIX)
    actual = sha256_hex(data)
    if actual != expected:
        raise ArchiveError(E_HASH_MISMATCH, f"expected sha256 {expected}, got {actual}")


def read_archive(
    data: bytes, expected_sha256: str | None = None, limits: Limits = DEFAULT_LIMITS
) -> dict[str, FileEntry]:
    """Verify (optionally) and parse an archive by archive.md section 5 rules.

    Parsing is the SDK reader's (`runwhen_capability.contracts.archive.read`): plain ustar headers
    only, so pax, GNU and sparse extension headers, symlinks, hardlinks and devices are rejected
    rather than folded into the next member, and unsafe or case-colliding paths are refused.
    This module adds the hash check before the tar is looked at, the size cap before hashing, a
    file/directory collision check, and this module's own file-count and per-file limits.
    `expected_sha256` may be bare hex or `sha256:<hex>`.
    The returned tree is exactly what was in the archive (root excluded paths are not dropped).
    """
    if len(data) > limits.archive_bytes:
        raise _too_large(f"archive is {len(data)} bytes, over {limits.archive_bytes}")
    if expected_sha256 is not None:
        _verify_hash(data, expected_sha256)
    try:
        entries = sdk_archive.read(data)
    except sdk_archive.ArchiveError as exc:
        raise _from_sdk(exc) from exc
    if len(entries) > limits.files:
        raise _too_large(f"more than {limits.files} files")
    files: dict[str, FileEntry] = {}
    for entry in entries:
        if len(entry.data) > limits.file_bytes:
            raise _too_large(f"{entry.path[:80]!r} is {len(entry.data)} bytes")
        files[entry.path] = FileEntry(entry.data, entry.executable)
    _check_collisions(list(files))
    return files


def _mkdirs(root: Path, parts: list[str]) -> Path:
    """Create `parts` under `root` one level at a time, refusing to traverse a symlink."""
    cur = root
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise _invalid(f"refusing to extract through symlink {cur.name!r}")
        if not cur.exists():
            cur.mkdir()
        elif not cur.is_dir():
            raise _invalid(f"{cur.name!r} exists and is not a directory")
    return cur


def safe_extract(data: bytes, dest: Path, expected_sha256: str) -> None:
    """Verify, fully validate, then write the archive's files under `dest`.

    Nothing is written unless the hash matched and every member passed the section 5 rules.
    Files are created exclusively (never over an existing file or through a symlink) with mode
    0644/0755 regardless of umask. `dest` should be a fresh or temporary directory.
    """
    files = read_archive(data, expected_sha256)
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    for path, entry in files.items():
        *dirs, leaf = path.split("/")
        parent = _mkdirs(root, dirs)
        mode = _MODE_EXECUTABLE if entry.executable else _MODE_PLAIN
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(parent / leaf, flags, 0o600)
        except OSError as exc:
            raise _invalid(f"cannot create {path!r}: {exc.strerror}") from exc
        with os.fdopen(fd, "wb") as fh:
            fh.write(entry.data)
            os.fchmod(fh.fileno(), mode)
