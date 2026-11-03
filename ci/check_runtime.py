"""Check a runtime image descriptor (`runtimes/<name>/runtime.yaml`) the way the plugin registry does.

The rules mirror plugin-registry's `RuntimeSpec` (runtimes.py), without importing it:
name, semver version, `image` a repository (no scheme, tag or digest), `digests` keys in
{index, linux/amd64, linux/arm64, linux/arm/v7} with `sha256:<64 lowercase hex>` values (the
all-zero placeholder is refused), `tools` a str -> str map, optional `description`, no other keys.

    check_runtime.py validate [--allow-placeholder] [--runtime-dir DIR]
    check_runtime.py tag <runtimes/NAME/vX.Y.Z> [--repo-root DIR]
    check_runtime.py compare [--runtime-dir DIR]        (needs docker buildx and registry access)
    check_runtime.py digests <image-ref>                (prints a digests: block for runtime.yaml)

`validate --allow-placeholder` is for branches and PRs, where runtime.yaml still carries the
placeholder until the "pin digests" PR; `tag` and `compare` are strict. `compare` looks up the
image by the digest runtime.yaml pins and fails unless the registry's index lists exactly the
platform digests runtime.yaml lists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

MAX_RUNTIME_YAML_BYTES = 64 * 1024
DIGEST_KEYS = frozenset({"index", "linux/amd64", "linux/arm64", "linux/arm/v7"})
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64
ALLOWED_KEYS = frozenset({"name", "version", "image", "digests", "tools", "description"})

TAG_RE = re.compile(r"^runtimes/(?P<name>[a-z][a-z0-9-]{0,62})/v(?P<version>.+)$")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Official semver 2.0 grammar (the registry's own).
_SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?"
)
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_HOST = r"[a-zA-Z0-9]+(?:[-.][a-zA-Z0-9]+)*(?::[0-9]+)?"
_IMAGE = re.compile(rf"(?:{_HOST}/)?{_COMPONENT}(?:/{_COMPONENT})*")
MAX_SEMVER_PART = 2_147_483_647


def parse_tag(tag: str) -> tuple[str, str]:
    """Split `runtimes/<name>/v<semver>` into (name, version); ValueError when it is not one."""
    m = TAG_RE.match(tag)
    if not m or not _SEMVER.fullmatch(m["version"]):
        raise ValueError(f"tag {tag!r} does not match runtimes/<name>/vX.Y.Z[-prerelease]")
    return m["name"], m["version"]


def load_runtime(path: Path, *, allow_placeholder: bool = False) -> dict:
    """Load and validate one runtime.yaml; ValueError says why (without echoing digests)."""
    if not path.is_file():
        raise ValueError(f"{path} is missing or not a regular file")
    data = path.read_bytes()
    if len(data) > MAX_RUNTIME_YAML_BYTES:
        raise ValueError(f"{path} is over {MAX_RUNTIME_YAML_BYTES} bytes")
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as e:
        raise ValueError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{path} must be a mapping")
    problems = _problems(doc, allow_placeholder)
    if problems:
        raise ValueError(f"{path} is invalid: " + "; ".join(problems))
    return doc


def _problems(doc: dict, allow_placeholder: bool) -> list[str]:
    out: list[str] = []
    for extra in sorted(set(map(str, doc)) - ALLOWED_KEYS):
        out.append(f"{extra}: extra key not permitted")
    for key in ("name", "version", "image", "digests", "tools"):
        if key not in doc:
            out.append(f"{key}: required")

    def is_str(value: object) -> bool:
        return isinstance(value, str)

    name = doc.get("name")
    if "name" in doc and not (is_str(name) and _NAME.fullmatch(name)):
        out.append("name: must match [a-z][a-z0-9-]{0,62}")
    version = doc.get("version")
    if "version" in doc:
        # A bare `1.0` or `1` loads as a number in YAML: quote it.
        m = _SEMVER.fullmatch(version) if is_str(version) else None
        if not m:
            out.append("version: must be a semver 2.0 string")
        elif max(int(m[1]), int(m[2]), int(m[3])) > MAX_SEMVER_PART:
            out.append(f"version: a numeric part is over {MAX_SEMVER_PART}")
    image = doc.get("image")
    if "image" in doc and not (is_str(image) and _IMAGE.fullmatch(image)):
        out.append("image: must be a repository reference without scheme, tag or digest")
    if "description" in doc and doc["description"] is not None and not is_str(doc["description"]):
        out.append("description: must be a string")
    tools = doc.get("tools")
    if "tools" in doc:
        if not isinstance(tools, dict) or not all(
            is_str(k) and is_str(v) for k, v in tools.items()
        ):
            out.append("tools: must map strings to strings (quote versions)")
    digests = doc.get("digests")
    if "digests" in doc:
        if not isinstance(digests, dict) or not digests:
            out.append("digests: must be a non-empty mapping")
        else:
            for key, value in digests.items():
                if key not in DIGEST_KEYS:
                    out.append(f"digests key must be one of {sorted(DIGEST_KEYS)}")
                elif not (is_str(value) and _DIGEST.fullmatch(value)):
                    out.append(f"digests.{key}: must be sha256:<64 lowercase hex>")
                elif value == PLACEHOLDER_DIGEST and not allow_placeholder:
                    out.append(f"digests.{key}: is the all-zero placeholder; pin a real digest")
    return out


def check_tag(tag: str, runtime_dir: Path) -> dict:
    """The tag must name this runtime and version, and runtime.yaml must be registry-valid."""
    name, version = parse_tag(tag)
    if runtime_dir.name != name:
        raise ValueError(f"tag {tag!r} names runtime {name!r} but the directory is {runtime_dir}")
    doc = load_runtime(runtime_dir / "runtime.yaml")
    if doc["name"] != name:
        raise ValueError(f"tag {tag!r} has name {name} but runtime.yaml declares {doc['name']}")
    if doc["version"] != version:
        raise ValueError(
            f"tag {tag!r} has version {version} but runtime.yaml declares {doc['version']}"
        )
    return doc


def digests_from_index(raw: bytes) -> dict[str, str]:
    """The registry-format digests of an OCI index / Docker manifest list, from its raw bytes.

    `index` is the sha256 of the bytes themselves; each platform manifest contributes its own
    digest under `linux/<arch>[/<variant>]`. Attestation manifests (platform unknown/unknown)
    and platforms the registry has no key for are skipped.
    """
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ValueError("the registry did not return a JSON manifest") from None
    manifests = doc.get("manifests") if isinstance(doc, dict) else None
    if not isinstance(manifests, list):
        raise ValueError("the image is a single manifest, not a multi-arch index")
    out = {"index": "sha256:" + hashlib.sha256(raw).hexdigest()}
    for m in manifests:
        platform = m.get("platform") or {}
        if platform.get("os") != "linux":
            continue
        key = f"linux/{platform.get('architecture')}"
        if platform.get("variant"):
            key += f"/{platform['variant']}"
        if key in DIGEST_KEYS:
            out[key] = m["digest"]
    return out


def compare_digests(pinned: dict[str, str], actual: dict[str, str]) -> None:
    """ValueError when runtime.yaml's digests are not exactly the pushed image's."""
    if pinned == actual:
        return
    lines = []
    for key in sorted(set(pinned) | set(actual)):
        if pinned.get(key) != actual.get(key):
            lines.append(f"  {key}: runtime.yaml={pinned.get(key)} registry={actual.get(key)}")
    raise ValueError("runtime.yaml digests differ from the pushed image:\n" + "\n".join(lines))


def inspect_raw(ref: str) -> bytes:
    """The raw manifest bytes of `ref` (a tag or `repo@sha256:...`), via docker buildx."""
    res = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        raise ValueError(f"cannot inspect {ref}: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def check_compare(runtime_dir: Path, inspect=inspect_raw) -> dict[str, str]:
    """Fetch the image runtime.yaml pins by its index digest and compare every digest."""
    doc = load_runtime(runtime_dir / "runtime.yaml")
    pinned = doc["digests"]
    if "index" not in pinned:
        raise ValueError("runtime.yaml pins no index digest; cannot look the image up")
    raw = inspect(f"{doc['image']}@{pinned['index']}")
    actual = digests_from_index(raw)
    compare_digests(pinned, actual)
    return actual


def format_digests(digests: dict[str, str]) -> str:
    return "digests:\n" + "".join(f'  {k}: "{digests[k]}"\n' for k in sorted(digests))


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_dir(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--runtime-dir", type=Path, default=root / "runtimes" / "kubernetes")

    v = sub.add_parser("validate")
    add_dir(v)
    v.add_argument("--allow-placeholder", action="store_true")
    t = sub.add_parser("tag")
    t.add_argument("tag")
    t.add_argument("--repo-root", type=Path, default=root)
    c = sub.add_parser("compare")
    add_dir(c)
    d = sub.add_parser("digests")
    d.add_argument("image")
    args = p.parse_args(argv[1:])
    try:
        if args.cmd == "validate":
            load_runtime(
                args.runtime_dir / "runtime.yaml", allow_placeholder=args.allow_placeholder
            )
        elif args.cmd == "tag":
            name, _ = parse_tag(args.tag)
            check_tag(args.tag, args.repo_root / "runtimes" / name)
        elif args.cmd == "compare":
            check_compare(args.runtime_dir)
        else:
            print(format_digests(digests_from_index(inspect_raw(args.image))), end="")
            return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
