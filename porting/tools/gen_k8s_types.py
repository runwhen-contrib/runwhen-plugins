#!/usr/bin/env python3
"""One-shot: print integration/types.yaml for the kubernetes plugin from
rw-discovery's rwdiscovery/chain.py (`CLUSTER`, `NAMESPACE`, `_BUILTINS`).

    gen_k8s_types.py [path/to/chain.py] [--repo DIR] [--ref REF]

With no path, the blob `<ref>:rwdiscovery/chain.py` (default `origin/main`) of
`--repo` is written to a temp file and imported. Only chain.py's own data is
used; CRD types are added to types.yaml by hand afterwards.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REPO = Path("/Users/samirpatil/Documents/runwhen-auto/envs/resources-design/rw-discovery-codecollection")
DEFAULT_REF = "origin/main"
CHAIN_PATH_IN_REPO = "rwdiscovery/chain.py"

# Bare words YAML 1.1 reads as booleans/null: always quoted so `no` (Node's
# alias) stays a string under any loader.
_YAML11_WORDS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null"}
_PLAIN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


def load_chain(path: Path):
    """Import chain.py from `path` (stdlib-only module)."""
    spec = importlib.util.spec_from_file_location("rwdiscovery_chain", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def fetch_chain(repo: Path, ref: str, into: Path) -> Path:
    """Write `<ref>:rwdiscovery/chain.py` of `repo` under `into`; return the file."""
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{CHAIN_PATH_IN_REPO}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    path = Path(into) / "chain.py"
    path.write_text(blob)
    return path


def _type_def(spec_type: str, plural: str, parent: str, aliases, group: str, kind: str, ephemeral: bool) -> dict:
    entry: dict = {"parent": parent, "plural": plural}
    if aliases:
        entry["aliases"] = list(aliases)
    entry["native"] = {"group": group, "kind": kind}
    if ephemeral:
        entry["ephemeral"] = True
    return entry


def build_types(chain) -> dict:
    """The `types.yaml` document: roots, then every `_BUILTINS` row in order.
    The last (canonical) `groups` entry of a row is its native group, as in
    chain.py's own `native`."""
    types: dict = {
        chain.CLUSTER.type: {"root": True, "rootKey": "server", "plural": chain.CLUSTER.plural},
        chain.NAMESPACE.type: _type_def(
            chain.NAMESPACE.type,
            chain.NAMESPACE.plural,
            chain.NAMESPACE.parent,
            chain.NAMESPACE.aliases,
            chain.NAMESPACE.native["apiGroup"],
            chain.NAMESPACE.native["kind"],
            chain.NAMESPACE.ephemeral,
        ),
    }
    for d in chain._BUILTINS:
        types[d.type] = _type_def(d.type, d.plural, d.parent, d.aliases, d.groups[-1][0], d.kind, d.ephemeral)
    return {
        "types": types,
        "discovered": {"parentFor": {"namespaced": "namespace", "clusterScoped": "cluster"}},
    }


def _scalar(value) -> str:
    if value is True:
        return "true"
    if isinstance(value, str):
        if _PLAIN.match(value) and value.lower() not in _YAML11_WORDS:
            return value
        return json.dumps(value)
    raise TypeError(f"unsupported scalar {value!r}")


def _flow(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k}: {_flow(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return _scalar(value)


def render(doc: dict) -> str:
    """types.yaml text, one flow-style line per type (the 41a layout)."""
    lines = ["types:"]
    lines += [f"  {name}: {_flow(entry)}" for name, entry in doc["types"].items()]
    lines += ["", "discovered:", f"  parentFor: {_flow(doc['discovered']['parentFor'])}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("chain", nargs="?", type=Path, help="path to chain.py (default: git blob of --ref)")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--ref", default=DEFAULT_REF)
    args = ap.parse_args(argv)
    if args.chain is not None:
        chain = load_chain(args.chain)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            chain = load_chain(fetch_chain(args.repo, args.ref, Path(tmp)))
    sys.stdout.write(render(build_types(chain)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
