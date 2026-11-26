"""The read side of a git source, against real local repositories (ported from the registry's tests).

`conftest.make_source` builds a GitSource on a hardened runner that may read file:// (tests only).
"""

import pytest
from conftest import make_source
from fixtures import make_plugin_tree
from plugin_tools.archive import ArchiveError, FileEntry, Limits
from plugin_tools.gitrun import GitError
from plugin_tools.gitsource import SourceInvalid, SourceSpec, tag_regex

TOKEN = "ghs_0123456789abcdefghij"


def _tree(version="0.1.0", name="kube"):
    return make_plugin_tree(name, version)


def _read(source, repo, tag, path="plugins/kube", **kw):
    source.fetch(tag, repo.git("rev-parse", f"{tag}^{{commit}}"))
    return source.read_tree(repo.git("rev-parse", f"{tag}^{{commit}}"), path, **kw)


def test_list_tags_peels_annotated_tags(repo, source):
    commit = repo.publish("plugins/kube", _tree(), "kube/v0.1.0", annotated=True)
    light = repo.publish("plugins/kube", _tree("0.2.0"), "kube/v0.2.0")
    assert source.list_tags() == {"kube/v0.1.0": commit, "kube/v0.2.0": light}


def test_tags_for_other_layouts_are_ignored(repo, source):
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    for t in ("kube/v1.0", "KUBE-v1.0.0", "kube/1.0.0", "kube/v1.0.0/extra", "v1.0.0"):
        repo.tag(t)
    assert [t.tag for t in source.matching_tags(source.list_tags())] == ["kube/v0.1.0"]


def test_runtime_tags_are_not_plugin_tags(repo, source):
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    repo.tag("runtimes/universal/v1.0.0")
    tags = source.list_tags()
    assert [t.tag for t in source.matching_tags(tags)] == ["kube/v0.1.0"]
    assert [(t.name, t.version) for t in source.matching_runtime_tags(tags)] == [
        ("universal", "1.0.0")
    ]


def test_custom_tag_pattern_and_path(repo, run_dir):
    repo.write_tree("src/solo", _tree("2.0.0-rc.1", name="solo"))
    repo.commit()
    repo.tag("solo-2.0.0-rc.1")
    source = make_source(repo.url, run_dir, tagPattern="{name}-{version}", path="src/{name}")
    (ref,) = source.matching_tags(source.list_tags())
    assert (ref.name, ref.version) == ("solo", "2.0.0-rc.1")


def test_single_plugin_repo_root_tags(repo, run_dir):
    for rel, entry in _tree("1.0.0", name="solo").items():
        repo.write(rel, entry.data, entry.executable)
    repo.commit()
    repo.tag("v1.0.0")
    source = make_source(repo.url, run_dir, tagPattern="v{version}", path="", plugin="solo")
    (ref,) = source.matching_tags(source.list_tags())
    assert (ref.name, ref.version) == ("solo", "1.0.0")
    files = _read(source, repo, "v1.0.0", path="")
    assert "plugin.yaml" in files


@pytest.mark.parametrize(
    "pattern", ["{name}+{version}", "{name}.v{version}", "a(b)/{name}-{version}"]
)
def test_tag_pattern_literals_are_escaped(pattern):
    m = tag_regex(pattern).fullmatch(
        pattern.replace("{name}", "kube").replace("{version}", "1.0.0")
    )
    assert m and (m["name"], m["version"]) == ("kube", "1.0.0")
    assert (
        tag_regex(pattern).fullmatch(
            pattern.replace("{name}", "kube").replace("{version}", "1x0y0")
        )
        is None
    )


def test_untracked_and_ignored_files_never_in_the_tree(repo, source):
    repo.write(".gitignore", b"ignored.txt\n")
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    tracked = len(_tree())
    repo.write("plugins/kube/untracked.txt", b"secret")
    repo.write("plugins/kube/ignored.txt", b"secret")
    files = _read(source, repo, "kube/v0.1.0")
    assert len(files) == tracked
    assert not any("untracked" in p or "ignored" in p for p in files)


def test_excluded_paths_are_not_read(repo, source):
    tree = _tree()
    tree["tests/big.json"] = FileEntry(b"{}")
    tree["runwhen.lock"] = FileEntry(b"{}")
    tree["skills/a/tests/run/x.yaml"] = FileEntry(b"{}")
    repo.publish("plugins/kube", tree, "kube/v0.1.0")
    files = _read(source, repo, "kube/v0.1.0")
    assert not any(p.startswith("tests/") or "/tests/" in p or p == "runwhen.lock" for p in files)


def test_exec_bit_comes_from_the_git_mode(repo, source):
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    files = _read(source, repo, "kube/v0.1.0")
    assert files["skills/a/scripts/run.sh"].executable is True
    assert files["skills/a/lib/helpers.sh"].executable is False
    assert files["skills/a/lib/helpers.sh"].data == b"# helpers\n"


def test_binary_and_empty_files_round_trip(repo, source):
    blob = bytes(range(256)) * 50
    tree = _tree()
    tree["assets/blob.bin"] = FileEntry(blob)
    tree["assets/empty"] = FileEntry(b"")
    tree["assets/same.bin"] = FileEntry(blob)
    repo.publish("plugins/kube", tree, "kube/v0.1.0")
    files = _read(source, repo, "kube/v0.1.0")
    assert files["assets/blob.bin"].data == files["assets/same.bin"].data == blob
    assert files["assets/empty"].data == b""


def test_symlink_in_the_tree_is_rejected(repo, source):
    repo.write_tree("plugins/kube", _tree())
    repo.symlink("plugins/kube/skills/a/lib/link", "/etc/passwd")
    repo.commit()
    repo.tag("kube/v0.1.0")
    with pytest.raises(ArchiveError, match="symlink") as exc:
        _read(source, repo, "kube/v0.1.0")
    assert exc.value.code == "E_INVALID_PLUGIN"


def test_submodule_gitlink_is_rejected(repo, source):
    repo.write_tree("plugins/kube", _tree())
    repo.git("add", "-A")
    repo.gitlink("plugins/kube/vendor")
    repo.git("commit", "-q", "-m", "submodule")
    repo.tag("kube/v0.1.0")
    with pytest.raises(ArchiveError, match="submodule"):
        _read(source, repo, "kube/v0.1.0")


def test_read_tree_honours_the_run_limits(repo, source):
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    with pytest.raises(ArchiveError) as exc:
        _read(source, repo, "kube/v0.1.0", limits=Limits(files=2))
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"
    with pytest.raises(ArchiveError) as exc:
        _read(source, repo, "kube/v0.1.0", limits=Limits(file_bytes=5))
    assert exc.value.code == "E_ARCHIVE_TOO_LARGE"


def test_a_tag_pointing_at_a_tree_is_not_a_commit(repo, source):
    repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    repo.git("tag", "kube/v0.2.0", "HEAD^{tree}")
    tags = source.list_tags()
    source.fetch("kube/v0.2.0", tags["kube/v0.2.0"])
    assert source.object_type(tags["kube/v0.2.0"]) == "tree"


def test_fetch_falls_back_to_the_tag_ref_when_the_sha_is_refused(repo, source, monkeypatch):
    commit = repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    real = source.git
    seen = []

    def git(*args, **kw):
        if args[:1] == ("fetch",):
            seen.append(args[-1])
            if args[-1] == commit:
                raise GitError("git fetch failed: not our ref")
        return real(*args, **kw)

    monkeypatch.setattr(source, "git", git)
    source.fetch("kube/v0.1.0", commit)
    assert seen == [commit, "refs/tags/kube/v0.1.0"]
    assert source.object_type(commit) == "commit"


def test_a_stale_shallow_lock_is_cleared_and_the_fetch_retried_once(repo, source, monkeypatch):
    commit = repo.publish("plugins/kube", _tree(), "kube/v0.1.0")
    source.runner.ensure_repo()
    (source.gitdir / "shallow.lock").write_text("")
    with pytest.raises(GitError, match="File exists"):
        source._fetch_commit("kube/v0.1.0", commit)  # really breaks without the retry
    source.fetch("kube/v0.1.0", commit)
    assert not (source.gitdir / "shallow.lock").exists()
    assert source.object_type(commit) == "commit"


def test_a_non_lock_fetch_error_is_not_retried(repo, source, monkeypatch):
    calls = []

    def git(*args, **kw):
        calls.append(args)
        raise GitError("git fetch failed: boom")

    monkeypatch.setattr(source, "git", git)
    with pytest.raises(GitError, match="boom"):
        source.fetch("kube/v0.1.0", "a" * 40)
    assert len([c for c in calls if c[0] == "fetch"]) == 2  # sha, then the tag ref; no lock retry


def test_unreachable_remote_is_a_git_error_without_the_url_userinfo(run_dir, tmp_path):
    missing = (tmp_path / "nope").as_uri()
    source = make_source(missing, run_dir)
    with pytest.raises(GitError):
        source.list_tags()


# -- the source input --------------------------------------------------------------------------


def test_source_spec_defaults_and_rules():
    s = SourceSpec.parse({"id": "pub", "url": "https://github.com/o/r"})
    assert (s.tag_pattern, s.path, s.plugin) == ("{name}/v{version}", "plugins/{name}", None)
    solo = SourceSpec.parse(
        {"id": "s", "url": "https://h/r", "tagPattern": "v{version}", "plugin": "solo", "path": ""}
    )
    assert solo.plugin == "solo"


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "a", "url": "http://h/r"},  # cleartext
        {"id": "a", "url": "ssh://git@h/r"},
        {"id": "a", "url": "file:///tmp/r"},
        {"id": "a", "url": "https://user:pw@h/r"},
        {"id": "a", "url": "https://h/r with space"},
        {"id": "A!", "url": "https://h/r"},
        {"id": "a", "url": "https://h/r", "extra": 1},
        {"id": "a", "url": "https://h/r", "tagPattern": "runtimes/{name}/v{version}"},
        {"id": "a", "url": "https://h/r", "tagPattern": "{name}"},
        {"id": "a", "url": "https://h/r", "tagPattern": "v{version}"},  # no {name}, no plugin
        {"id": "a", "url": "https://h/r", "path": "../x"},
        {"id": "a", "url": "https://h/r", "path": "/abs"},
        {
            "id": "a",
            "url": "https://h/r",
            "tagPattern": "v{version}",
            "plugin": "p",
            "path": "{name}",
        },
        {"id": "a", "url": "https://h/r", "plugin": "p"},  # plugin set but pattern has {name}
    ],
)
def test_source_spec_refuses(raw):
    with pytest.raises(SourceInvalid):
        SourceSpec.parse(raw)
