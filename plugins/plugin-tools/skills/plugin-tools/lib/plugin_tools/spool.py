"""Where a run's built archives go: declared artifact outputs `archive1`..`archive20`.

A V13 script cannot reach the runner relay (the SDK's script process "never has the relay"), but
the host uploads every declared artifact output a script wrote to `$RW_OUT/<name>` once the script
exits (runwhen_capability plugin/run.py `_collect_artifacts`, plugin/lease.py `relay_uploader`).
Output names must be declared in tasks.yaml and match `^[a-z][A-Za-z0-9]*$`, so the archives use
20 fixed slots, filled in order; a slot that is not written is not uploaded. A manifest version
entry names its slot in `artifact`, and papi reads the bytes from the run's artifact of that name
and checks them against `archive.sha256`.
"""

import os
from pathlib import Path

from plugin_tools.prepare import Prepared

SLOTS = 20
CONTENT_TYPE = "application/x-tar"


def slot_name(index: int) -> str:
    return f"archive{index}"


class ArtifactSpool:
    def __init__(self, out_dir: Path | str, slots: int = SLOTS) -> None:
        if not 1 <= slots <= SLOTS:
            raise ValueError(f"slots must be between 1 and {SLOTS}")
        self.out_dir = Path(out_dir)
        self.slots = slots
        self._by_sha: dict[str, str] = {}

    @classmethod
    def from_env(cls, environ=os.environ) -> "ArtifactSpool":
        out = environ.get("RW_OUT")
        if not out:
            raise RuntimeError("RW_OUT is not set: the task has nowhere to write artifacts")
        return cls(out)

    def name_for(self, prepared: Prepared) -> str | None:
        """The slot `prepared` goes in (the same slot for the same bytes), None when all are used."""
        existing = self._by_sha.get(prepared.sha256)
        if existing is not None:
            return existing
        if len(self._by_sha) >= self.slots:
            return None
        return slot_name(len(self._by_sha) + 1)

    def put(self, prepared: Prepared) -> str:
        """Write the archive bytes to its slot; returns the slot name (an artifact output name)."""
        name = self.name_for(prepared)
        if name is None:
            raise ValueError("no archive slot is free")
        if prepared.sha256 not in self._by_sha:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / name).write_bytes(prepared.archive)
            self._by_sha[prepared.sha256] = name
        return name
