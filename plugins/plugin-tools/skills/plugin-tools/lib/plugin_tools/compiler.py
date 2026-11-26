"""Turning a plugin tree into its compiled document, with the SDK's compiler.

Ported from the registry's `compiler.py`, keeping `SdkCompiler` only: the provisional
`ManifestCompiler` was a fallback for an SDK without `runwhen_capability.plugin`, and the runtime
this runs in always ships one. The SDK compiler validates the whole tree itself (layout allowlist,
frontmatter and tasks schemas, rule budget, names), so no separate layout step runs.
"""

import importlib
import logging
from collections.abc import Mapping
from typing import Any

import runwhen_capability
from runwhen_capability.contracts import TreeEntry

from plugin_tools.archive import FileEntry
from plugin_tools.manifest import Detail, InvalidPlugin, summarize

log = logging.getLogger(__name__)


class SdkCompiler:
    name = "runwhen-capability"
    validates_layout = True

    def __init__(self) -> None:
        self.version = getattr(runwhen_capability, "__version__", "unknown")

    def compile(self, files: Mapping[str, FileEntry]) -> dict:
        sdk = importlib.import_module("runwhen_capability.plugin")
        entries = [TreeEntry(p, f.data, f.executable) for p, f in files.items()]
        try:
            diagnostics = sdk.validate_entries(entries)
            errors = [d for d in diagnostics if d.severity == "error"]
            if errors:
                raise sdk.PluginInvalid(errors)
            for d in diagnostics:
                log.warning("plugin diagnostic %s", _diagnostic_detail(d))
            return sdk.compile_entries(entries, sdk_version=self.version)
        except sdk.PluginInvalid as exc:
            details = [_diagnostic_detail(d) for d in exc.diagnostics]
            if not details:
                raise InvalidPlugin(str(exc) or "plugin rejected by the SDK compiler") from exc
            raise InvalidPlugin(summarize("plugin is invalid", details), details) from exc


def _diagnostic_detail(diagnostic: Any) -> Detail:
    file, line = diagnostic.file, getattr(diagnostic, "line", None)
    where = f"{file}:{line}" if file and line else (file or "")
    return {"path": where, "message": f"{diagnostic.code}: {diagnostic.message}"}
