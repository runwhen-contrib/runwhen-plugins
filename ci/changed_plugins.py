"""Select the plugins affected by a change set.

`python3 ci/changed_plugins.py` reads `git diff --name-only $BASE...HEAD` and
prints one plugin per line. With no usable BASE (unset, or the all-zero SHA a
first push reports) every plugin is selected.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Changes to these paths can affect every plugin's CI result.
ALL_TRIGGERS = ("ci/changed_plugins.py", "ci/papi_register_check.py", ".github/workflows/plugins.yml")


def changed_plugins(paths: list[str], all_plugins: list[str]) -> list[str]:
    if any(p in ALL_TRIGGERS for p in paths):
        return sorted(all_plugins)
    known = set(all_plugins)
    hit: set[str] = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "plugins" and parts[1] in known:
            hit.add(parts[1])
    return sorted(hit)


def _discover_plugins(root: Path) -> list[str]:
    return sorted(p.parent.name for p in (root / "plugins").glob("*/plugin.yaml"))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    all_plugins = _discover_plugins(root)
    base = os.environ.get("BASE", "")
    if not base or set(base) == {"0"}:
        selected = sorted(all_plugins)
    else:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        selected = changed_plugins([line for line in out.splitlines() if line], all_plugins)
    for name in selected:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
