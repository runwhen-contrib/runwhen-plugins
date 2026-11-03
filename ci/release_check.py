"""Validate a per-plugin release tag `<plugin>/vX.Y.Z[-rc.N]` (CAP-13).

`python3 ci/release_check.py <tag>` exits non-zero on a bad tag and prints the
plugin name on success.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

TAG_RE = re.compile(r"^(?P<p>[a-z][a-z0-9-]*)/v(?P<v>\d+\.\d+\.\d+(?:-rc\.\d+)?)$")


def parse_tag(tag: str) -> tuple[str, str]:
    m = TAG_RE.match(tag)
    if not m:
        raise ValueError(f"tag {tag!r} does not match <plugin>/vX.Y.Z[-rc.N]")
    return m["p"], m["v"]


def check_tag(tag: str, repo_root: Path) -> None:
    plugin, version = parse_tag(tag)
    manifest = repo_root / "plugins" / plugin / "plugin.yaml"
    if not manifest.is_file():
        raise ValueError(f"tag {tag!r}: no such plugin ({manifest} missing)")
    declared = (yaml.safe_load(manifest.read_text()) or {}).get("version")
    if str(declared) != version:
        raise ValueError(f"tag {tag!r} has version {version} but {manifest} declares {declared}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_check.py <tag>", file=sys.stderr)
        return 2
    try:
        check_tag(argv[1], Path(__file__).resolve().parents[1])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(parse_tag(argv[1])[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
