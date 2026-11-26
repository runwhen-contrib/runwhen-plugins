import base64
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from plugin_tools.archive import (
    ArchiveError,
    FileEntry,
    Limits,
    build_archive,
    normalize_tree,
    read_archive,
    safe_extract,
    sha256_hex,
    sha256_ref,
)
from runwhen_capability.contracts import archive as sdk_archive

TESTDATA = Path(__file__).parent / "data" / "archive"
TREE = {
    "plugin.yaml": FileEntry(b"name: x\n"),
    "skills/a/SKILL.md": FileEntry(b"---\n"),
    "skills/a/run.sh": FileEntry(b"#!/bin/sh\n", executable=True),
}


def _code(exc_info) -> str:
    return exc_info.value.code


def _golden_tree() -> dict[str, FileEntry]:
    raw = json.loads((TESTDATA / "simple.json").read_text())
    return {p: FileEntry(base64.b64decode(v["data_b64"]), v["executable"]) for p, v in raw.items()}


def _golden_blob() -> bytes:
    """The frozen golden archive: writer bytes for the vector tree minus its root `tests/` file.

    The vector (shared with the SDK and the Go extractor) includes `skills/x/tests/fixture.json`,
    which `build_archive` now drops, so the frozen hash is checked against the SDK writer directly.
    """
    tree = {p: e for p, e in _golden_tree().items() if p != "tests/dropped.json"}
    entries = [
        sdk_archive.TreeEntry(p, tree[p].data, tree[p].executable)
        for p in sorted(tree, key=lambda p: p.encode("utf-8"))
    ]
    return sdk_archive.build(entries)


def _tar(*members: tuple[str, bytes, bytes], fmt: int = tarfile.USTAR_FORMAT) -> bytes:
    """Hand-build a tar from (name, typeflag, content) members; linkname 'target' for links."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for name, typeflag, content in members:
            info = tarfile.TarInfo(name)
            info.type = typeflag
            info.mode = 0o644
            if typeflag in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "target"
            else:
                info.size = len(content)
            tf.addfile(info, io.BytesIO(content) if info.size else None)
    return buf.getvalue()


def test_same_tree_same_bytes_regardless_of_order():
    a = {"plugin.yaml": FileEntry(b"name: x\n"), "skills/a/SKILL.md": FileEntry(b"---\n")}
    assert build_archive(a) == build_archive(dict(reversed(list(a.items()))))


def test_header_fields_are_zeroed():
    blob = build_archive(TREE)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        members = tf.getmembers()
    assert [m.name for m in members] == sorted(TREE)
    for m in members:
        assert m.uid == m.gid == 0
        assert m.mtime == 0
        assert m.uname == m.gname == ""
        assert m.mode in {0o644, 0o755}
        assert m.type == tarfile.REGTYPE  # no DIRTYPE members
        assert m.linkname == ""


def test_exec_bit_preserved():
    with tarfile.open(fileobj=io.BytesIO(build_archive(TREE))) as tf:
        assert tf.getmember("skills/a/run.sh").mode == 0o755
        assert tf.getmember("plugin.yaml").mode == 0o644
    assert read_archive(build_archive(TREE))["skills/a/run.sh"].executable is True


def test_length_is_multiple_of_10240():
    assert len(build_archive(TREE)) % 10240 == 0
    assert len(build_archive({})) == 10240


def test_numeric_fields_match_contracts_v1():
    # archive.md 3.2: uid/gid/mtime are zero-padded octal 0; devmajor/devminor are all NUL.
    blob = build_archive({"plugin.yaml": FileEntry(b"x")})
    assert blob[100:108] == b"0000644\0"
    assert blob[108:124] == b"0000000\0" * 2
    assert blob[124:148] == b"00000000001\0" + b"00000000000\0"
    assert blob[257:265] == b"ustar\x0000"
    assert blob[329:345] == b"\0" * 16
    assert blob[265:329] == b"\0" * 64  # uname, gname empty


def test_root_tests_dir_excluded_only_at_root():
    tree = {
        "plugin.yaml": FileEntry(b"x"),
        "tests/x": FileEntry(b"dropped"),
        "rules/tests/x": FileEntry(b"kept"),
        "tests": FileEntry(b"a file named tests is not the directory"),
    }
    assert sorted(normalize_tree(tree)) == ["plugin.yaml", "rules/tests/x", "tests"]


def test_skill_tests_dir_excluded_exactly_one_segment_under_skills():
    tree = {
        "plugin.yaml": FileEntry(b"x"),
        "skills/a/SKILL.md": FileEntry(b"kept"),
        "skills/a/tests/t/s.yaml": FileEntry(b"dropped"),
        "skills/a/tests/fixtures/big.json": FileEntry(b"dropped"),
        "skills/b/tests/odd name\\.json": FileEntry(b"dropped, not rejected"),
        "skills/c/tests": FileEntry(b"a file named tests is not the directory"),
        "skills/a/sub/tests/y": FileEntry(b"kept"),
        "skills/a/tests.md": FileEntry(b"kept"),
        "skills/a/testsx/y": FileEntry(b"kept"),
        "skills/tests/y": FileEntry(b"kept"),
        "other/a/tests/y": FileEntry(b"kept"),
    }
    assert sorted(normalize_tree(tree)) == [
        "other/a/tests/y",
        "plugin.yaml",
        "skills/a/SKILL.md",
        "skills/a/sub/tests/y",
        "skills/a/tests.md",
        "skills/a/testsx/y",
        "skills/c/tests",
        "skills/tests/y",
    ]


def test_lock_and_dot_runwhen_excluded_at_root_only():
    tree = {
        "plugin.yaml": FileEntry(b"x"),
        "runwhen.lock": FileEntry(b"dropped"),
        ".runwhen/schema.json": FileEntry(b"dropped"),
        ".runwhen/deep/odd name\\.json": FileEntry(b"dropped, not rejected"),
        "skills/a/runwhen.lock": FileEntry(b"kept"),
        "skills/a/.runwhen/x.json": FileEntry(b"kept"),
        "runwhen.lock.bak": FileEntry(b"kept"),
        ".runwhenx/y": FileEntry(b"kept"),
        ".runwhen": FileEntry(b"a file named .runwhen is not the directory"),
    }
    assert sorted(normalize_tree(tree)) == [
        ".runwhen",
        ".runwhenx/y",
        "plugin.yaml",
        "runwhen.lock.bak",
        "skills/a/.runwhen/x.json",
        "skills/a/runwhen.lock",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/abs",
        "a/../b",
        "../b",
        "a/./b",
        "a//b",
        "a/",
        "",
        "a\\b",
        "a\x00b",
        "a\x07b",
        "a\x7fb",
        "a\x85b",
        "é.md",  # NFD: "e" + combining acute
        "a\udcffb",  # not encodable as UTF-8
    ],
)
def test_bad_paths_rejected(path):
    with pytest.raises(ArchiveError) as exc:
        normalize_tree({path: FileEntry(b"x")})
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_nfc_path_accepted():
    assert "é.md" in normalize_tree({"é.md": FileEntry(b"x")})


def test_case_insensitive_collision_rejected():
    with pytest.raises(ArchiveError) as exc:
        normalize_tree({"A.md": FileEntry(b"1"), "a.md": FileEntry(b"2")})
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_file_and_directory_collision_rejected():
    with pytest.raises(ArchiveError) as exc:
        normalize_tree({"a": FileEntry(b"1"), "a/b": FileEntry(b"2")})
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_path_over_255_bytes_rejected():
    ok = "/".join(["d" * 50] * 4) + "/f"  # 203 bytes, splittable
    assert ok in normalize_tree({ok: FileEntry(b"x")})
    too_long = "/".join(["d" * 50] * 5) + "/f"  # 258 bytes
    with pytest.raises(ArchiveError) as exc:
        normalize_tree({too_long: FileEntry(b"x")})
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_path_over_255_bytes_counts_bytes_not_characters():
    path = "é" * 128  # 128 characters, 256 bytes
    with pytest.raises(ArchiveError) as exc:
        normalize_tree({path: FileEntry(b"x")})
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_unsplittable_path_rejected():
    for path in ("n" * 101, "p" * 156 + "/f", "a/" + "n" * 101):
        with pytest.raises(ArchiveError) as exc:
            normalize_tree({path: FileEntry(b"x")})
        assert _code(exc) == "E_INVALID_PLUGIN"


def test_ustar_split_uses_longest_prefix():
    path = "rules/" + "d" * 60 + "/" + "e" * 60 + "/f.md"
    blob = build_archive({path: FileEntry(b"x")})
    assert blob[0:100].rstrip(b"\0") == b"f.md"
    assert blob[345:500].rstrip(b"\0") == ("rules/" + "d" * 60 + "/" + "e" * 60).encode()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        assert tf.getnames() == [path]


def test_limits():
    many = {f"f{i}": FileEntry(b"") for i in range(10_001)}
    with pytest.raises(ArchiveError) as exc:
        build_archive(many)
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"
    big = {"big.bin": FileEntry(b"\0" * (16 * 1024 * 1024 + 1))}
    with pytest.raises(ArchiveError) as exc:
        build_archive(big)
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"
    # Exactly at the limits is fine, and dropped tests/ files do not count.
    assert len(normalize_tree({f"f{i}": FileEntry(b"") for i in range(10_000)})) == 10_000
    at_cap = {f"f{i}": FileEntry(b"") for i in range(10_000)} | {"tests/x": FileEntry(b"")}
    assert len(normalize_tree(at_cap)) == 10_000
    with pytest.raises(ArchiveError) as exc:
        build_archive({}, Limits(archive_bytes=10_240 - 1))
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"


def test_golden_vector():
    # FROZEN: this hash is the byte-for-byte contract shared with the SDK reference builder, papi
    # and the Go runner's extractor. It must NEVER change. If this test fails, the writer is wrong,
    # not the vector. Re-frozen once, for contracts-v1 (devmajor/devminor became all NUL); cross-
    # checked against an independent hand-rolled ustar writer.
    expected = (TESTDATA / "simple.sha256").read_text().strip()
    assert expected == "e1207a39b0a3109a8397346f4f06426f7ba2ae2880629ed81670ae11ef6ea452"
    blob = _golden_blob()
    assert sha256_hex(blob) == expected
    # build_archive additionally drops skills/<skill>/tests/**: the golden minus its fixture.
    no_fixture = {p: e for p, e in _golden_tree().items() if p != "skills/x/tests/fixture.json"}
    assert "skills/x/tests/fixture.json" in _golden_tree()
    kept = build_archive(_golden_tree())
    assert kept == build_archive(no_fixture)
    assert kept == build_archive(dict(reversed(list(_golden_tree().items()))))
    assert "skills/x/tests/fixture.json" not in read_archive(kept)


GOLDEN_PATHS = [
    "knowledge/café.md",
    "plugin.yaml",
    "rules/" + "d" * 60 + "/" + "e" * 60 + "/long.md",
    "skills/x/SKILL.md",
    "skills/x/run.sh",
    "skills/x/tests/fixture.json",
]


@pytest.mark.skipif(shutil.which("tar") is None, reason="tar not installed")
def test_tar_lists_golden_archive(tmp_path):
    path = tmp_path / "golden.tar"
    path.write_bytes(_golden_blob())
    out = subprocess.run(
        ["tar", "-tvf", str(path)], capture_output=True, text=True, check=True
    ).stdout
    rows = [line.split() for line in out.splitlines()]
    assert [r[-1] for r in rows] == GOLDEN_PATHS
    modes = {r[-1]: r[0] for r in rows}
    assert modes["skills/x/run.sh"] == "-rwxr-xr-x"
    assert modes["plugin.yaml"] == "-rw-r--r--"


VECTORS = sorted((TESTDATA / "contracts-v1").glob("*.json"))


@pytest.mark.parametrize("vector", VECTORS, ids=lambda p: p.stem)
def test_contracts_v1_vectors(vector):
    case = json.loads(vector.read_text())
    expected = vector.with_suffix(".tar").read_bytes()
    entries = {
        e["path"]: FileEntry(base64.b64decode(e["dataBase64"]), e["executable"])
        for e in case["entries"]
    }
    blob = build_archive(entries)
    assert blob == expected
    assert len(blob) == case["size"]
    assert sha256_ref(sha256_hex(blob)) == case["sha256"]
    assert read_archive(expected, case["sha256"]) == entries


INVALID_VECTORS = sorted((TESTDATA / "contracts-v1" / "invalid").glob("*.json"))


@pytest.mark.parametrize("vector", INVALID_VECTORS, ids=lambda p: p.stem)
def test_contracts_v1_invalid_vectors_rejected(vector, tmp_path):
    case = json.loads(vector.read_text())
    blob = vector.with_suffix(".tar").read_bytes()
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == case["expectedCode"]
    # The same bytes must also be refused with a matching hash, and must write nothing.
    with pytest.raises(ArchiveError) as exc:
        safe_extract(blob, tmp_path / "out", sha256_hex(blob))
    assert _code(exc) == case["expectedCode"]
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_contracts_v1_invalid_vectors_are_present():
    assert {p.stem for p in INVALID_VECTORS} >= {
        "bad-checksum",
        "base256-size",
        "declared-size-over-limit",
        "gnu-longname",
        "gnu-sparse",
        "pax-extended-header",
        "pax-global-path-override",
        "symlink",
        "trailing-garbage-after-end",
        "truncated-data",
    }


def test_contracts_v1_vectors_are_present():
    assert {p.stem for p in VECTORS} >= {
        "empty-file",
        "long-path-prefix-split",
        "nested-executable",
        "single-file",
        "ten-files-out-of-order",
    }


def test_sha256_forms():
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_ref("ab" * 32) == "sha256:" + "ab" * 32


def test_read_archive_roundtrip_and_hash_check():
    blob = build_archive(TREE)
    digest = sha256_hex(blob)
    assert read_archive(blob) == TREE
    assert read_archive(blob, digest) == TREE
    assert read_archive(blob, sha256_ref(digest)) == TREE
    for wrong in ("0" * 64, "sha256:" + "0" * 64, "garbage"):
        with pytest.raises(ArchiveError) as exc:
            read_archive(blob, wrong)
        assert _code(exc) == "E_HASH_MISMATCH"


def test_read_archive_rebuilds_identical_bytes():
    blob = _golden_blob()
    tree = read_archive(blob)
    assert "tests/dropped.json" not in tree
    assert "skills/x/tests/fixture.json" in tree  # reading never drops; building does
    rebuilt = build_archive(tree)
    assert read_archive(rebuilt) == {p: e for p, e in tree.items() if "/tests/" not in p}
    assert build_archive(read_archive(rebuilt)) == rebuilt


def test_hash_is_checked_before_parsing():
    with pytest.raises(ArchiveError) as exc:
        read_archive(b"not a tar", "0" * 64)
    assert _code(exc) == "E_HASH_MISMATCH"


def test_oversized_archive_rejected_before_hashing():
    with pytest.raises(ArchiveError) as exc:
        read_archive(b"\0" * 1025, "0" * 64, Limits(archive_bytes=1024))
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"


@pytest.mark.parametrize("junk", [b"", b"not a tar at all", b"\x1f\x8b" + b"\0" * 600])
def test_garbage_and_gzip_rejected(junk):
    with pytest.raises(ArchiveError) as exc:
        read_archive(junk)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_gzip_of_valid_archive_rejected():
    import gzip

    with pytest.raises(ArchiveError) as exc:
        read_archive(gzip.compress(build_archive(TREE)))
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_symlink_rejected():
    blob = _tar(("ok", tarfile.REGTYPE, b"x"), ("link", tarfile.SYMTYPE, b""))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


@pytest.mark.parametrize(
    "typeflag", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE]
)
def test_other_member_types_rejected(typeflag):
    with pytest.raises(ArchiveError) as exc:
        read_archive(_tar(("x", typeflag, b"")))
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_directory_entries_ignored():
    blob = _tar(("skills", tarfile.DIRTYPE, b""), ("skills/a", tarfile.REGTYPE, b"x"))
    assert read_archive(blob) == {"skills/a": FileEntry(b"x")}


def test_legacy_null_typeflag_rejected():
    blob = bytearray(_tar(("a", tarfile.REGTYPE, b"x")))
    blob[156] = 0
    # Fix the checksum so only the typeflag differs.
    blob[148:156] = b"        "
    blob[148:156] = b"%06o\0 " % sum(blob[:512])
    with pytest.raises(ArchiveError) as exc:
        read_archive(bytes(blob))
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_pax_extension_headers_rejected():
    long_name = "/".join(["d" * 60] * 5) + "/f"  # needs a pax record in PAX_FORMAT
    blob = _tar((long_name, tarfile.REGTYPE, b"x"), fmt=tarfile.PAX_FORMAT)
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


@pytest.mark.parametrize("name", ["../x", "/etc/x", "a/../../x", "a\\b"])
def test_read_archive_rejects_unsafe_paths(name):
    with pytest.raises(ArchiveError) as exc:
        read_archive(_tar((name, tarfile.REGTYPE, b"x")))
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_read_archive_rejects_duplicates_and_case_collisions():
    for names in (("a", "a"), ("A", "a")):
        blob = _tar(*[(n, tarfile.REGTYPE, b"x") for n in names])
        with pytest.raises(ArchiveError) as exc:
            read_archive(blob)
        assert _code(exc) == "E_INVALID_PLUGIN"


def test_declared_size_over_limit_rejected_without_reading(monkeypatch):
    # A header claiming 1 GiB with no body: rejected on the declared size, not by reading it.
    info = tarfile.TarInfo("bomb")
    info.size = 1 << 30
    blob = info.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict") + b"\0" * 1024
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"


def test_per_file_and_count_limits_while_parsing():
    blob = _tar(("a", tarfile.REGTYPE, b"x" * 10), ("b", tarfile.REGTYPE, b"y"))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob, limits=Limits(file_bytes=9))
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob, limits=Limits(file_bytes=100, files=1))
    assert _code(exc) == "E_ARCHIVE_TOO_LARGE"


def test_truncated_body_rejected():
    blob = _tar(("a", tarfile.REGTYPE, b"x" * 2000))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob[: 512 + 700])
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_trailing_garbage_after_end_marker_rejected():
    blob = bytearray(build_archive(TREE))
    blob[-1] = 1
    with pytest.raises(ArchiveError) as exc:
        read_archive(bytes(blob))
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_safe_extract_writes_tree_with_modes(tmp_path):
    blob = _golden_blob()
    dest = tmp_path / "out"
    safe_extract(blob, dest, sha256_ref(sha256_hex(blob)))
    assert (dest / "plugin.yaml").read_bytes().startswith(b"apiVersion")
    assert (dest / "skills/x/run.sh").stat().st_mode & 0o777 == 0o755
    assert (dest / "plugin.yaml").stat().st_mode & 0o777 == 0o644
    assert not (dest / "tests").exists()
    assert (dest / "skills/x/tests/fixture.json").is_file()


def test_safe_extract_rejects_traversal(tmp_path):
    blob = _tar(("ok.txt", tarfile.REGTYPE, b"fine"), ("../x", tarfile.REGTYPE, b"evil"))
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError) as exc:
        safe_extract(blob, dest, sha256_hex(blob))
    assert _code(exc) == "E_INVALID_PLUGIN"
    assert not (tmp_path / "x").exists()
    assert not dest.exists()  # all-or-nothing: nothing written, not even the valid member


def test_safe_extract_rejects_hash_mismatch_and_writes_nothing(tmp_path):
    blob = build_archive(TREE)
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError) as exc:
        safe_extract(blob, dest, "0" * 64)
    assert _code(exc) == "E_HASH_MISMATCH"
    assert not dest.exists()


def test_safe_extract_does_not_follow_preexisting_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "skills").symlink_to(outside, target_is_directory=True)
    blob = build_archive({"skills/a.md": FileEntry(b"x")})
    with pytest.raises(ArchiveError) as exc:
        safe_extract(blob, dest, sha256_hex(blob))
    assert _code(exc) == "E_INVALID_PLUGIN"
    assert list(outside.iterdir()) == []


def test_safe_extract_does_not_overwrite_existing_file(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "plugin.yaml").write_text("mine")
    blob = build_archive({"plugin.yaml": FileEntry(b"x")})
    with pytest.raises(ArchiveError):
        safe_extract(blob, dest, sha256_hex(blob))
    assert (dest / "plugin.yaml").read_text() == "mine"


def _with_global_header(records: dict[str, str], *members: tuple[str, bytes, bytes]) -> bytes:
    """A pax global header ('g') followed by an ordinary ustar archive."""
    return tarfile.TarInfo.create_pax_global_header(records) + _tar(*members)


def test_pax_global_header_rejected():
    blob = _with_global_header({"comment": "hi"}, ("a", tarfile.REGTYPE, b"x"))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_empty_pax_global_header_rejected():
    # No records, so tarfile reports nothing odd on the member; only the offset gap gives it away.
    blob = _with_global_header({}, ("a", tarfile.REGTYPE, b"x"))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_pax_global_header_cannot_override_path():
    blob = _with_global_header({"path": "renamed"}, ("a", tarfile.REGTYPE, b"x"))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_pax_global_header_cannot_override_size():
    blob = _with_global_header({"size": "1"}, ("a", tarfile.REGTYPE, b"xyz"))
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_pax_global_sparse_expansion_rejected_without_allocating(monkeypatch):
    # 'g' turns a tiny member into a sparse file whose declared realsize passes the per-file cap.
    # It must be rejected before anything is read, not expanded and measured afterwards.
    records = {
        "GNU.sparse.name": "big",
        "GNU.sparse.realsize": str(16 * 1024 * 1024),
        "GNU.sparse.map": "0,1",
    }
    blob = _with_global_header(records, ("a", tarfile.REGTYPE, b"x"))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:  # sanity: tarfile does see a sparse file
        member = tf.next()
        assert member is not None and member.size == 16 * 1024 * 1024 and member.sparse

    def no_read(*args, **kwargs):
        raise AssertionError("sparse member must be rejected before extraction")

    monkeypatch.setattr(tarfile.TarFile, "extractfile", no_read)
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_pax_extended_header_before_directory_rejected():
    long_dir = "/".join(["d" * 60] * 5)
    blob = _tar((long_dir, tarfile.DIRTYPE, b""), fmt=tarfile.PAX_FORMAT)
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_directory_entry_with_body_rejected():
    info = tarfile.TarInfo("d")
    info.type = tarfile.DIRTYPE
    info.size = 512
    blob = info.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict") + b"x" * 512 + b"\0" * 10240
    with pytest.raises(ArchiveError) as exc:
        read_archive(blob)
    assert _code(exc) == "E_INVALID_PLUGIN"


def test_limits_can_only_lower_the_defaults():
    Limits(archive_bytes=1, files=1, file_bytes=1)
    for bad in (
        {"archive_bytes": 64 * 1024 * 1024 + 1},
        {"files": 10_001},
        {"file_bytes": 16 * 1024 * 1024 + 1},
        {"files": 0},
    ):
        with pytest.raises(ValueError):
            Limits(**bad)
