"""GitWriter: commit a plugin tree onto a branch of a real bare remote, tag it, push atomically.

Ported from the registry's tests. The writer is not exposed as a task (git write-back ships
later); these tests keep its behaviour pinned. The runner may read/push file:// here (tests only).
"""

import pytest
from conftest import make_source
from plugin_tools.archive import ArchiveError, FileEntry
from plugin_tools.git_write import BranchMoved, GitWriter, TagExists, WriteIdentity
from plugin_tools.gitrun import GitError


def _writer(remote, run_dir, **write) -> GitWriter:
    return GitWriter(make_source(remote.url, run_dir), WriteIdentity(**write))


def _files(**named: bytes) -> dict[str, FileEntry]:
    return {name.replace("__", "/"): FileEntry(data) for name, data in named.items()}


def _commit(writer, files, path="plugins/kube", tag="kube/v1.0.0", message="m", branch="main"):
    return writer.commit_tree(
        files, path, branch=branch, base_branch="main", message=message, tag=tag
    )


def test_commit_tree_commits_tags_and_pushes(remote, run_dir, tmp_path):
    outcome = _commit(_writer(remote, run_dir), _files(a=b"1", sub__b=b"2"), message="kube v1.0.0")
    assert outcome.changed and outcome.branch == "main" and outcome.tag == "kube/v1.0.0"
    assert remote.refs() == {
        "refs/heads/main": outcome.commit,
        "refs/tags/kube/v1.0.0": outcome.commit,
    }
    assert remote.tree("main") == {
        "plugins/kube/a": ("100644", b"1"),
        "plugins/kube/sub/b": ("100644", b"2"),
    }
    assert remote.message("main") == "kube v1.0.0"


def test_commit_uses_the_configured_identity(remote, run_dir, tmp_path):
    writer = _writer(remote, run_dir, authorName="Bot", authorEmail="bot@example.invalid")
    _commit(writer, _files(a=b"1"))
    assert remote.author("main") == "Bot <bot@example.invalid>|Bot <bot@example.invalid>"


def test_empty_remote_gets_a_root_commit(remote, run_dir, tmp_path):
    outcome = _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert remote.git("rev-list", "--parents", "-n1", "main") == outcome.commit  # no parent
    assert remote.head() == outcome.commit


def test_other_repo_content_is_untouched(remote, run_dir, tmp_path):
    first = remote.commit_files(
        {"README.md": b"hello", "plugins/other/x": b"x", "plugins/kube/old": b"old"}
    )
    outcome = _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert remote.tree("main") == {
        "README.md": ("100644", b"hello"),
        "plugins/other/x": ("100644", b"x"),
        "plugins/kube/a": ("100644", b"1"),
    }
    assert remote.git("rev-parse", f"{outcome.commit}^") == first


def test_files_removed_from_the_plugin_are_gone(remote, run_dir, tmp_path):
    writer = _writer(remote, run_dir)
    _commit(writer, _files(a=b"1", gone=b"x", d__deep=b"y"), tag="kube/v1.0.0")
    _commit(writer, _files(a=b"2"), tag="kube/v1.1.0")
    assert remote.tree("kube/v1.0.0").keys() == {
        "plugins/kube/a",
        "plugins/kube/gone",
        "plugins/kube/d/deep",
    }
    assert remote.tree("kube/v1.1.0") == {"plugins/kube/a": ("100644", b"2")}


def test_repo_root_path_replaces_the_whole_tree(remote, run_dir, tmp_path):
    remote.commit_files({"stale.txt": b"s", "dir/stale": b"s"})
    _commit(_writer(remote, run_dir), _files(a=b"1"), path="")
    assert remote.tree("main") == {"a": ("100644", b"1")}


def test_executable_bit_is_a_git_mode(remote, run_dir, tmp_path):
    files = {"run.sh": FileEntry(b"#!/bin/sh\n", executable=True), "plain": FileEntry(b"p")}
    _commit(_writer(remote, run_dir), files)
    assert remote.tree("main") == {
        "plugins/kube/run.sh": ("100755", b"#!/bin/sh\n"),
        "plugins/kube/plain": ("100644", b"p"),
    }


def test_unchanged_tree_without_a_tag_pushes_nothing(remote, run_dir, tmp_path):
    writer = _writer(remote, run_dir)
    first = _commit(writer, _files(a=b"1"), tag=None)
    refs = remote.refs()
    again = _commit(writer, _files(a=b"1"), tag=None)
    assert (again.changed, again.commit) == (False, first.commit)
    assert remote.refs() == refs


def test_new_branch_starts_from_the_base_branch(remote, run_dir, tmp_path):
    base = remote.commit_files({"README.md": b"hi"})
    outcome = _writer(remote, run_dir).commit_tree(
        _files(a=b"1"), "plugins/kube", branch="drafts/x", base_branch="main", message="d", tag=None
    )
    assert remote.head("drafts/x") == outcome.commit
    assert remote.head("main") == base  # the base branch did not move
    assert remote.git("rev-parse", f"{outcome.commit}^") == base
    assert "README.md" in remote.tree("drafts/x")


def test_tag_that_exists_raises_tag_exists_with_its_commit(remote, run_dir, tmp_path):
    other = remote.commit_files({"plugins/kube/a": b"theirs"}, tag="kube/v1.0.0")
    refs = remote.refs()
    with pytest.raises(TagExists) as exc:
        _commit(_writer(remote, run_dir), _files(a=b"mine"))
    assert exc.value.commit == other
    assert remote.refs() == refs  # atomic: the branch did not move either


def test_annotated_tag_reports_the_commit_it_points_at(remote, run_dir, tmp_path):
    other = remote.commit_files({"plugins/kube/a": b"theirs"})
    remote.seed.git("tag", "-a", "-m", "t", "kube/v1.0.0")
    remote.seed.git("push", "-q", "origin", "refs/tags/kube/v1.0.0")
    with pytest.raises(TagExists) as exc:
        _commit(_writer(remote, run_dir), _files(a=b"mine"))
    assert exc.value.commit == other


def _move_branch_after_fetch(monkeypatch, remote, times):
    """The remote's branch moves between the writer's fetch and its push, `times` times."""
    calls = []
    original = GitWriter._fetch_tip

    def fetch_then_move(self, *args, **kw):
        tip = original(self, *args, **kw)
        calls.append(tip)
        if len(calls) <= times:
            remote.commit_files({f"moved{len(calls)}": b"m"}, message="someone else")
        return tip

    monkeypatch.setattr(GitWriter, "_fetch_tip", fetch_then_move)
    return calls


def test_branch_moved_once_is_retried_on_the_new_tip(monkeypatch, remote, run_dir, tmp_path):
    calls = _move_branch_after_fetch(monkeypatch, remote, times=1)
    outcome = _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert len(calls) == 2 and calls[0] != calls[1]
    assert remote.head() == outcome.commit
    assert remote.tree("main").keys() == {"moved1", "plugins/kube/a"}  # their commit is kept
    assert remote.refs()["refs/tags/kube/v1.0.0"] == outcome.commit


def test_branch_moved_twice_raises_branch_moved(monkeypatch, remote, run_dir, tmp_path):
    calls = _move_branch_after_fetch(monkeypatch, remote, times=99)
    with pytest.raises(BranchMoved):
        _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert len(calls) == 2  # one retry, no more
    assert "refs/tags/kube/v1.0.0" not in remote.refs()
    assert "plugins/kube/a" not in remote.tree("main")


def test_tag_wins_over_a_moved_branch(monkeypatch, remote, run_dir, tmp_path):
    """With --atomic both refs are rejected; an existing tag is the answer, not a retry."""
    other = remote.commit_files({"plugins/kube/a": b"theirs"}, tag="kube/v1.0.0")
    original = GitWriter._fetch_tip

    def fetch_then_move(self, *args, **kw):
        tip = original(self, *args, **kw)
        remote.commit_files({"later": b"m"})
        return tip

    monkeypatch.setattr(GitWriter, "_fetch_tip", fetch_then_move)
    with pytest.raises(TagExists) as exc:
        _commit(_writer(remote, run_dir), _files(a=b"mine"))
    assert exc.value.commit == other


UTF16 = b"A\x00-N"  # "A" + U+4E2D in UTF-16LE: converting it to UTF-8 keeps 4 bytes ("A" + 3)


def test_attributes_in_the_upload_are_content_not_filters(remote, run_dir, tmp_path):
    """`* text` in an uploaded .gitattributes would turn CRLF into LF in `git add`; it must not."""
    files = _files(a=b"one\r\ntwo\r\n", b=UTF16)
    files[".gitattributes"] = FileEntry(b"* text\nb working-tree-encoding=UTF-16LE\n")
    _commit(_writer(remote, run_dir), files)
    assert remote.tree("main") == {
        "plugins/kube/a": ("100644", b"one\r\ntwo\r\n"),
        "plugins/kube/b": ("100644", UTF16),
        "plugins/kube/.gitattributes": ("100644", b"* text\nb working-tree-encoding=UTF-16LE\n"),
    }


def test_attributes_already_in_the_repo_cannot_change_the_content(remote, run_dir, tmp_path):
    remote.commit_files({".gitattributes": b"* text eol=lf\n*.md working-tree-encoding=UTF-16LE\n"})
    files = {"README.md": FileEntry(UTF16), "crlf": FileEntry(b"one\r\n")}
    _commit(_writer(remote, run_dir), files)
    tree = remote.tree("main")
    assert tree["plugins/kube/README.md"] == ("100644", UTF16)
    assert tree["plugins/kube/crlf"] == ("100644", b"one\r\n")
    assert tree[".gitattributes"][0] == "100644"  # and the repo's own file is still there


def test_awkward_names_and_empty_files_round_trip(remote, run_dir, tmp_path):
    files = {
        "with space.txt": FileEntry(b"s"),
        "dir with space/a,b.txt": FileEntry(b"c"),
        "h\u00e9llo-\u00fc\u4e2d.txt": FileEntry(b"u"),
        "-dash": FileEntry(b"d"),
        "empty": FileEntry(b""),
        "bin": FileEntry(bytes(range(256)), executable=True),
    }
    _commit(_writer(remote, run_dir), files)
    assert remote.tree("main") == {
        f"plugins/kube/{p}": ("100755" if e.executable else "100644", e.data)
        for p, e in files.items()
    }


@pytest.mark.parametrize("name", ["sub/.git/HEAD", ".git/config", "x/.GIT/a"])
def test_a_dot_git_path_is_refused_and_nothing_is_pushed(remote, run_dir, tmp_path, name):
    with pytest.raises(ArchiveError) as exc:
        _commit(_writer(remote, run_dir), _files(ok=b"1") | {name: FileEntry(b"x")})
    assert exc.value.code == "E_INVALID_PLUGIN"
    assert remote.refs() == {}


def test_a_file_in_the_way_of_the_plugin_directory_is_replaced(remote, run_dir, tmp_path):
    remote.commit_files({"plugins": b"a file where the directory goes"})
    _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert remote.tree("main") == {"plugins/kube/a": ("100644", b"1")}


def test_client_hooks_from_the_operators_config_do_not_run(monkeypatch, remote, run_dir, tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\necho hook ran > {tmp_path}/ran\nexit 1\n")
    hook.chmod(0o755)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    _commit(_writer(remote, run_dir), _files(a=b"1"))
    assert remote.head() is not None
    assert not (tmp_path / "ran").exists()


def test_global_git_config_cannot_change_the_tree(monkeypatch, remote, run_dir, tmp_path):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = true\n\tfilemode = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    files = {"run.sh": FileEntry(b"a\r\nb\r\n", executable=True)}
    _commit(_writer(remote, run_dir), files)
    assert remote.tree("main") == {"plugins/kube/run.sh": ("100755", b"a\r\nb\r\n")}


def test_push_failure_is_a_git_error(remote, run_dir, tmp_path):
    writer = _writer(remote, run_dir)
    remote.git("config", "receive.denyNonFastForwards", "true")
    hook = remote.path / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(GitError) as exc:
        _commit(writer, _files(a=b"1"))
    assert "policy says no" in str(exc.value)
    assert remote.refs() == {}


def test_work_dirs_are_cleaned_up(remote, run_dir, tmp_path):
    _commit(_writer(remote, run_dir), _files(a=b"1"))
    left = [p.name for p in run_dir.work.iterdir() if p.name != "repo.git"]
    assert left == []
