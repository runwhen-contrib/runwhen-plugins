"""The read side of a git source: list tags, filter them, fetch a commit, read its tree.

Ported from the registry's `sources/git.py` (the sync, DB and credential-directory parts are gone:
the task's `sync` module drives this, and the credential comes from the connection). Files come
from git's tree object for the tagged commit (`ls-tree` + `cat-file`), never from a work tree, so
untracked and ignored files cannot leak in. Every git call goes through `GitRunner` (hardened,
non-interactive, with a timeout); an error never contains the credential.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

from plugin_tools.archive import (
    DEFAULT_LIMITS,
    E_ARCHIVE_TOO_LARGE,
    E_INVALID_PLUGIN,
    ArchiveError,
    FileEntry,
    Limits,
    is_excluded,
)
from plugin_tools.gitrun import GitError, GitRunner, GitTimeout
from plugin_tools.manifest import PLUGIN_YAML
from plugin_tools.runtimes import RuntimeInvalid, RuntimeSpec, parse_runtime_yaml

log = logging.getLogger(__name__)

# rejected[] code for a tag whose git calls failed (network, timeout); not the plugin's fault.
E_UNAVAILABLE = "E_UNAVAILABLE"
_NAME = r"[a-z][a-z0-9-]{0,62}"
# Semver 2.0 without anchors or capture groups; `parse_semver` double-checks every match.
_VERSION = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
# git's "Unable to create '.../shallow.lock': File exists." (a lock a killed process left behind).
_LOCK_EXISTS = re.compile(r"\.lock'?: File exists")
# `runtimes/{name}/v{version}` tags carry a runtime.yaml, not a plugin (D73 runtime layout).
_RUNTIME_TAG = re.compile(rf"runtimes/(?P<name>{_NAME})/v(?P<version>{_VERSION})")
RUNTIME_FILE = "runtimes/{name}/runtime.yaml"
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

_MODE_FILE = b"100644"
_MODE_EXEC = b"100755"
_MODE_SYMLINK = b"120000"
_MODE_GITLINK = b"160000"


class SourceInvalid(ValueError):
    """The `source` input is not acceptable; the message never holds a URL's userinfo."""


@dataclass(frozen=True)
class SourceSpec:
    """The task's `source` input: where plugin tags live and how they are laid out."""

    id: str
    url: str
    tag_pattern: str = "{name}/v{version}"
    path: str = "plugins/{name}"
    plugin: str | None = None

    @classmethod
    def parse(cls, raw: Mapping, *, allow_any_scheme: bool = False) -> "SourceSpec":
        """Validate the input (the registry's `SourceConfig` rules, https only for the URL).

        `allow_any_scheme` lifts the https-only rule for the unit tests, which read local
        repositories; no task input reaches it.
        """
        if not isinstance(raw, Mapping):
            raise SourceInvalid("source must be an object")
        unknown = set(raw) - {"id", "url", "tagPattern", "path", "plugin"}
        if unknown:
            raise SourceInvalid(f"source has unknown keys: {', '.join(sorted(unknown))}")
        sid, url = raw.get("id"), raw.get("url")
        if not isinstance(sid, str) or not _SOURCE_ID.fullmatch(sid):
            raise SourceInvalid(f"source.id must match {_SOURCE_ID.pattern}")
        if not isinstance(url, str) or not url:
            raise SourceInvalid("source.url is required")
        if not allow_any_scheme:
            _check_url(url)
        pattern = raw.get("tagPattern", "{name}/v{version}")
        path = raw.get("path", "plugins/{name}")
        plugin = raw.get("plugin") or None
        if not isinstance(pattern, str) or not isinstance(path, str):
            raise SourceInvalid("source.tagPattern and source.path must be strings")
        if plugin is not None and not (isinstance(plugin, str) and re.fullmatch(_NAME, plugin)):
            raise SourceInvalid(f"source.plugin must match {_NAME}")
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            raise SourceInvalid("source.path must be a relative path without '..'")
        if pattern.startswith("runtimes/"):
            raise SourceInvalid("source.tagPattern must not start with 'runtimes/'")
        if pattern.count("{version}") != 1:
            raise SourceInvalid("source.tagPattern must contain {version} exactly once")
        names = pattern.count("{name}")
        if names > 1:
            raise SourceInvalid("source.tagPattern must contain {name} at most once")
        if plugin is None:
            if names == 0:
                raise SourceInvalid("a tagPattern without {name} needs source.plugin")
            if "{name}" not in path:
                raise SourceInvalid("source.path must contain {name} (or set source.plugin)")
        else:
            if names:
                raise SourceInvalid("source.plugin is set, so tagPattern must not contain {name}")
            if "{name}" in path:
                raise SourceInvalid("source.path must not contain {name} when plugin is set")
        return cls(sid, url, pattern, path, plugin)


def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise SourceInvalid("source.url is not a URL") from None
    if parts.scheme != "https" or not parts.hostname:
        raise SourceInvalid("source.url must be an https:// URL")
    if "@" in parts.netloc:
        raise SourceInvalid(
            "source.url must not carry user:password@ (the credential comes from the connection)"
        )
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise SourceInvalid("source.url must not contain whitespace or control characters")


def tag_regex(pattern: str) -> re.Pattern[str]:
    """`{name}/v{version}` -> a regex with `name` and `version` groups; the rest is literal."""
    out = ""
    for part in re.split(r"(\{name\}|\{version\})", pattern):
        if part == "{name}":
            out += f"(?P<name>{_NAME})"
        elif part == "{version}":
            out += f"(?P<version>{_VERSION})"
        else:
            out += re.escape(part)
    return re.compile(out)


@dataclass(frozen=True)
class TagRef:
    tag: str
    name: str
    version: str
    commit: str


class GitSource:
    def __init__(self, spec: SourceSpec, runner: GitRunner) -> None:
        self.spec = spec
        self.runner = runner
        self._tag_re = tag_regex(spec.tag_pattern)

    # -- git plumbing ------------------------------------------------------------------------

    def git(self, *args: str, **kw) -> bytes:
        return self.runner.git(*args, **kw)

    @property
    def gitdir(self) -> Path:
        return self.runner.gitdir

    def clear_stale_locks(self) -> list[Path]:
        """Delete `*.lock` files left in the bare repo by a git process that was killed."""
        if not self.gitdir.is_dir():
            return []
        found = [*self.gitdir.glob("*.lock"), *(self.gitdir / "refs").glob("**/*.lock")]
        removed = []
        for lock in found:
            try:
                lock.unlink()
            except OSError:
                continue
            removed.append(lock)
        return removed

    # -- tags and trees ----------------------------------------------------------------------

    def list_tags(self) -> dict[str, str]:
        """Tag name -> commit sha for every tag on the remote (annotated tags peeled)."""
        out = self.git("ls-remote", "--tags", "--", self.spec.url, remote=True).decode(
            "utf-8", "replace"
        )
        direct: dict[str, str] = {}
        peeled: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.startswith("refs/tags/") or not _SHA.match(sha):
                continue
            name = ref.removeprefix("refs/tags/")
            if name.endswith("^{}"):
                peeled[name.removesuffix("^{}")] = sha
            else:
                direct[name] = sha
        return {tag: peeled.get(tag, sha) for tag, sha in direct.items()}

    def fetch(self, tag: str, commit: str) -> None:
        """Fetch the commit; a stale-lock failure is retried once after clearing the locks."""
        try:
            self._fetch_commit(tag, commit)
        except GitError as exc:
            if not _LOCK_EXISTS.search(str(exc)):
                raise
            log.warning("fetching %s hit a stale git lock; clearing and retrying once", commit)
            self.clear_stale_locks()
            self._fetch_commit(tag, commit)

    def _fetch_commit(self, tag: str, commit: str) -> None:
        if self._has_commit(commit):
            return
        try:
            self.git("fetch", "--depth", "1", "--no-tags", "--", self.spec.url, commit, remote=True)
        except GitTimeout:
            raise  # the tag ref would only wait as long again
        except GitError as exc:
            log.info("fetching %s by sha failed (%s); trying the tag ref", commit, exc)
            self.git(
                "fetch",
                "--depth",
                "1",
                "--no-tags",
                "--",
                self.spec.url,
                f"refs/tags/{tag}",
                remote=True,
            )

    def _has_commit(self, commit: str) -> bool:
        try:
            self.git("cat-file", "-e", f"{commit}^{{commit}}")
        except GitError:
            return False
        return True

    def object_type(self, sha: str) -> str:
        return self.git("cat-file", "-t", sha).decode("ascii", "replace").strip()

    def read_tree(
        self, commit: str, path: str, limits: Limits = DEFAULT_LIMITS
    ) -> dict[str, FileEntry]:
        """The files under `path` at `commit`, keyed relative to `path`.

        Raises ArchiveError(E_INVALID_PLUGIN) for symlinks, submodules and other non-file entries.
        """
        if not _SHA.match(commit):
            raise ValueError(f"not a full commit sha: {commit!r}")
        root = path.strip("/")
        if root in ("", "."):
            root = ""
            listing = self.git("ls-tree", "-r", "-z", commit)
        else:
            listing = self.git("ls-tree", "-r", "-z", commit, "--", root)
        prefix = f"{root}/" if root else ""

        wanted: list[tuple[str, bytes, str]] = []  # (relative path, mode, oid)
        for rec in listing.split(b"\0"):
            if not rec:
                continue
            meta, _, raw = rec.partition(b"\t")
            mode, _, oid = meta.split(b" ")
            try:
                full = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ArchiveError(
                    E_INVALID_PLUGIN, "a path in the tree is not valid UTF-8"
                ) from None
            if not full.startswith(prefix):
                continue
            rel = full[len(prefix) :]
            if is_excluded(rel):
                continue  # archive.md section 2: the four excluded path families
            if mode == _MODE_SYMLINK:
                raise ArchiveError(E_INVALID_PLUGIN, f"{rel!r} is a symlink")
            if mode == _MODE_GITLINK:
                raise ArchiveError(E_INVALID_PLUGIN, f"{rel!r} is a submodule")
            if mode not in (_MODE_FILE, _MODE_EXEC):
                raise ArchiveError(
                    E_INVALID_PLUGIN, f"{rel!r} has unsupported mode {mode.decode()}"
                )
            wanted.append((rel, mode, oid.decode("ascii")))
        if len(wanted) > limits.files:
            raise ArchiveError(
                E_ARCHIVE_TOO_LARGE, f"{len(wanted)} files exceeds the {limits.files} file limit"
            )

        blobs = self._read_blobs(sorted({oid for _, _, oid in wanted}), limits)
        return {
            rel: FileEntry(blobs[oid], executable=mode == _MODE_EXEC) for rel, mode, oid in wanted
        }

    def _read_blobs(self, oids: list[str], limits: Limits) -> dict[str, bytes]:
        """Blob contents by id from one `git cat-file --batch` process (lock-step, no deadlock)."""
        if not oids:
            return {}
        proc, timer, expired = self.runner.popen_batch()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            return self._batch(proc.stdin, proc.stdout, oids, limits)
        except GitError:
            if expired.is_set():
                raise GitTimeout(f"git cat-file timed out after {self.runner.timeout}s") from None
            raise
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()

    @staticmethod
    def _batch(
        stdin: IO[bytes], stdout: IO[bytes], oids: list[str], limits: Limits
    ) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        total = 0
        try:
            for oid in oids:
                stdin.write(oid.encode("ascii") + b"\n")
                stdin.flush()
                header = stdout.readline().split()
                if len(header) != 3 or header[0].decode("ascii", "replace") != oid:
                    raise GitError(f"git cat-file could not read object {oid}")
                if header[1] != b"blob":
                    raise GitError(f"object {oid} is a {header[1].decode()}, not a blob")
                size = int(header[2])
                if size > limits.file_bytes:
                    raise ArchiveError(
                        E_ARCHIVE_TOO_LARGE, f"a file is {size} bytes, over {limits.file_bytes}"
                    )
                total += size
                if total > limits.archive_bytes:
                    raise ArchiveError(
                        E_ARCHIVE_TOO_LARGE, f"tree is over {limits.archive_bytes} bytes"
                    )
                data = stdout.read(size)
                if len(data) != size or stdout.read(1) != b"\n":
                    raise GitError("git cat-file output was truncated")
                out[oid] = data
        except (BrokenPipeError, ValueError):
            raise GitError("git cat-file exited unexpectedly") from None
        return out

    # -- tag matching ------------------------------------------------------------------------

    def matching_tags(self, tags: Mapping[str, str]) -> list[TagRef]:
        """Tags that follow the pattern, in tag order.

        The pattern admits only semver 2.0 grammar; a version whose numeric parts are too big is
        rejected by the caller (`parse_semver`).
        """
        found = []
        for tag in sorted(tags):
            if _RUNTIME_TAG.fullmatch(tag):
                continue  # a runtime tag, handled by `matching_runtime_tags`
            m = self._tag_re.fullmatch(tag)
            if m is None:
                continue
            # A single-plugin source has no {name} in its pattern: the tag's plugin is configured.
            found.append(TagRef(tag, self.spec.plugin or m["name"], m["version"], tags[tag]))
        return found

    @staticmethod
    def matching_runtime_tags(tags: Mapping[str, str]) -> list[TagRef]:
        """`runtimes/{name}/v{version}` tags, in tag order."""
        found = []
        for tag in sorted(tags):
            m = _RUNTIME_TAG.fullmatch(tag)
            if m is not None:
                found.append(TagRef(tag, m["name"], m["version"], tags[tag]))
        return found

    def read_runtime_file(self, commit: str, name: str) -> bytes:
        """`runtimes/{name}/runtime.yaml` at `commit`; ArchiveError unless it is a plain file."""
        path = RUNTIME_FILE.replace("{name}", name)
        listing = self.git("ls-tree", "-z", commit, "--", path)
        records = [r for r in listing.split(b"\0") if r]
        if len(records) != 1:
            raise ArchiveError(E_INVALID_PLUGIN, f"{path} is missing")
        meta, _, _ = records[0].partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if kind != b"blob" or mode != _MODE_FILE:
            raise ArchiveError(
                E_INVALID_PLUGIN,
                f"{path} must be a regular file (mode 100644), not {mode.decode()}",
            )
        return self._read_blobs([oid.decode("ascii")], DEFAULT_LIMITS)[oid.decode("ascii")]

    def read_runtime(self, tag: TagRef) -> RuntimeSpec:
        """Fetch and parse one runtime tag; ArchiveError or RuntimeInvalid when it is not one."""
        self.fetch(tag.tag, tag.commit)
        if self.object_type(tag.commit) != "commit":
            raise ArchiveError(E_INVALID_PLUGIN, "tag does not point at a commit")
        return parse_runtime_yaml(
            self.read_runtime_file(tag.commit, tag.name), tag.name, tag.version
        )


__all__ = [
    "E_UNAVAILABLE",
    "PLUGIN_YAML",
    "GitSource",
    "RuntimeInvalid",
    "SourceInvalid",
    "SourceSpec",
    "TagRef",
    "tag_regex",
]
