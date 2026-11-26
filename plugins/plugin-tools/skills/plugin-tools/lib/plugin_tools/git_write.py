"""Writing a plugin tree back to a git source: one commit on a branch, an optional tag, one push.

PORTED BUT NOT EXPOSED (decision: git write-back ships later). No task in this plugin calls it;
it is kept, with its tests, so the code and its hardening survive the move out of the registry.
It runs through the same hardened `GitRunner` as the read side.

Everything runs in the source's bare repo (the one `sync` fetches into), under the source's
exclusive lock, and through `GitSource.git` so the credential reaches only the calls that talk to
the remote and every message is scrubbed. The new tree is built without a checkout and without a
work tree: the branch's tip is read into a throwaway index, the entries under the plugin's `path`
are replaced by the uploaded files (their bytes go in with `hash-object --no-filters` and their
entries with `update-index --index-info`, so no attribute can change what is stored), and
`commit-tree` makes the commit. Nothing outside the plugin's `path` is touched, so the rest of the
repository stays as it was.

The one exception inside `path` is what the archive leaves out (the root tests directory,
`runwhen.lock`, `.runwhen/` and each skill's tests directory): an archive-built upload carries none of it, so when the upload
holds no such path the tip's own are kept. An upload that carries any of them is authoritative for
the whole directory.

The branch and the tag are pushed in one `--atomic` push, so a rejected tag never leaves a
published commit behind. A push that loses a race for the branch is retried once on the new tip.
"""

import hashlib
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from plugin_tools.archive import E_INVALID_PLUGIN, ArchiveError, FileEntry, is_excluded
from plugin_tools.gitrun import GitError, GitTimeout
from plugin_tools.gitsource import GitSource

log = logging.getLogger(__name__)

# Where the fetched tip of the branch lives in the bare repo while a write runs.
TIP_REF = "refs/registry-write/tip"
# What git would otherwise take from a developer's or the image's own configuration.
_TREE_CONFIG = [
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.filemode=true",
    "-c",
    "core.symlinks=false",
]
# `git push --porcelain` reasons that mean the branch moved under us (a retry can fix them).
_BRANCH_MOVED = ("fetch first", "non-fast-forward", "cannot lock ref", "stale info")
_TAG_EXISTS = "already exists"
_MODE_FILE = "100644"
_MODE_EXEC = "100755"


class BranchMoved(Exception):
    """The branch moved during the write, twice in a row; nothing was pushed."""


class TagExists(Exception):
    """The remote already has the tag (at `commit`, peeled for an annotated tag)."""

    def __init__(self, commit: str) -> None:
        super().__init__(f"tag already exists at {commit}")
        self.commit = commit


@dataclass(frozen=True)
class CommitOutcome:
    commit: str  # the new commit, or the unchanged tip when nothing changed
    branch: str
    tag: str | None
    changed: bool  # False: the tree equals the tip's and nothing was pushed


@dataclass(frozen=True)
class _TipEntry:
    """One file entry of the tip's tree: the raw path bytes, git mode and object id."""

    name: bytes
    mode: str
    oid: str


def _blob_oid(data: bytes) -> str:
    """The id git gives a blob (sha-1 object format)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _rejected_refs(porcelain: str) -> dict[str, str]:
    """Destination ref -> reason for each `!` line of `git push --porcelain` output.

    A line is `!<TAB><from>:<to><TAB>[rejected] (<reason>)`.
    """
    out: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line.startswith("!"):
            continue
        _, _, rest = line.partition("\t")
        refspec, _, summary = rest.partition("\t")
        out[refspec.partition(":")[2]] = summary
    return out


@dataclass(frozen=True)
class WriteIdentity:
    """The commit author and committer; there is no per-user git identity."""

    authorName: str = "RunWhen plugin tools"
    authorEmail: str = "plugin-tools@runwhen.invalid"


class GitWriter:
    def __init__(self, source: GitSource, identity: WriteIdentity | None = None) -> None:
        self.source = source
        self.write = identity or WriteIdentity()

    def commit_tree(
        self,
        files: Mapping[str, FileEntry],
        path: str,
        *,
        branch: str,
        base_branch: str,
        message: str,
        tag: str | None,
    ) -> CommitOutcome:
        """Replace the files under `path` with `files` on `branch`, tag the commit, push.

        `branch` starts from `base_branch` when the remote has no such branch, and as a root
        commit when it has neither. `path` "" is the repository root (everything else goes).
        With no `tag` and nothing changed, nothing is committed; a `branch` the remote does not
        have yet is still created, at the base branch's tip.

        Raises `TagExists` (nothing pushed), `BranchMoved` (the branch moved on two attempts),
        `GitError`, and `ArchiveError` when git would store the files differently from `files`.
        """
        path = path.strip("/")
        if path == ".":
            path = ""
        for attempt in (1, 2):
            try:
                return self._attempt(files, path, branch, base_branch, message, tag)
            except BranchMoved:
                if attempt == 2:
                    raise
                log.info("branch %s of source %s moved during a write; retrying", branch, self._id)
        raise AssertionError("unreachable")

    @property
    def _id(self) -> str:
        return self.source.spec.id

    def _attempt(
        self,
        files: Mapping[str, FileEntry],
        path: str,
        branch: str,
        base_branch: str,
        message: str,
        tag: str | None,
    ) -> CommitOutcome:
        tip, on_branch = self._fetch_tip(branch, base_branch)
        tree = self._write_tree(tip, files, path)
        if tip is not None and tag is None:
            if tree == self.source.git("rev-parse", f"{tip}^{{tree}}").decode().strip():
                if not on_branch:
                    # The tip is the base branch's: nothing to commit, but `branch` must exist.
                    self._push(tip, branch, None)
                return CommitOutcome(commit=tip, branch=branch, tag=None, changed=False)
        commit = self._commit(tree, tip, message)
        self._push(commit, branch, tag)
        return CommitOutcome(commit=commit, branch=branch, tag=tag, changed=True)

    # -- steps ---------------------------------------------------------------------------

    def _remote_has_branch(self, branch: str) -> bool:
        ref = f"refs/heads/{branch}"
        out = self.source.git("ls-remote", "--heads", "--", self.source.spec.url, ref, remote=True)
        return any(
            line.partition("\t")[2] == ref for line in out.decode("utf-8", "replace").splitlines()
        )

    def _fetch_tip(self, branch: str, base_branch: str) -> tuple[str | None, bool]:
        """Fetch the branch's tip (or the base branch's) into `TIP_REF`.

        Returns the tip (None: no parent) and whether it is `branch`'s own, not the base's.
        """
        src = self.source
        src.git("update-ref", "-d", TIP_REF)  # never read a tip left by an earlier write
        for name in dict.fromkeys((branch, base_branch)):
            if not self._remote_has_branch(name):
                continue
            src.git(
                "fetch",
                "--depth",
                "1",
                "--no-tags",
                "--",
                src.spec.url,
                f"+refs/heads/{name}:{TIP_REF}",
                remote=True,
            )
            tip = src.git("rev-parse", "--verify", f"{TIP_REF}^{{commit}}").decode().strip()
            return tip, name == branch
        return None, False

    def _write_tree(self, tip: str | None, files: Mapping[str, FileEntry], path: str) -> str:
        """The tree of `tip` with the files under `path` replaced by `files`.

        No work tree and no `add`: the bytes go in with `hash-object --no-filters` and the
        entries with `update-index --index-info`, so no attribute (`text`, `eol`, `filter`,
        `working-tree-encoding`, whether from the upload or from a `.gitattributes` already in the
        repository) can change what is stored. The tip's archive-excluded entries under `path`
        stay when `files` has none of its own (see the module docstring).
        """
        src = self.source
        src.runner.workdir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=f"write-{self._id}-", dir=src.runner.workdir))
        try:
            env = {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_INDEX_FILE": str(scratch / "index"),
                "GIT_LITERAL_PATHSPECS": "1",
            }
            oids = self._hash_blobs(scratch, files, env)
            prefix = f"{path}/" if path else ""
            old = self._tip_entries(tip, path, env) if tip is not None else []
            keep = self._preserved(old, files, prefix)
            if tip is None or (not path and not keep):
                src.git(*_TREE_CONFIG, "read-tree", "--empty", env=env)
            else:
                src.git(*_TREE_CONFIG, "read-tree", tip, env=env)
                self._update_index(self._removals(old, keep), env)
            adds = b"".join(
                f"{_MODE_EXEC if e.executable else _MODE_FILE} {oid}\t{prefix}{rel}".encode()
                + b"\0"
                for (rel, e), oid in zip(files.items(), oids, strict=True)
            )
            self._update_index(adds, env)
            tree = src.git(*_TREE_CONFIG, "write-tree", env=env).decode().strip()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._check_stored(tree, path, files, keep)
        return tree

    def _hash_blobs(
        self, scratch: Path, files: Mapping[str, FileEntry], env: dict[str, str]
    ) -> list[str]:
        """Write each file's bytes to the object store unfiltered; the ids, in `files` order."""
        blobs = scratch / "blobs"
        blobs.mkdir()
        listed = []
        for i, (rel, entry) in enumerate(files.items()):
            if rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"unsafe path in the tree: {rel!r}")
            target = blobs / str(i)  # numbered: the upload's names never reach the file system
            target.write_bytes(entry.data)
            listed.append(str(target))
        if not listed:
            return []
        out = self.source.git(
            "hash-object",
            "-w",
            "--no-filters",
            "--stdin-paths",
            env=env,
            stdin=("\n".join(listed) + "\n").encode(),
        )
        oids = out.decode("ascii").split()
        if len(oids) != len(listed):
            raise GitError("git hash-object returned the wrong number of objects")
        return oids

    def _tip_entries(self, tip: str, path: str, env: dict[str, str]) -> list[_TipEntry]:
        """Every file entry of `tip` under `path` (the whole tree for the root)."""
        pathspec = ["--", path] if path else []
        listing = self.source.git("ls-tree", "-r", "-z", tip, *pathspec, env=env)
        entries = []
        for rec in listing.split(b"\0"):
            if rec:
                meta, _, name = rec.partition(b"\t")
                mode, _, oid = meta.decode("ascii").split()
                entries.append(_TipEntry(name, mode, oid))
        return entries

    @staticmethod
    def _preserved(
        old: list[_TipEntry], files: Mapping[str, FileEntry], prefix: str
    ) -> dict[str, _TipEntry]:
        """The tip's archive-excluded entries (by path relative to `path`) that stay.

        Empty when `files` carries an excluded path itself: the upload is then authoritative.
        """
        if any(is_excluded(rel) for rel in files):
            return {}
        kept = {}
        for entry in old:
            text = entry.name.decode("utf-8", "surrogateescape")
            if text.startswith(prefix) and is_excluded(rel := text.removeprefix(prefix)):
                kept[rel] = entry
        return kept

    @staticmethod
    def _removals(old: list[_TipEntry], keep: Mapping[str, _TipEntry]) -> bytes:
        """`update-index --index-info` input that drops every entry of `old` not in `keep`."""
        kept = {e.name for e in keep.values()}
        zeros = b"0" * len(old[0].oid) if old else b""
        return b"".join(b"0 " + zeros + b"\t" + e.name + b"\0" for e in old if e.name not in kept)

    def _update_index(self, info: bytes, env: dict[str, str]) -> None:
        if not info:
            return
        try:
            self.source.git(
                *_TREE_CONFIG,
                "update-index",
                "--add",
                "--replace",
                "-z",
                "--index-info",
                env=env,
                stdin=info,
            )
        except GitError as exc:
            if "Invalid path" not in str(exc):
                raise
            raise ArchiveError(
                E_INVALID_PLUGIN,
                "a path in the plugin cannot be stored in git (for example a .git directory)",
            ) from None

    def _check_stored(
        self, tree: str, path: str, files: Mapping[str, FileEntry], keep: Mapping[str, _TipEntry]
    ) -> None:
        """Refuse unless the tree holds exactly the uploaded paths, modes and blob ids.

        Plus the tip's own entries that were kept (`keep`), unchanged. The blob id is computed
        here from the bytes, so this holds even if the steps above were changed to a path that
        filters.
        """
        pathspec = ["--", path] if path else []
        listing = self.source.git(
            "ls-tree", "-r", "-z", tree, *pathspec, env={"GIT_LITERAL_PATHSPECS": "1"}
        )
        prefix = f"{path}/" if path else ""
        stored: dict[str, tuple[str, str]] = {}
        for rec in listing.split(b"\0"):
            if not rec:
                continue
            meta, _, name = rec.partition(b"\t")
            mode, _, oid = meta.decode("ascii").split()
            stored[name.decode("utf-8", "surrogateescape").removeprefix(prefix)] = (mode, oid)
        expected = {
            rel: (_MODE_EXEC if entry.executable else _MODE_FILE, _blob_oid(entry.data))
            for rel, entry in files.items()
        }
        expected.update({rel: (e.mode, e.oid) for rel, e in keep.items()})
        if stored != expected:
            raise ArchiveError(
                E_INVALID_PLUGIN, "git did not store the files exactly as uploaded; nothing pushed"
            )

    def _commit(self, tree: str, parent: str | None, message: str) -> str:
        who = self.write
        args = [*_TREE_CONFIG, "commit-tree", tree]
        if parent is not None:
            args += ["-p", parent]
        env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": who.authorName,
            "GIT_AUTHOR_EMAIL": who.authorEmail,
            "GIT_COMMITTER_NAME": who.authorName,
            "GIT_COMMITTER_EMAIL": who.authorEmail,
        }
        return self.source.git(*args, "-m", message, env=env).decode().strip()

    def _push(self, commit: str, branch: str, tag: str | None) -> None:
        src = self.source
        refspecs = [f"{commit}:refs/heads/{branch}"]
        if tag is not None:
            refspecs.append(f"{commit}:refs/tags/{tag}")
        try:
            src.git(
                "push",
                "--atomic",
                "--porcelain",
                "--",
                src.spec.url,
                *refspecs,
                remote=True,
            )
        except GitTimeout:
            raise
        except GitError as exc:
            rejected = _rejected_refs(exc.stdout)
            # With --atomic every ref of a failed push is rejected, so what git blames on the
            # tag is checked first: an existing tag is an answer, a moved branch only a retry.
            if tag is not None and _TAG_EXISTS in rejected.get(f"refs/tags/{tag}", ""):
                raise TagExists(self._remote_tag_commit(tag)) from None
            reason = rejected.get(f"refs/heads/{branch}", "")
            if any(marker in reason for marker in _BRANCH_MOVED):
                raise BranchMoved(f"branch {branch} moved") from None
            raise

    def _remote_tag_commit(self, tag: str) -> str:
        commit = self.source.list_tags().get(tag)
        if commit is None:
            raise GitError(f"tag {tag} exists on the remote but its commit cannot be read")
        return commit
