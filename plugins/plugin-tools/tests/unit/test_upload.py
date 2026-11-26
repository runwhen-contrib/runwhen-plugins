"""validate-upload: a single plugin tar(.gz) or a multi-plugin bundle, validated like a sync tag."""

import gzip
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime

import jsonschema
import pytest
from conftest import PLUGIN
from fixtures import make_plugin_tree
from plugin_tools import TOOLS_VERSION
from plugin_tools.archive import FileEntry, build_archive
from plugin_tools.bundle import is_bundle, read_bundle
from plugin_tools.prepare import prepare
from plugin_tools.relay import PluginRelay
from plugin_tools.snapshot import SnapshotVersion, UstarWriter, canonical_json, export_snapshot
from plugin_tools.sync import Uploader
from plugin_tools.upload import run_validate

SCHEMA = json.loads((PLUGIN / "skills/plugin-tools/schemas/sync-result.v1.json").read_text())
LISTED_AT = "2026-11-27T10:00:00Z"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _validate(relay, data, upload_id="up_1"):
    manifest = run_validate(
        upload_id, data, Uploader(PluginRelay(relay.client())), listed_at=LISTED_AT
    )
    jsonschema.validate(manifest, SCHEMA)
    return manifest


def _plugin_tar(tree, *, dot_prefix=True, gz=False, dirs=True):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        prefix = "./" if dot_prefix else ""
        if dirs:
            d = tarfile.TarInfo(prefix or ".")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
        for path, entry in tree.items():
            info = tarfile.TarInfo(prefix + path)
            info.size = len(entry.data)
            info.mode = 0o755 if entry.executable else 0o644
            tf.addfile(info, io.BytesIO(entry.data))
    data = buf.getvalue()
    return gzip.compress(data) if gz else data


def _versions(*specs):
    out = []
    for name, version in specs:
        prepared = prepare(make_plugin_tree(name, version))
        out.append(
            SnapshotVersion(prepared, {"kind": "public", "id": "s", "tag": f"{name}/v{version}"})
        )
    return out


def _bundle(*specs):
    buf = io.BytesIO()
    export_snapshot(buf, _versions(*specs), [], release=None, now=NOW)
    return buf.getvalue()


def _tar_of(members):
    """A snapshot-style tar from (path, bytes) pairs, written like the exporter does."""
    buf = io.BytesIO()
    w = UstarWriter(buf)
    for path, data in members:
        w.add(path, data, len(data))
    w.close()
    return buf.getvalue()


# -- a single plugin ---------------------------------------------------------------------------


@pytest.mark.parametrize("gz", [False, True])
@pytest.mark.parametrize("dot_prefix", [False, True])
def test_single_plugin_tar_and_tar_gz(relay, gz, dot_prefix):
    tree = make_plugin_tree("kube", "0.4.0")
    manifest = _validate(relay, _plugin_tar(tree, gz=gz, dot_prefix=dot_prefix))
    assert manifest["uploadId"] == "up_1" and "sourceId" not in manifest
    assert manifest["toolsVersion"] == TOOLS_VERSION and manifest["more"] is False
    assert manifest["rejected"] == [] and manifest["runtimes"] == []
    (v,) = manifest["versions"]
    assert (v["name"], v["version"], v["tag"], v["commit"]) == ("kube", "0.4.0", None, None)
    expected = "sha256:" + hashlib.sha256(build_archive(tree)).hexdigest()
    assert v["archive"]["sha256"] == expected and relay.stored[expected] == build_archive(tree)


def test_single_plugin_with_files_the_archive_leaves_out(relay):
    tree = make_plugin_tree("kube", "0.4.0")
    plain = _validate(relay, _plugin_tar(tree))
    tree["tests/x.json"] = FileEntry(b"{}")
    tree["skills/a/tests/run/s.yaml"] = FileEntry(b"{}")
    again = _validate(relay, _plugin_tar(tree))
    assert again["versions"][0]["archive"] == plain["versions"][0]["archive"]


def test_invalid_single_plugin_is_rejected_by_name(relay):
    tree = make_plugin_tree("kube", "0.4.0")
    del tree["skills/a/tasks.yaml"]
    tree["skills/b/SKILL.md"] = FileEntry(b"no frontmatter\n")
    manifest = _validate(relay, _plugin_tar(tree))
    assert manifest["versions"] == [] and relay.stored == {}
    (r,) = manifest["rejected"]
    assert r["tag"] == "kube/v0.4.0" and r["commit"] is None and r["code"] == "E_PLUGIN_INVALID"


def test_unreadable_uploads_are_one_rejection(relay):
    for data, code in (
        (b"not a tar at all" * 50, "E_PLUGIN_INVALID"),
        (b"\x1f\x8b" + b"garbage" * 10, "E_PLUGIN_INVALID"),
    ):
        manifest = _validate(relay, data)
        (r,) = manifest["rejected"]
        assert (r["tag"], r["code"]) == (None, code) and manifest["versions"] == []


def test_a_tar_with_a_symlink_is_rejected(relay):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("plugin.yaml")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(info)
    manifest = _validate(relay, buf.getvalue())
    assert "regular files" in manifest["rejected"][0]["message"]


def test_a_gzip_bomb_is_refused_before_it_is_inflated(relay, monkeypatch):
    monkeypatch.setattr("plugin_tools.upload.MAX_INFLATED_BYTES", 1024 * 1024)
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    manifest = _validate(relay, bomb)
    assert manifest["rejected"][0]["code"] == "E_ARCHIVE_TOO_LARGE"


# -- a bundle ----------------------------------------------------------------------------------


def test_bundle_of_several_plugins_and_versions(relay):
    data = _bundle(("kube", "0.4.0"), ("kube", "0.5.0"), ("pg", "1.0.0"))
    assert is_bundle(data)
    manifest = _validate(relay, data)
    assert manifest["rejected"] == [] and manifest["uploadId"] == "up_1"
    assert [(v["name"], v["version"]) for v in manifest["versions"]] == [
        ("kube", "0.4.0"),
        ("kube", "0.5.0"),
        ("pg", "1.0.0"),
    ]
    assert all(v["tag"] is None and v["commit"] is None for v in manifest["versions"])
    assert len(relay.stored) == 3
    for v in manifest["versions"]:
        assert relay.stored[v["archive"]["sha256"]]


def test_gzipped_bundle(relay):
    manifest = _validate(relay, gzip.compress(_bundle(("kube", "0.4.0"))))
    assert [v["name"] for v in manifest["versions"]] == ["kube"]


def test_bundle_round_trips_through_read_bundle():
    listed, blobs = read_bundle(_bundle(("kube", "0.4.0"), ("pg", "1.0.0")))
    assert [(e.name, e.version) for e in listed] == [("kube", "0.4.0"), ("pg", "1.0.0")]
    assert all(hashlib.sha256(blobs[e.sha256]).hexdigest() == e.sha256 for e in listed)


def test_bundle_entry_that_disagrees_with_its_archive_is_rejected(relay):
    # snapshot.json says pg@1.0.0 but the blob is kube@0.4.0
    good = prepare(make_plugin_tree("kube", "0.4.0"))
    manifest_json = canonical_json(
        {
            "apiVersion": "registry.runwhen.com/snapshot/v1",
            "createdAt": "2026-11-20T00:00:00Z",
            "release": None,
            "source": "public",
            "plugins": [
                {"name": "pg", "version": "1.0.0", "sha256": good.sha256, "size": good.size}
            ],
            "runtimes": [],
        }
    )
    data = _tar_of(
        [("snapshot.json", manifest_json), (f"blobs/sha256/{good.sha256}", good.archive)]
    )
    manifest = _validate(relay, data)
    assert manifest["versions"] == [] and relay.stored == {}
    (r,) = manifest["rejected"]
    assert r["tag"] == "pg/v1.0.0" and "does not match the expected plugin 'pg'" in r["message"]


def test_bundle_with_one_bad_plugin_still_builds_the_others(relay):
    ok = prepare(make_plugin_tree("kube", "0.4.0"))
    broken_tree = make_plugin_tree("bad", "0.1.0")
    del broken_tree["skills/a/SKILL.md"]
    bad_archive = build_archive(broken_tree)
    bad_sha = hashlib.sha256(bad_archive).hexdigest()
    manifest_json = canonical_json(
        {
            "apiVersion": "registry.runwhen.com/snapshot/v1",
            "createdAt": "2026-11-20T00:00:00Z",
            "release": None,
            "source": "public",
            "plugins": [
                {"name": "bad", "version": "0.1.0", "sha256": bad_sha, "size": len(bad_archive)},
                {"name": "kube", "version": "0.4.0", "sha256": ok.sha256, "size": ok.size},
                {"name": "gone", "version": "0.1.0", "sha256": "c" * 64, "size": 10},
            ],
            "runtimes": [],
        }
    )
    data = _tar_of(
        [
            ("snapshot.json", manifest_json),
            (f"blobs/sha256/{bad_sha}", bad_archive),
            (f"blobs/sha256/{ok.sha256}", ok.archive),
        ]
    )
    manifest = _validate(relay, data)
    assert [v["name"] for v in manifest["versions"]] == ["kube"]
    assert {r["tag"]: r["code"] for r in manifest["rejected"]} == {
        "bad/v0.1.0": "E_PLUGIN_INVALID",
        "gone/v0.1.0": "E_PLUGIN_INVALID",
    }


_EMPTY_MANIFEST = b'{"apiVersion":"registry.runwhen.com/snapshot/v1","plugins":[]}'


@pytest.mark.parametrize(
    "members,fragment",
    [
        (
            [("snapshot.json", _EMPTY_MANIFEST), ("blobs/sha256/" + "a" * 64, b"x")],
            "does not hash to its name",
        ),
        ([("snapshot.json", b"not json")], "not valid JSON"),
        ([("snapshot.json", b'{"apiVersion":"nope"}')], "apiVersion"),
        ([("snapshot.json", _EMPTY_MANIFEST), ("readme.txt", b"x")], "unexpected member"),
    ],
)
def test_malformed_bundles_are_one_rejection(relay, members, fragment):
    manifest = _validate(relay, _tar_of(members))
    (r,) = manifest["rejected"]
    assert fragment in r["message"] and r["tag"] is None and relay.stored == {}


def test_a_tar_without_snapshot_json_is_read_as_a_plugin_tree(relay):
    manifest = _validate(relay, _tar_of([("index/x/1.json", b"{}")]))
    assert manifest["versions"] == [] and manifest["rejected"][0]["code"] == "E_PLUGIN_INVALID"


def test_bundle_with_a_pax_header_is_refused_by_the_prescan(relay):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo("snapshot.json")
        data = b"{}"
        info.size = len(data)
        info.pax_headers = {"comment": "x"}
        tf.addfile(info, io.BytesIO(data))
    manifest = _validate(relay, buf.getvalue())
    assert manifest["rejected"][0]["code"] == "E_INVALID_SNAPSHOT"


def test_bundle_listing_the_same_version_twice_is_refused(relay):
    p = prepare(make_plugin_tree("kube", "0.4.0"))
    entry = {"name": "kube", "version": "0.4.0", "sha256": p.sha256, "size": p.size}
    doc = {"apiVersion": "registry.runwhen.com/snapshot/v1", "plugins": [entry, entry]}
    manifest = _validate(relay, _tar_of([("snapshot.json", canonical_json(doc))]))
    assert "twice" in manifest["rejected"][0]["message"]


def test_the_archive_cap_stops_a_bundle_with_more(relay):
    relay.cap = 1
    manifest = _validate(relay, _bundle(("kube", "0.4.0"), ("kube", "0.5.0"), ("pg", "1.0.0")))
    assert [v["version"] for v in manifest["versions"]] == ["0.4.0"] and manifest["more"] is True


def test_reuploading_the_same_bundle_sends_nothing_new(relay):
    data = _bundle(("kube", "0.4.0"))
    first = _validate(relay, data)
    relay.requests.clear()
    second = _validate(relay, data)
    assert second == first and len(relay.stored) == 1
