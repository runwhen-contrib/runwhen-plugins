"""The sync pipeline end to end: a real local repo, the real SDK compiler, an artifact spool."""

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from conftest import PLUGIN, make_source
from fixtures import make_plugin_tree
from plugin_tools import TOOLS_VERSION, result
from plugin_tools.archive import FileEntry, build_archive
from plugin_tools.gitrun import GitError, GitTimeout
from plugin_tools.gitsource import TagRef
from plugin_tools.sync import (
    InputInvalid,
    SyncRequest,
    classify_git_error,
    newest_first,
    run_sync,
)
from runwhen_capability.rwerrors import RwError

SCHEMA = json.loads((PLUGIN / "skills/plugin-tools/schemas/sync-result.v1.json").read_text())
LISTED_AT = "2026-11-27T10:00:00Z"
GOOD_RUNTIME = """\
name: universal
version: 1.0.0
image: ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal
digests:
  index: "sha256:{a}"
  linux/amd64: "sha256:{b}"
tools:
  git: "2.47"
""".format(a="a" * 64, b="b" * 64)


def _sync(repo, run_dir, spool, *, known=None, max_versions=None, limits=None, **spec):
    source = make_source(repo.url, run_dir, **spec)
    request = SyncRequest.parse(
        {"id": "t", "url": repo.url, **spec}, known, max_versions, limits, allow_any_scheme=True
    )
    manifest = run_sync(request, source, spool, listed_at=LISTED_AT)
    jsonschema.validate(manifest, SCHEMA)
    return manifest


def _publish(repo, name, version, **kw):
    return repo.publish(
        f"plugins/{name}", make_plugin_tree(name, version, **kw), f"{name}/v{version}"
    )


def _tags(manifest):
    return [v["tag"] for v in manifest["versions"]]


def test_sync_builds_uploads_and_returns_the_manifest(repo, run_dir, spool):
    kube = _publish(repo, "kube", "0.1.0")
    pg = _publish(repo, "pg", "1.2.0", requires="requires: {kube: ^0.1}\n")
    manifest = _sync(repo, run_dir, spool, max_versions=None)

    assert manifest["apiVersion"] == "plugins.runwhen.com/sync-result/v1"
    assert manifest["sourceId"] == "t" and "uploadId" not in manifest
    assert manifest["listedAt"] == LISTED_AT and manifest["toolsVersion"] == TOOLS_VERSION
    assert manifest["more"] is False and manifest["rejected"] == [] and manifest["runtimes"] == []
    assert _tags(manifest) == ["pg/v1.2.0", "kube/v0.1.0"]  # newest first
    by_name = {v["name"]: v for v in manifest["versions"]}
    assert by_name["kube"]["commit"] == kube and by_name["pg"]["commit"] == pg
    assert by_name["pg"]["requires"] == {"plugins": {"kube": "^0.1"}}
    assert by_name["kube"]["requires"] == {}
    assert by_name["kube"]["compiled"]["apiVersion"] == "runwhen.com/compiled-plugin/v1"
    assert by_name["kube"]["sdkVersion"]
    assert [v["artifact"] for v in manifest["versions"]] == ["archive1", "archive2"]

    # what was uploaded is exactly what the manifest names: the deterministic ustar by sha256
    for v in manifest["versions"]:
        sha = v["archive"]["sha256"]
        data = spool.stored[sha]
        assert sha == "sha256:" + hashlib.sha256(data).hexdigest()
        assert v["archive"]["size"] == len(data)
    tree = make_plugin_tree("kube", "0.1.0")
    assert (
        by_name["kube"]["archive"]["sha256"]
        == "sha256:" + hashlib.sha256(build_archive(tree)).hexdigest()
    )
    assert by_name["kube"]["archive"]["files"] == len(tree)


def test_sync_is_deterministic(repo, run_dir, spool):
    _publish(repo, "kube", "0.1.0")
    first = _sync(repo, run_dir, spool)
    second = _sync(repo, run_dir, spool)
    assert first == second


def test_known_tags_are_skipped_at_the_same_commit(repo, run_dir, spool):
    _publish(repo, "kube", "0.1.0")
    c2 = _publish(repo, "kube", "0.2.0")
    first = _sync(repo, run_dir, spool)
    known = [
        {"tag": v["tag"], "commit": v["commit"], "sha256": v["archive"]["sha256"]}
        for v in first["versions"]
    ]
    written = dict(spool.stored)
    again = _sync(repo, run_dir, spool, known=known)
    assert again["versions"] == [] and again["rejected"] == [] and again["more"] is False
    assert spool.stored == written  # nothing built or written

    # a new tag is the only one built; a known tag that MOVED (new commit) is not skipped
    _publish(repo, "kube", "0.3.0")
    repo.write_tree("plugins/kube", make_plugin_tree("kube", "0.2.0", skills=("a", "b")))
    repo.commit("rewrite")
    repo.tag("kube/v0.2.0", force=True)
    third = _sync(repo, run_dir, spool, known=known)
    assert sorted(_tags(third)) == ["kube/v0.2.0", "kube/v0.3.0"]
    moved = next(v for v in third["versions"] if v["tag"] == "kube/v0.2.0")
    assert moved["commit"] != c2 and moved["commit"] == repo.head()


def test_max_versions_takes_the_newest_and_the_next_run_continues(repo, run_dir, spool):
    for v in ("0.1.0", "0.2.0", "0.3.0", "0.10.0", "0.4.0-rc.1"):
        _publish(repo, "kube", v)
    first = _sync(repo, run_dir, spool, max_versions=2)
    assert _tags(first) == ["kube/v0.10.0", "kube/v0.4.0-rc.1"] and first["more"] is True
    known = [{"tag": v["tag"], "commit": v["commit"]} for v in first["versions"]]
    second = _sync(repo, run_dir, spool, max_versions=2, known=known)
    assert _tags(second) == ["kube/v0.3.0", "kube/v0.2.0"] and second["more"] is True
    known += [{"tag": v["tag"], "commit": v["commit"]} for v in second["versions"]]
    third = _sync(repo, run_dir, spool, max_versions=2, known=known)
    assert _tags(third) == ["kube/v0.1.0"] and third["more"] is False


def test_semver_precedence_orders_prereleases_below_their_release():
    tags = [
        TagRef(f"k/v{v}", "k", v, "a" * 40)
        for v in ("1.0.0-rc.1", "1.0.0", "1.0.0-alpha", "0.9.9", "1.0.0-rc.10")
    ]
    assert [t.version for t in newest_first(tags)] == [
        "1.0.0",
        "1.0.0-rc.10",
        "1.0.0-rc.1",
        "1.0.0-alpha",
        "0.9.9",
    ]


def test_manifest_cap_sets_more_and_stops_before_uploading(repo, run_dir, spool, monkeypatch):
    for v in ("0.1.0", "0.2.0", "0.3.0"):
        _publish(repo, "kube", v)
    one = _sync(repo, run_dir, spool, max_versions=1)
    size_of_one = len(json.dumps(one, separators=(",", ":")).encode())
    spool.stored.clear()
    # a cap that holds one version (and the skeleton) but not two
    monkeypatch.setattr(result, "MAX_MANIFEST_BYTES", size_of_one + 200)
    capped = _sync(repo, run_dir, spool)
    assert _tags(capped) == ["kube/v0.3.0"] and capped["more"] is True
    assert len(json.dumps(capped, separators=(",", ":")).encode()) <= size_of_one + 200
    assert len(spool.stored) == 1  # the version that did not fit was not uploaded


def test_the_real_cap_is_one_mib():
    assert result.MAX_MANIFEST_BYTES == 1024 * 1024


def test_manifest_refuses_to_emit_over_the_cap(monkeypatch):
    m = result.Manifest(tools_version=TOOLS_VERSION, listed_at=LISTED_AT, source_id="t")
    monkeypatch.setattr(result, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(ValueError, match="1 MiB"):
        m.finish()
    big = {"tag": "x" * 100, "commit": None, "code": "c", "message": "m"}
    monkeypatch.setattr(result, "MAX_MANIFEST_BYTES", 250)
    assert m.add("rejected", big) is False and m.more is True and m.doc["rejected"] == []


def test_the_cap_counts_non_ascii_as_it_is_sent(monkeypatch):
    # The SDK sends the result with non-ASCII escaped: "…" is 6 bytes on the wire, not 3.
    m = result.Manifest(tools_version=TOOLS_VERSION, listed_at=LISTED_AT, source_id="t")
    item = {"tag": None, "commit": None, "code": "c", "message": "…" * 100}
    wire = len(json.dumps({**m.doc, "rejected": [item]}, separators=(",", ":")).encode())
    monkeypatch.setattr(result, "MAX_MANIFEST_BYTES", wire - 1)
    assert m.add("rejected", item) is False
    monkeypatch.setattr(result, "MAX_MANIFEST_BYTES", wire)
    assert m.add("rejected", item) is True


def test_tag_that_disagrees_with_plugin_yaml_is_rejected_not_uploaded(repo, run_dir, spool):
    repo.publish("plugins/kube", make_plugin_tree("kube", "1.3.0"), "kube/v1.4.0")  # version
    repo.publish("plugins/pg", make_plugin_tree("other", "1.0.0"), "pg/v1.0.0")  # name
    _publish(repo, "ok", "2.0.0")
    manifest = _sync(repo, run_dir, spool)
    assert _tags(manifest) == ["ok/v2.0.0"]
    rejected = {r["tag"]: r for r in manifest["rejected"]}
    assert set(rejected) == {"kube/v1.4.0", "pg/v1.0.0"}
    assert all(r["code"] == "E_PLUGIN_INVALID" for r in rejected.values())
    assert "does not match the expected version '1.4.0'" in rejected["kube/v1.4.0"]["message"]
    assert "does not match the expected plugin 'pg'" in rejected["pg/v1.0.0"]["message"]
    assert rejected["kube/v1.4.0"]["commit"]
    assert len(spool.stored) == 1


def test_bad_plugins_are_rejected_with_a_code_and_the_run_goes_on(repo, run_dir, spool):
    # a symlink, a plugin the SDK refuses (a skill without SKILL.md), a missing path, an
    # oversized version, a tag that is a tree, and a good one
    repo.write_tree("plugins/sym", make_plugin_tree("sym", "0.1.0"))
    repo.symlink("plugins/sym/skills/a/lib/link", "/etc/passwd")
    repo.commit()
    repo.tag("sym/v0.1.0")
    broken = make_plugin_tree("broken", "0.1.0")
    del broken["skills/a/SKILL.md"]
    repo.publish("plugins/broken", broken, "broken/v0.1.0")
    repo.write("README.md", b"x")
    repo.commit()
    repo.tag("ghost/v0.1.0")
    repo.tag("big/v99999999999.0.0")
    repo.git("tag", "tree/v0.1.0", "HEAD^{tree}")
    _publish(repo, "good", "1.0.0")

    manifest = _sync(repo, run_dir, spool)
    assert _tags(manifest) == ["good/v1.0.0"]
    got = {r["tag"]: (r["code"], r["message"]) for r in manifest["rejected"]}
    assert set(got) == {
        "sym/v0.1.0",
        "broken/v0.1.0",
        "ghost/v0.1.0",
        "big/v99999999999.0.0",
        "tree/v0.1.0",
    }
    assert "symlink" in got["sym/v0.1.0"][1] and got["sym/v0.1.0"][0] == "E_PLUGIN_INVALID"
    assert got["broken/v0.1.0"][0] == "E_PLUGIN_INVALID" and "SKILL.md" in got["broken/v0.1.0"][1]
    assert got["ghost/v0.1.0"][0] == "E_PLUGIN_INVALID"
    assert got["big/v99999999999.0.0"][0] == "E_PLUGIN_INVALID"
    assert "commit" in got["tree/v0.1.0"][1]


def test_run_limits_apply_to_every_tag(repo, run_dir, spool):
    _publish(repo, "kube", "0.1.0")
    manifest = _sync(repo, run_dir, spool, limits={"files": 3})
    assert manifest["versions"] == []
    assert [r["code"] for r in manifest["rejected"]] == ["E_ARCHIVE_TOO_LARGE"]


def test_runtime_tags_become_runtimes_and_bad_ones_are_rejected(repo, run_dir, spool):
    _publish(repo, "kube", "0.1.0")
    repo.write("runtimes/universal/runtime.yaml", GOOD_RUNTIME.encode())
    repo.commit()
    repo.tag("runtimes/universal/v1.0.0")
    repo.write(
        "runtimes/other/runtime.yaml",
        GOOD_RUNTIME.replace("universal", "other").replace("a" * 64, "0" * 64).encode(),
    )
    repo.commit()
    repo.tag("runtimes/other/v1.0.0")  # all-zero placeholder digest
    manifest = _sync(repo, run_dir, spool)
    (rt,) = manifest["runtimes"]
    assert (rt["name"], rt["major"], rt["version"]) == ("universal", 1, "1.0.0")
    assert rt["digests"]["linux/amd64"] == "sha256:" + "b" * 64 and rt["tools"] == {"git": "2.47"}
    assert [r["tag"] for r in manifest["rejected"]] == ["runtimes/other/v1.0.0"]
    assert "placeholder" in manifest["rejected"][0]["message"]
    # a known runtime tag is not read again
    commit = repo.git("rev-parse", "runtimes/universal/v1.0.0^{commit}")
    again = _sync(
        repo, run_dir, spool, known=[{"tag": "runtimes/universal/v1.0.0", "commit": commit}]
    )
    assert again["runtimes"] == []


def test_archives_go_to_slots_in_order_and_unused_slots_are_not_written(repo, run_dir, spool):
    for v in ("0.1.0", "0.2.0", "0.3.0"):
        _publish(repo, "kube", v)
    manifest = _sync(repo, run_dir, spool)
    assert [v["artifact"] for v in manifest["versions"]] == ["archive1", "archive2", "archive3"]
    assert sorted(p.name for p in spool.out_dir.iterdir()) == ["archive1", "archive2", "archive3"]
    for v in manifest["versions"]:
        data = (spool.out_dir / v["artifact"]).read_bytes()
        assert v["archive"]["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert v["archive"]["size"] == len(data)


def test_the_same_bytes_reuse_their_slot(repo, run_dir, spool):
    _publish(repo, "kube", "0.1.0")
    first = _sync(repo, run_dir, spool)
    second = _sync(repo, run_dir, spool)
    assert first == second and [p.name for p in spool.out_dir.iterdir()] == ["archive1"]


def test_running_out_of_slots_stops_the_run_with_more(repo, run_dir, spool):
    for v in ("0.1.0", "0.2.0", "0.3.0"):
        _publish(repo, "kube", v)
    small = type(spool)(spool.out_dir, slots=2)
    manifest = _sync(repo, run_dir, small)
    assert _tags(manifest) == ["kube/v0.3.0", "kube/v0.2.0"] and manifest["more"] is True
    assert manifest["rejected"] == [] and len(list(spool.out_dir.iterdir())) == 2


def test_there_are_twenty_slots_and_max_versions_is_clamped_to_them():
    from plugin_tools.spool import SLOTS

    assert SLOTS == 20
    assert SyncRequest.parse({"id": "a", "url": "https://h/r"}, max_versions=500).max_versions == 20
    assert SyncRequest.parse({"id": "a", "url": "https://h/r"}, max_versions=7).max_versions == 7


def test_listing_failure_fails_the_task(run_dir, spool, tmp_path):
    source = make_source((tmp_path / "missing").as_uri(), run_dir)
    request = SyncRequest.parse(
        {"id": "t", "url": (tmp_path / "missing").as_uri()}, allow_any_scheme=True
    )
    with pytest.raises(RwError) as exc:
        run_sync(request, source, spool, listed_at=LISTED_AT)
    assert exc.value.code in {"not_found", "dependency", "unreachable"} and exc.value.scope == "t"


def test_a_git_failure_on_one_tag_is_unavailable_and_the_rest_sync(
    repo, run_dir, spool, monkeypatch
):
    _publish(repo, "kube", "0.1.0")
    _publish(repo, "pg", "0.1.0")
    source = make_source(repo.url, run_dir)
    real = source.fetch

    def fetch(tag, commit):
        if tag == "pg/v0.1.0":
            raise GitError("git fetch failed: connection reset")
        return real(tag, commit)

    monkeypatch.setattr(source, "fetch", fetch)
    request = SyncRequest.parse({"id": "t", "url": repo.url}, allow_any_scheme=True)
    manifest = run_sync(request, source, spool, listed_at=LISTED_AT)
    assert _tags(manifest) == ["kube/v0.1.0"]
    assert [(r["tag"], r["code"]) for r in manifest["rejected"]] == [("pg/v0.1.0", "E_UNAVAILABLE")]


@pytest.mark.parametrize(
    "message,code",
    [
        (
            "git ls-remote failed: fatal: Authentication failed for 'https://h/r/'",
            "permission_denied",
        ),
        (
            "git ls-remote failed: fatal: could not read Username for 'https://h': terminal prompts disabled",
            "permission_denied",
        ),
        ("git ls-remote failed: remote: Repository not found.", "not_found"),
        (
            "git ls-remote failed: fatal: unable to access 'https://h/': Could not resolve host: h",
            "unreachable",
        ),
        ("git ls-remote failed: something else", "dependency"),
    ],
)
def test_git_failures_map_to_task_error_codes(message, code):
    assert classify_git_error(GitError(message)) == code
    assert classify_git_error(GitTimeout("git fetch timed out after 300s")) == "timeout"


# -- inputs ------------------------------------------------------------------------------------


def test_inputs_are_parsed_and_defaulted():
    req = SyncRequest.parse({"id": "a", "url": "https://h/r"})
    assert req.max_versions == 20 and req.known == set()
    assert (req.limits.archive_bytes, req.limits.files, req.limits.file_bytes) == (
        67108864,
        10000,
        16777216,
    )
    sha = "sha256:" + "a" * 64
    req = SyncRequest.parse(
        {"id": "a", "url": "https://h/r"},
        [{"tag": "k/v1.0.0", "commit": "c" * 40, "sha256": sha}],
        5,
        {"files": 10},
    )
    assert (
        req.known == {("k/v1.0.0", "c" * 40)} and req.max_versions == 5 and req.limits.files == 10
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"known": "nope"},
        {"known": ["x"]},
        {"known": [{"tag": "t", "commit": "short"}]},
        {"known": [{"tag": "t", "commit": "c" * 40, "sha256": "zz"}]},
        {"max_versions": 0},
        {"max_versions": True},
        {"limits": {"files": 10_001}},
        {"limits": {"bogus": 1}},
        {"limits": {"files": "9"}},
        {"limits": []},
    ],
)
def test_bad_inputs_are_invalid(kwargs):
    with pytest.raises(InputInvalid):
        SyncRequest.parse({"id": "a", "url": "https://h/r"}, **kwargs)


def test_bad_source_is_invalid_input():
    with pytest.raises(InputInvalid):
        SyncRequest.parse({"id": "a", "url": "http://h/r"})


def test_tools_version_matches_plugin_yaml():
    text = (Path(PLUGIN) / "plugin.yaml").read_text()
    assert f"version: {TOOLS_VERSION.split('@')[1]}" in text


def test_rejected_tags_do_not_use_up_max_versions(repo, run_dir, spool):
    for i in range(3):
        repo.write_tree(f"plugins/bad{i}", {"plugin.yaml": FileEntry(b"not: a plugin\n")})
        repo.commit()
        repo.tag(f"bad{i}/v9.0.0")
    _publish(repo, "good", "1.0.0")
    manifest = _sync(repo, run_dir, spool, max_versions=1)
    assert _tags(manifest) == ["good/v1.0.0"] and len(manifest["rejected"]) == 3
    assert manifest["more"] is False
