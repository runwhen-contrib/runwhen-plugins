import gzip
import io
import tarfile
import time
import zlib

import pytest
from plugin_tools.archive import MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_FILES, ArchiveError
from plugin_tools.tree_input import gunzip_file, is_gzip_file, maybe_gunzip, read_tree_tar


def _tar(*members: tuple[str, bytes, bytes], mode: int = 0o644) -> bytes:
    """Build a tar from (name, typeflag, content); links get linkname 'target'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, typeflag, content in members:
            info = tarfile.TarInfo(name)
            info.type = typeflag
            info.mode = mode
            if typeflag in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "target"
            if typeflag == tarfile.REGTYPE:
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            else:
                tf.addfile(info)
    return buf.getvalue()


def _file(name: str, content: bytes = b"x") -> tuple[str, bytes, bytes]:
    return (name, tarfile.REGTYPE, content)


def test_reads_dot_prefixed_tar_with_dirs():
    data = _tar(
        ("./", tarfile.DIRTYPE, b""),
        ("./skills", tarfile.DIRTYPE, b""),
        _file("./plugin.yaml", b"name: x\n"),
        _file("./skills/a/SKILL.md", b"---\n"),
    )
    files = read_tree_tar(data)
    assert sorted(files) == ["plugin.yaml", "skills/a/SKILL.md"]
    assert files["plugin.yaml"].data == b"name: x\n"


def test_exec_bit_preserved():
    plain = read_tree_tar(_tar(_file("run.sh", b"#!/bin/sh\n")))
    executable = read_tree_tar(_tar(_file("run.sh", b"#!/bin/sh\n"), mode=0o755))
    assert plain["run.sh"].executable is False
    assert executable["run.sh"].executable is True


@pytest.mark.parametrize("typeflag", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_non_regular_members_rejected(typeflag):
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(("link", typeflag, b"")))
    assert exc.value.code == "E_INVALID_PLUGIN"
    assert "only regular files are allowed" in exc.value.detail


def test_symlink_rejected():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(_file("a"), ("l", tarfile.SYMTYPE, b"")))
    assert exc.value.code == "E_INVALID_PLUGIN"
    assert exc.value.detail == "'l': only regular files are allowed"


def test_hardlink_rejected():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(_file("a"), ("h", tarfile.LNKTYPE, b"")))
    assert exc.value.code == "E_INVALID_PLUGIN"


def test_dotdot_rejected():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(_file("../evil")))
    assert exc.value.code == "E_INVALID_PLUGIN"
    with pytest.raises(ArchiveError):
        read_tree_tar(_tar(_file("a/../../evil")))


def test_absolute_path_rejected():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(_file("/etc/passwd")))
    assert exc.value.code == "E_INVALID_PLUGIN"


def test_duplicate_path_rejected():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(_tar(_file("a.txt", b"1"), _file("./a.txt", b"2")))
    assert exc.value.code == "E_INVALID_PLUGIN"


def test_too_many_files_413_code():
    data = _tar(*(_file(f"f{i}", b"") for i in range(MAX_FILES + 1)))
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(data)
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"


def test_file_over_16mib_413_code():
    data = _tar(_file("big.bin", b"\0" * (MAX_FILE_BYTES + 1)))
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(data)
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"


def test_not_a_tar_422_code():
    with pytest.raises(ArchiveError) as exc:
        read_tree_tar(b"this is definitely not a tar archive" * 40)
    assert exc.value.code == "E_INVALID_PLUGIN"
    assert exc.value.detail == "not a tar archive"


def test_gunzip_round_trip_and_passthrough():
    payload = b"hello" * 1000
    assert maybe_gunzip(gzip.compress(payload), MAX_ARCHIVE_BYTES) == payload
    assert maybe_gunzip(payload, 10) == payload


def test_upload_gzip_bomb_413(monkeypatch):
    comp = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    zeros = b"\0" * (1024 * 1024)
    bomb = b"".join(comp.compress(zeros) for _ in range(200)) + comp.flush()
    assert len(bomb) < 1024 * 1024

    max_lengths: list[int] = []
    real = zlib.decompressobj

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def decompress(self, data, max_length=0):
            max_lengths.append(max_length)
            return self._inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(zlib, "decompressobj", lambda *a, **k: _Spy(real(*a, **k)))
    started = time.monotonic()
    with pytest.raises(ArchiveError) as exc:
        maybe_gunzip(bomb, MAX_ARCHIVE_BYTES)
    assert time.monotonic() - started < 2
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"
    assert max_lengths and all(0 < m <= 4 * 1024 * 1024 for m in max_lengths)


def test_gunzip_corrupt_and_truncated():
    good = gzip.compress(b"abc" * 1000)
    with pytest.raises(ArchiveError) as exc:
        maybe_gunzip(good[:-8], 100_000)
    assert exc.value.code == "E_INVALID_PLUGIN"
    with pytest.raises(ArchiveError) as exc:
        maybe_gunzip(b"\x1f\x8b" + b"garbage" * 5, 100_000)
    assert exc.value.code == "E_INVALID_PLUGIN"


def test_gunzip_file_is_bounded_and_streams_to_disk(tmp_path):
    src, dest = tmp_path / "in.gz", tmp_path / "out"
    src.write_bytes(gzip.compress(b"abc" * 1000))
    assert is_gzip_file(src)
    gunzip_file(src, dest, 10_000)
    assert dest.read_bytes() == b"abc" * 1000
    with pytest.raises(ArchiveError) as exc:
        gunzip_file(src, tmp_path / "small", 100)
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"
    assert not is_gzip_file(dest)
