"""The `sync` task: list a git source's tags, build what is new, write each archive to an
artifact slot, return the manifest.

    1. `git ls-remote --tags`
    2. filter by `tagPattern`, minus `known`, newest first, capped at `maxVersions`
    3. per tag: fetch --depth 1, read the tree, normalize, parse plugin.yaml, validate + compile
       (the SDK), check the tag against plugin.yaml's name and version
    4. build the deterministic ustar and take its sha256
    5. write the archive to an artifact slot (`archive1`..`archive20`, spool.py); the host uploads it
    6. return the manifest (1 MiB cap: `more` instead of a larger result)

A tag the plugin author got wrong is a `rejected` entry and the run goes on. What the run cannot
do at all (list the tags) fails the task with a structured error: the platform then has
no manifest to register and the next run retries.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runwhen_capability.rwerrors import RwError

from plugin_tools import TOOLS_VERSION
from plugin_tools.archive import ArchiveError, Limits
from plugin_tools.gitrun import GitError, GitTimeout
from plugin_tools.gitsource import (
    E_UNAVAILABLE,
    GitSource,
    SourceInvalid,
    SourceSpec,
    TagRef,
)
from plugin_tools.prepare import E_PLUGIN_INVALID, Rejected, prepare, reject_code
from plugin_tools.result import Manifest, rejected_item, runtime_item, version_item
from plugin_tools.runtimes import RuntimeInvalid
from plugin_tools.semver import parse_semver, version_key
from plugin_tools.spool import SLOTS, ArtifactSpool

log = logging.getLogger(__name__)

DEFAULT_MAX_VERSIONS = 20
MAX_MAX_VERSIONS = SLOTS  # one artifact slot per version; a larger request is clamped
MAX_KNOWN = 100_000
_SHA_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class InputInvalid(ValueError):
    """An input of the task is not acceptable (`invalid_input`)."""


def parse_limits(raw: Mapping | None) -> Limits:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise InputInvalid("limits must be an object")
    unknown = set(raw) - {"archiveBytes", "files", "fileBytes"}
    if unknown:
        raise InputInvalid(f"limits has unknown keys: {', '.join(sorted(unknown))}")
    values = {}
    for key, field in (
        ("archiveBytes", "archive_bytes"),
        ("files", "files"),
        ("fileBytes", "file_bytes"),
    ):
        if key in raw:
            v = raw[key]
            if not isinstance(v, int) or isinstance(v, bool):
                raise InputInvalid(f"limits.{key} must be an integer")
            values[field] = v
    try:
        return Limits(**values)
    except ValueError as exc:
        raise InputInvalid(str(exc)) from None


def parse_known(raw: Sequence | None) -> set[tuple[str, str]]:
    """`known[]` as the (tag, commit) pairs to skip. A tag at a different commit is not skipped."""
    if raw is None:
        raw = []
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise InputInvalid("known must be a list")
    if len(raw) > MAX_KNOWN:
        raise InputInvalid(f"known has more than {MAX_KNOWN} entries")
    out: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise InputInvalid("known entries must be objects")
        tag, commit, sha = item.get("tag"), item.get("commit"), item.get("sha256")
        if not isinstance(tag, str) or not tag:
            raise InputInvalid("known[].tag must be a string")
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise InputInvalid("known[].commit must be 40 lowercase hex characters")
        if sha is not None and not (isinstance(sha, str) and _SHA_REF.fullmatch(sha)):
            raise InputInvalid("known[].sha256 must be sha256:<64 hex>")
        out.add((tag, commit))
    return out


def parse_max_versions(raw: Any) -> int:
    if raw is None:
        return DEFAULT_MAX_VERSIONS
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise InputInvalid("maxVersions must be an integer of at least 1")
    return min(raw, MAX_MAX_VERSIONS)


_AUTH_HINTS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "http basic: access denied",
    "returned error: 401",
    "returned error: 403",
    "permission denied",
)
_NOT_FOUND_HINTS = ("repository not found", "not found", "returned error: 404")
_UNREACHABLE_HINTS = (
    "could not resolve host",
    "failed to connect",
    "connection refused",
    "connection timed out",
    "unable to access",
    "ssl",
    "tls",
)


def classify_git_error(exc: GitError) -> str:
    """The task error code for a git failure (it never holds a credential)."""
    if isinstance(exc, GitTimeout):
        return "timeout"
    text = str(exc).lower()
    if any(h in text for h in _AUTH_HINTS):
        return "permission_denied"
    if any(h in text for h in _NOT_FOUND_HINTS):
        return "not_found"
    if any(h in text for h in _UNREACHABLE_HINTS):
        return "unreachable"
    return "dependency"


def newest_first(tags: Sequence[TagRef]) -> list[TagRef]:
    """Highest semver first; ties (the same version under two names) by name, then tag.

    A tag whose version `parse_semver` refuses (a numeric part too big) goes last: it is rejected
    when its turn comes, and must not break the ordering of the others.
    """
    valid, invalid = [], []
    for t in sorted(tags, key=lambda t: (t.name, t.tag)):
        try:
            parse_semver(t.version)
        except ValueError:
            invalid.append(t)
        else:
            valid.append(t)
    return sorted(valid, key=lambda t: version_key(t.version), reverse=True) + invalid


@dataclass(frozen=True)
class SyncRequest:
    source: SourceSpec
    known: set[tuple[str, str]]
    max_versions: int
    limits: Limits

    @classmethod
    def parse(
        cls,
        source: Any,
        known: Any = None,
        max_versions: Any = None,
        limits: Any = None,
        *,
        allow_any_scheme: bool = False,
    ) -> "SyncRequest":
        try:
            spec = SourceSpec.parse(source, allow_any_scheme=allow_any_scheme)
        except SourceInvalid as exc:
            raise InputInvalid(str(exc)) from None
        return cls(spec, parse_known(known), parse_max_versions(max_versions), parse_limits(limits))


def run_sync(
    request: SyncRequest,
    git: GitSource,
    spool: ArtifactSpool,
    *,
    listed_at: str,
) -> dict:
    """Steps 1-6. `git` is a `GitSource` on a hardened runner; `spool` takes the built archives."""
    spec = request.source
    manifest = Manifest(tools_version=TOOLS_VERSION, listed_at=listed_at, source_id=spec.id)

    # 1. ls-remote
    try:
        git.clear_stale_locks()
        listed = git.list_tags()
    except GitError as exc:
        raise RwError(classify_git_error(exc), str(exc), scope=spec.id) from None

    # 2. filter: pattern, minus known, newest first, capped
    plugin_tags = [t for t in git.matching_tags(listed) if (t.tag, t.commit) not in request.known]
    runtime_tags = [
        t for t in git.matching_runtime_tags(listed) if (t.tag, t.commit) not in request.known
    ]
    candidates = newest_first(plugin_tags)

    # runtimes: a handful of tiny files, not counted against maxVersions
    for tag in runtime_tags:
        if not _sync_runtime(git, manifest, tag):
            return manifest.finish()

    # 3-5. per plugin tag. maxVersions counts versions BUILT: a tag that is rejected again on every
    # run must not use up the slots and starve the good tags behind it.
    built = 0
    for tag in candidates:
        if built >= request.max_versions:
            manifest.set_more()  # the rest is for the next run
            break
        outcome = _sync_tag(git, spool, manifest, request, tag)
        if outcome == "stop":
            manifest.set_more()  # this tag, and any after it, are for the next run
            break
        built += outcome == "built"
    return manifest.finish()


def _sync_runtime(git: GitSource, manifest: Manifest, tag: TagRef) -> bool:
    """False when the manifest is full."""
    try:
        parse_semver(tag.version)
        spec = git.read_runtime(tag)
    except ValueError as exc:
        return _reject(manifest, tag, E_PLUGIN_INVALID, str(exc))
    except ArchiveError as exc:
        return _reject(manifest, tag, reject_code(exc.code), exc.detail)
    except RuntimeInvalid as exc:
        return _reject(manifest, tag, E_PLUGIN_INVALID, exc.detail)
    except GitError as exc:
        return _reject(manifest, tag, E_UNAVAILABLE, str(exc))
    return manifest.add("runtimes", runtime_item(spec))


def _reject(manifest: Manifest, tag: TagRef, code: str, message: str) -> bool:
    log.info("rejected tag %s: %s: %s", tag.tag, code, message)
    return manifest.add("rejected", rejected_item(code, message, tag=tag.tag, commit=tag.commit))


def _sync_tag(git, spool, manifest: Manifest, request: SyncRequest, tag: TagRef) -> str:
    """ "built", "rejected", or "stop" (the manifest is full or the artifact slots are)."""
    limits = request.limits
    try:
        parse_semver(tag.version)
    except ValueError as exc:
        return "rejected" if _reject(manifest, tag, E_PLUGIN_INVALID, str(exc)) else "stop"
    path = request.source.path.replace("{name}", tag.name)
    try:
        git.fetch(tag.tag, tag.commit)
        if git.object_type(tag.commit) != "commit":
            return (
                "rejected"
                if _reject(manifest, tag, E_PLUGIN_INVALID, "tag does not point at a commit")
                else "stop"
            )
        files = git.read_tree(tag.commit, path, limits)
    except ArchiveError as exc:
        return "rejected" if _reject(manifest, tag, reject_code(exc.code), exc.detail) else "stop"
    except GitError as exc:
        return "rejected" if _reject(manifest, tag, E_UNAVAILABLE, str(exc)) else "stop"

    result = prepare(files, expected_name=tag.name, expected_version=tag.version, limits=limits)
    if isinstance(result, Rejected):
        return "rejected" if _reject(manifest, tag, result.code, result.detail) else "stop"

    artifact = spool.name_for(result)
    if artifact is None:
        return "stop"  # all slots are used
    item = version_item(result, tag=tag.tag, commit=tag.commit, artifact=artifact)
    # The cap is checked before the write: an archive nobody can register is not worth keeping.
    if not manifest.fits("versions", item):
        return "stop"
    spool.put(result)
    manifest.add("versions", item)
    return "built"
