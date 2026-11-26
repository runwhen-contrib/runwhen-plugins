"""Semver 2.0 parsing and precedence (ported from the registry's repository.py)."""

import re
from functools import cmp_to_key

# Official semver 2.0 grammar.
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?"
)
# The semver columns are int4 on Postgres: no part may exceed it.
MAX_SEMVER_PART = 2_147_483_647


def parse_semver(version: str) -> tuple[int, int, int, str | None]:
    """Split a semver 2.0 string; ValueError when it is malformed or a numeric part is too big."""
    m = _SEMVER.fullmatch(version)
    if not m:
        raise ValueError(f"not a semver 2.0 version: {version!r}")
    parts = int(m[1]), int(m[2]), int(m[3])
    if max(parts) > MAX_SEMVER_PART:
        raise ValueError(f"a numeric part of {version!r} is over {MAX_SEMVER_PART}")
    return (*parts, m[4])


def _cmp_prerelease(a: str | None, b: str | None) -> int:
    if a == b:
        return 0
    if a is None:  # a release outranks any prerelease of the same triple
        return 1
    if b is None:
        return -1
    ai, bi = a.split("."), b.split(".")
    for x, y in zip(ai, bi, strict=False):
        if x == y:
            continue
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            return -1 if int(x) < int(y) else 1
        if xn != yn:  # numeric identifiers rank below alphanumeric ones
            return -1 if xn else 1
        return -1 if x < y else 1
    return (len(ai) > len(bi)) - (len(ai) < len(bi))


def compare_versions(a: str, b: str) -> int:
    """-1, 0 or 1 by semver precedence (build metadata is ignored, as semver says)."""
    pa, pb = parse_semver(a), parse_semver(b)
    if pa[:3] != pb[:3]:
        return -1 if pa[:3] < pb[:3] else 1
    return _cmp_prerelease(pa[3], pb[3])


version_key = cmp_to_key(compare_versions)
