"""Validate, compile and archive one plugin tree: steps 3 and 4 of the sync task (`sync.py`).

Ported from the registry's `Ingestor.prepare` (`ingest.py`), without the database, the blob store
and the ownership and version-conflict checks: those are papi's now. A bad plugin comes back as
`Rejected`; only unexpected failures raise.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from runwhen_capability.contracts import validate

from plugin_tools.archive import (
    DEFAULT_LIMITS,
    E_ARCHIVE_TOO_LARGE,
    E_HASH_MISMATCH,
    E_INVALID_PLUGIN,
    ArchiveError,
    FileEntry,
    Limits,
    build_archive,
    normalize_tree,
    sha256_hex,
)
from plugin_tools.compiler import SdkCompiler
from plugin_tools.manifest import (
    PLUGIN_YAML,
    Detail,
    InvalidPlugin,
    PluginManifest,
    parse_plugin_yaml,
    summarize,
    violation_details,
)
from plugin_tools.semver import parse_semver

log = logging.getLogger(__name__)

# The codes a rejection carries in the sync manifest. The archive layer still speaks
# its own E_INVALID_PLUGIN (archive.md, the registry's tests); `reject_code` maps it.
E_PLUGIN_INVALID = "E_PLUGIN_INVALID"

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def reject_code(code: str) -> str:
    """The manifest's code for an archive-layer or manifest-layer code."""
    return E_PLUGIN_INVALID if code == E_INVALID_PLUGIN else code


@dataclass(frozen=True)
class Rejected:
    code: str
    detail: str
    details: list[Detail] = field(default_factory=list)


@dataclass(frozen=True)
class Prepared:
    """A validated, compiled and archived plugin version."""

    name: str
    version: str
    manifest: PluginManifest
    compiled: dict
    archive: bytes
    sha256: str  # bare 64 lowercase hex
    size: int
    files: int
    requires: dict[str, Any]
    runtimes: list[str]
    sdk_version: str

    @property
    def sha256_ref(self) -> str:
        return f"sha256:{self.sha256}"


def _check_identity(
    manifest: PluginManifest, expected_name: str | None, expected_version: str | None
) -> None:
    if not _NAME.fullmatch(manifest.name):
        raise InvalidPlugin(f"plugin name {manifest.name!r} does not match {_NAME.pattern}")
    try:
        parse_semver(manifest.version)
    except ValueError as exc:
        raise InvalidPlugin(f"plugin.yaml version: {exc}") from None
    if expected_name is not None and expected_name != manifest.name:
        raise InvalidPlugin(
            f"plugin.yaml name {manifest.name!r} does not match the expected plugin "
            f"{expected_name!r}"
        )
    if expected_version is not None and expected_version != manifest.version:
        raise InvalidPlugin(
            f"plugin.yaml version {manifest.version!r} does not match the expected "
            f"version {expected_version!r}"
        )


def _check_compiled(doc: dict) -> None:
    """Hold the compiler's output to the compiled-plugin schema."""
    violations = validate("compiled-plugin", doc)
    if violations:
        details = violation_details("compiled-plugin", violations)
        raise InvalidPlugin(summarize("compiled plugin is invalid", details), details)


def _requires(flat: Mapping[str, str]) -> dict[str, Any]:
    """plugin.yaml's flat `requires` as the API's Requires: `platform`, the rest under `plugins`."""
    out: dict[str, Any] = {}
    if "platform" in flat:
        out["platform"] = flat["platform"]
    plugins = {name: rng for name, rng in flat.items() if name != "platform"}
    if plugins:
        out["plugins"] = plugins
    return out


def _runtimes(doc: dict) -> list[str]:
    """The runtimes the tasks declare. A task with none compiles with none (SDK 0.6.0: papi applies
    the workspace's default runtime), so there is no `generic@1` placeholder to list."""
    return sorted({t["runtime"] for t in doc.get("tasks") or [] if t.get("runtime")})


def prepare(
    files: Mapping[str, FileEntry],
    *,
    expected_name: str | None = None,
    expected_version: str | None = None,
    compiler: SdkCompiler | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> Prepared | Rejected:
    """Normalise, validate, compile and archive one plugin tree.

    `expected_name` and `expected_version` are what the tag (or an upload's index) says the plugin
    is; plugin.yaml must agree (a mismatch is rejected).
    """
    compiler = compiler or SdkCompiler()
    try:
        tree = normalize_tree(files, limits)
        if PLUGIN_YAML not in tree:
            raise InvalidPlugin("plugin.yaml is missing from the plugin root")
        manifest = parse_plugin_yaml(tree[PLUGIN_YAML].data)
        _check_identity(manifest, expected_name, expected_version)
        try:
            doc = compiler.compile(tree)
        except InvalidPlugin:
            raise
        except Exception as exc:
            log.info("compiling %s@%s failed: %r", manifest.name, manifest.version, exc)
            message = f"{type(exc).__name__}: {exc}"
            raise InvalidPlugin(
                f"compile failed: {message}", [{"path": "", "message": message}]
            ) from exc
        _check_compiled(doc)
        archive = build_archive(tree, limits)
        return Prepared(
            name=manifest.name,
            version=manifest.version,
            manifest=manifest,
            compiled=doc,
            archive=archive,
            sha256=sha256_hex(archive),
            size=len(archive),
            files=len(tree),
            requires=_requires(manifest.requires),
            runtimes=_runtimes(doc),
            sdk_version=compiler.version,
        )
    except ArchiveError as exc:
        return Rejected(reject_code(exc.code), exc.detail)
    except InvalidPlugin as exc:
        return Rejected(E_PLUGIN_INVALID, exc.detail, exc.details)
    except RecursionError:
        return Rejected(E_PLUGIN_INVALID, "a plugin file is nested too deeply to process")


__all__ = [
    "E_ARCHIVE_TOO_LARGE",
    "E_HASH_MISMATCH",
    "E_PLUGIN_INVALID",
    "Prepared",
    "Rejected",
    "prepare",
    "reject_code",
]
