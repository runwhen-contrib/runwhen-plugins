"""Build real local git repos for the git tests (no network, no global git config). Ported from the registry's tests."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from plugin_tools.archive import FileEntry

_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitRepo:
    """A work tree with a `.git`; `url` is what a registry source would be pointed at."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")

    @property
    def url(self) -> str:
        return self.path.resolve().as_uri()

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={**os.environ, **_ENV},
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def write(self, rel: str, data: bytes, executable: bool = False) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if executable else 0o644)

    def write_tree(self, prefix: str, tree: Mapping[str, FileEntry]) -> None:
        """Replace everything under `prefix` with `tree`."""
        shutil.rmtree(self.path / prefix, ignore_errors=True)
        for rel, entry in tree.items():
            self.write(f"{prefix}/{rel}", entry.data, entry.executable)

    def symlink(self, rel: str, target: str) -> None:
        link = self.path / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    def gitlink(self, rel: str, sha: str = "1" * 40) -> None:
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}")

    def commit(self, message: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, force: bool = False, annotated: bool = False) -> None:
        args = ["tag"]
        if force:
            args.append("-f")
        if annotated:
            args += ["-a", "-m", name]
        self.git(*args, name)

    def publish(self, prefix: str, tree: Mapping[str, FileEntry], tag: str, **kw: bool) -> str:
        """Write `tree` under `prefix`, commit, tag; returns the commit sha."""
        self.write_tree(prefix, tree)
        sha = self.commit(f"release {tag}")
        self.tag(tag, **kw)
        return sha


class BareRemote:
    """A bare repo the registry pushes to. `seed` is a work tree that fills and moves it.

    The registry never pushes to a checked-out branch, so write tests need a bare remote; the
    seed commits the way a developer would (fetch, commit on top, push).
    """

    def __init__(self, path: Path, seed_path: Path | None = None) -> None:
        self.path = path
        self._run(["git", "init", "-q", "--bare", "-b", "main", str(path)])
        self.seed = GitRepo(seed_path or path.parent / f"{path.name}-seed")
        self.seed.git("remote", "add", "origin", str(path))

    @property
    def url(self) -> str:
        return self.path.resolve().as_uri()

    @staticmethod
    def _run(cmd: list[str]) -> str:
        return subprocess.run(
            cmd, env={**os.environ, **_ENV}, check=True, capture_output=True, text=True
        ).stdout.strip()

    def git(self, *args: str) -> str:
        return self._run(["git", f"--git-dir={self.path}", *args])

    def refs(self) -> dict[str, str]:
        """Every ref of the remote (heads and tags) -> object id."""
        out = self.git("for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split(" ") for line in out.splitlines())

    def head(self, branch: str = "main") -> str | None:
        return self.refs().get(f"refs/heads/{branch}")

    def commit_files(
        self,
        files: Mapping[str, bytes],
        message: str = "seed",
        branch: str = "main",
        tag: str | None = None,
    ) -> str:
        """Commit `files` on top of the remote's `branch` (as a developer would) and push."""
        self.seed.git("fetch", "-q", "origin")
        if f"refs/heads/{branch}" in self.refs():
            self.seed.git("checkout", "-q", "-B", branch, f"origin/{branch}")
        else:
            self.seed.git("checkout", "-q", "-B", branch)
        for rel, data in files.items():
            self.seed.write(rel, data)
        commit = self.seed.commit(message)
        self.seed.git("push", "-q", "origin", f"{branch}:{branch}")
        if tag:
            self.seed.tag(tag)
            self.seed.git("push", "-q", "origin", f"refs/tags/{tag}")
        return commit

    def tree(self, ref: str) -> dict[str, tuple[str, bytes]]:
        """Every file at `ref` -> (git mode, content)."""
        out = self.git("ls-tree", "-r", "-z", ref)
        files = {}
        for line in out.split("\0"):
            if not line:
                continue
            meta, _, name = line.partition("\t")
            mode, _, oid = meta.split(" ")
            blob = subprocess.run(
                ["git", f"--git-dir={self.path}", "cat-file", "blob", oid],
                env={**os.environ, **_ENV},
                check=True,
                capture_output=True,
            ).stdout
            files[name] = (mode, blob)
        return files

    def message(self, ref: str) -> str:
        return self.git("log", "-1", "--format=%B", ref).strip()

    def author(self, ref: str) -> str:
        return self.git("log", "-1", "--format=%an <%ae>|%cn <%ce>", ref)
