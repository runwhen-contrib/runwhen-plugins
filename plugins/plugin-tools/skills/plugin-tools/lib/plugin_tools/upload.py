"""The `validate-upload` task: an org admin's offline upload, validated and built like a git tag.

The bytes would arrive through the relay (`GET plugin-uploads/{uploadId}`), which a script cannot
reach: THE TASK IS NOT RUNNABLE YET (scripts/validate_upload.py says so). This module takes the
bytes directly, which is how the tests drive it. Accepted: a single plugin as a tar or tar.gz (plugin.yaml at its root),
or a multi-plugin bundle in the registry's snapshot tar format (`bundle.py`). Each plugin goes
through the same `prepare` as a sync tag (validate, compile, deterministic archive), is written to an
artifact slot, and comes back in the same manifest as sync's, with `uploadId` in place of `sourceId`.
A plugin that does not validate is `rejected`; a bundle or tar that cannot be read at all is one
rejection naming the upload.
"""

import logging

from plugin_tools import TOOLS_VERSION
from plugin_tools.archive import (
    DEFAULT_LIMITS,
    E_HASH_MISMATCH,
    ArchiveError,
    Limits,
    read_archive,
)
from plugin_tools.bundle import is_bundle, read_bundle
from plugin_tools.manifest import PLUGIN_YAML, parse_plugin_yaml
from plugin_tools.prepare import E_PLUGIN_INVALID, Prepared, Rejected, prepare, reject_code
from plugin_tools.result import Manifest, rejected_item, version_item
from plugin_tools.spool import ArtifactSpool
from plugin_tools.tree_input import maybe_gunzip, read_tree_tar

log = logging.getLogger(__name__)

# What one upload may be: as sent, and once a gzip layer is inflated. A bundle of several plugin
# versions is the large case; each blob inside is still held to the 64 MiB archive limit.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_INFLATED_BYTES = 512 * 1024 * 1024
UPLOAD_LABEL = "upload"


def run_validate(
    upload_id: str,
    data: bytes,
    spool: ArtifactSpool,
    *,
    listed_at: str,
    limits: Limits = DEFAULT_LIMITS,
) -> dict:
    manifest = Manifest(tools_version=TOOLS_VERSION, listed_at=listed_at, upload_id=upload_id)
    try:
        tar = maybe_gunzip(data, MAX_INFLATED_BYTES)
    except ArchiveError as exc:
        manifest.add(
            "rejected", rejected_item(reject_code(exc.code), exc.detail, tag=None, commit=None)
        )
        return manifest.finish()
    del data

    if is_bundle(tar):
        _validate_bundle(manifest, tar, spool, limits)
    else:
        _validate_single(manifest, tar, spool, limits)
    return manifest.finish()


def _publish(manifest: Manifest, spool: ArtifactSpool, prepared: Prepared, label: str) -> bool:
    """Write and add one version. False when the run must stop (slots or manifest full)."""
    artifact = spool.name_for(prepared)
    if artifact is None:
        manifest.set_more()
        return False
    item = version_item(prepared, tag=None, commit=None, artifact=artifact)
    if not manifest.fits("versions", item):
        manifest.set_more()
        return False
    spool.put(prepared)
    manifest.add("versions", item)
    return True


def _reject(manifest: Manifest, label: str | None, code: str, message: str) -> bool:
    return manifest.add("rejected", rejected_item(code, message, tag=label, commit=None))


def _validate_single(manifest: Manifest, tar: bytes, spool: ArtifactSpool, limits: Limits) -> None:
    try:
        files = read_tree_tar(tar, limits)
    except ArchiveError as exc:
        _reject(manifest, None, reject_code(exc.code), exc.detail)
        return
    result = prepare(files, limits=limits)
    if isinstance(result, Rejected):
        _reject(manifest, _label_of(files), result.code, result.detail)
        return
    _publish(manifest, spool, result, f"{result.name}/v{result.version}")


def _label_of(files) -> str | None:
    """`<name>/v<version>` of an upload whose plugin.yaml parses, for naming a rejection."""
    entry = files.get(PLUGIN_YAML)
    if entry is None:
        return None
    try:
        m = parse_plugin_yaml(entry.data)
    except Exception:  # noqa: BLE001 - the rejection will say what is wrong with it
        return None
    return f"{m.name}/v{m.version}"


def _validate_bundle(manifest: Manifest, tar: bytes, spool: ArtifactSpool, limits: Limits) -> None:
    try:
        listed, blobs = read_bundle(tar)
    except ArchiveError as exc:
        _reject(manifest, None, reject_code(exc.code), exc.detail)
        return
    del tar
    for entry in listed:
        label = f"{entry.name}/v{entry.version}"
        blob = blobs.get(entry.sha256)
        if blob is None:
            if not _reject(
                manifest, label, E_PLUGIN_INVALID, "the bundle has no archive for this entry"
            ):
                return
            continue
        if len(blob) != entry.size:
            if not _reject(
                manifest, label, E_HASH_MISMATCH, "the archive is not the size snapshot.json lists"
            ):
                return
            continue
        try:
            files = read_archive(blob, entry.sha256, limits)
        except ArchiveError as exc:
            if not _reject(manifest, label, reject_code(exc.code), exc.detail):
                return
            continue
        result = prepare(
            files, expected_name=entry.name, expected_version=entry.version, limits=limits
        )
        if isinstance(result, Rejected):
            if not _reject(manifest, label, result.code, result.detail):
                return
            continue
        if not _publish(manifest, spool, result, label):
            return
