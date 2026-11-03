import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_k8s_types as gen  # noqa: E402

DISCOVERY_REPO = Path(os.environ.get("RW_DISCOVERY_REPO", "/nonexistent"))  # rw-discovery-codecollection checkout

MINI_CHAIN = textwrap.dedent(
    '''
    from __future__ import annotations
    from dataclasses import dataclass, field
    from typing import NamedTuple

    @dataclass(frozen=True)
    class TypeSpec:
        type: str
        plural: str
        parent: str | None
        display_name: str
        category: str
        ephemeral: bool
        name_case: str
        aliases: tuple = ()
        native: dict = field(default_factory=dict)

    CLUSTER = TypeSpec("cluster", "clusters", None, "Cluster", "cluster", False, "preserve")
    NAMESPACE = TypeSpec("namespace", "namespaces", "cluster", "Namespace", "core", False, "preserve",
                         ("ns",), {"apiGroup": "", "apiVersion": "v1", "kind": "Namespace"})

    class _Def(NamedTuple):
        type: str
        plural: str
        parent: str
        display_name: str
        category: str
        ephemeral: bool
        kind: str
        groups: tuple
        aliases: tuple = ()

    _BUILTINS = (
        _Def("pod", "pods", "namespace", "Pod", "workload", True, "Pod", (("", "v1"),)),
        _Def("node", "nodes", "cluster", "Node", "infra", False, "Node", (("", "v1"),), ("no",)),
        _Def("deployment", "deployments", "namespace", "Deployment", "workload", False, "Deployment",
             (("extensions", "v1beta1"), ("apps", "v1")), ("deploy",)),
    )
    '''
)


@pytest.fixture(scope="module")
def mini_chain(tmp_path_factory):
    path = tmp_path_factory.mktemp("chain") / "chain.py"
    path.write_text(MINI_CHAIN)
    return gen.load_chain(path)


def _real_chain_path(tmp_path):
    if not DISCOVERY_REPO.exists():
        pytest.skip("rw-discovery-codecollection checkout not available")
    try:
        return gen.fetch_chain(DISCOVERY_REPO, "origin/main", tmp_path)
    except subprocess.CalledProcessError:
        pytest.skip("origin/main:rwdiscovery/chain.py not available")


@pytest.fixture(scope="module")
def real_chain(tmp_path_factory):
    return gen.load_chain(_real_chain_path(tmp_path_factory.mktemp("real")))


def test_mini_chain_shapes(mini_chain):
    doc = gen.build_types(mini_chain)
    types = doc["types"]
    assert list(types) == ["cluster", "namespace", "pod", "node", "deployment"]
    assert types["cluster"] == {"root": True, "rootKey": "server", "plural": "clusters"}
    assert types["namespace"]["native"] == {"group": "", "kind": "Namespace"}
    assert types["namespace"]["aliases"] == ["ns"]
    assert types["pod"]["ephemeral"] is True
    assert "ephemeral" not in types["node"]
    assert types["node"]["parent"] == "cluster"
    # the canonical (last) group wins, as chain.py's own `native` does
    assert types["deployment"]["native"] == {"group": "apps", "kind": "Deployment"}
    assert doc["discovered"] == {"parentFor": {"namespaced": "namespace", "clusterScoped": "cluster"}}


def test_every_builtin_kind_has_a_type(real_chain):
    types = gen.build_types(real_chain)["types"]
    by_kind = {(t["native"]["group"], t["native"]["kind"]): n for n, t in types.items() if "native" in t}
    assert len(real_chain._BUILTINS) >= 40
    for d in real_chain._BUILTINS:
        t = types[d.type]
        assert t["parent"] == d.parent
        assert t["plural"] == d.plural
        assert set(t["native"]) == {"group", "kind"}
        assert t["native"]["kind"] == d.kind
        assert t["native"]["group"] == d.groups[-1][0]
        assert by_kind[(t["native"]["group"], d.kind)] == d.type
    assert len(types) == len(real_chain._BUILTINS) + 2


def test_real_chain_roots_and_flags(real_chain):
    doc = gen.build_types(real_chain)
    types = doc["types"]
    assert types["cluster"] == {"root": True, "rootKey": "server", "plural": "clusters"}
    assert types["namespace"]["parent"] == "cluster"
    assert types["pod"]["ephemeral"] is True
    assert "ephemeral" not in types["deployment"]
    assert doc["discovered"]["parentFor"] == {"namespaced": "namespace", "clusterScoped": "cluster"}


def test_rendered_yaml_round_trips_and_keeps_no_a_string(mini_chain):
    text = gen.render(gen.build_types(mini_chain))
    # PyYAML is YAML 1.1: a bare `no` would load as False, so it must be quoted
    assert yaml.safe_load(text) == gen.build_types(mini_chain)
    assert yaml.safe_load(text)["types"]["node"]["aliases"] == ["no"]
    assert '"no"' in text


def test_cli_prints_types_yaml(tmp_path, capsys):
    path = tmp_path / "chain.py"
    path.write_text(MINI_CHAIN)
    assert gen.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert yaml.safe_load(out)["types"]["cluster"]["root"] is True
