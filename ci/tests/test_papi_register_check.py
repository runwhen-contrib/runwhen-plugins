import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from papi_register_check import check_compiled, main  # noqa: E402


def compiled(**definition) -> dict:
    base = {
        "platform": "demo",
        "types": {
            "cluster": {"root": True, "rootKey": "server", "plural": "clusters"},
            "pod": {"parent": "cluster", "plural": "pods"},
            "secret": {"parent": "cluster", "plural": "secrets"},
        },
        "facets": [
            {
                "key": "podSummary",
                "title": "Pod summary",
                "appliesTo": {"types": ["pod", "demo/secret"]},
                "populator": {"kind": "projection", "expression": "spec"},
                "volatility": "config",
            }
        ],
        "rules": [
            {
                "id": "demo.pod-uses-secret",
                "edgeType": "uses_secret",
                "from": {"types": ["pod"]},
                "to": {"types": ["secret"]},
                "strategy": "reference",
                "params": {"values": "spec.secretName", "scope": "sameParent"},
            }
        ],
    }
    base.update(definition)
    return {
        "plugin": {"name": "demo", "version": "0.1.0"},
        "integration": {"definition": base, "facetSchemas": {}},
    }


def rule(**changes) -> dict:
    out = copy.deepcopy(compiled()["integration"]["definition"]["rules"][0])
    out.update(changes)
    return out


def test_valid_integration_has_no_errors():
    assert check_compiled(compiled()) == []


def test_plugin_without_integration_or_content_is_fine():
    assert check_compiled({"plugin": {"name": "x", "version": "1"}}) == []
    assert check_compiled(compiled(types={}, facets=[], rules=[])) == []


def test_reference_rule_requires_values_and_scope():
    doc = compiled(rules=[rule(params={"field": "spec.secretName"})])
    errors = check_compiled(doc)
    assert any("requires 'values'" in e for e in errors)
    assert any("requires 'scope'" in e for e in errors)
    assert any("no param 'field'" in e for e in errors)


def test_bad_scope_and_unknown_strategy():
    assert any("params.scope" in e for e in check_compiled(compiled(rules=[rule(params={"values": "a", "scope": "ns"})])))
    assert any("strategy" in e for e in check_compiled(compiled(rules=[rule(strategy="magic")])))


def test_edge_type_must_be_core_or_declared():
    bad = compiled(rules=[rule(edgeType="depends_on")])
    assert any("depends_on" in e for e in check_compiled(bad))
    declared = compiled(rules=[rule(edgeType="depends_on")], edgeTypes={"depends_on": {"title": "Depends on"}})
    assert check_compiled(declared) == []
    untitled = compiled(rules=[rule(edgeType="depends_on")], edgeTypes={"depends_on": {}})
    assert any("needs a title" in e for e in check_compiled(untitled))


def test_facet_applies_to_other_platform_is_refused():
    doc = compiled()
    doc["integration"]["definition"]["facets"][0]["appliesTo"]["types"] = ["aws/iam-role"]
    assert any("not a demo type" in e for e in check_compiled(doc))


def test_duplicates_and_bad_names():
    doc = compiled(rules=[rule(), rule()])
    assert any("declared more than once" in e for e in check_compiled(doc))
    doc = compiled()
    doc["integration"]["definition"]["types"]["Bad Name"] = {"plural": "pods"}
    errors = check_compiled(doc)
    assert any("types.Bad Name" in e for e in errors)
    assert any("also the plural" in e for e in errors)


def test_main_exit_codes(tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(compiled()))
    bad.write_text(json.dumps(compiled(rules=[rule(edgeType="depends_on")])))
    assert main(["x", str(good)]) == 0
    assert main(["x", str(good), str(bad)]) == 1
    assert "depends_on" in capsys.readouterr().out
    assert main(["x"]) == 2


# --- agreement with papi's real validator (opt-in: CI cannot reach papi) ------------------------------------

PAPI_DIR = os.environ.get("PAPI_BACKEND_DIR")

_PAPI_RUN = """
import json, sys
from papi.plugins.integration_registration import pack_from_integration
from shared.resources.identity import TypeRegistry
from shared.resources.packs import PackIn, PackValidationError, validate_pack
body = pack_from_integration(json.load(sys.stdin))
if body is not None:
    validate_pack(PackIn.model_validate(body), existing_types=TypeRegistry([]))
"""


def papi_accepts(doc: dict) -> bool:
    assert PAPI_DIR
    run = subprocess.run(
        [str(Path(PAPI_DIR) / ".venv/bin/python"), "-c", _PAPI_RUN],
        input=json.dumps(doc),
        text=True,
        capture_output=True,
        cwd=PAPI_DIR,
        env={**os.environ, "PYTHONPATH": PAPI_DIR, "REDIS_DB": os.environ.get("REDIS_DB", "0")},
    )
    return run.returncode == 0


def _mutations() -> dict[str, dict]:
    cases = {"valid": compiled()}
    cases["reference-field-param"] = compiled(rules=[rule(params={"field": "spec.secretName"})])
    cases["reference-no-scope"] = compiled(rules=[rule(params={"values": "spec.secretName"})])
    cases["bad-scope"] = compiled(rules=[rule(params={"values": "spec.a", "scope": "ns"})])
    cases["unknown-strategy"] = compiled(rules=[rule(strategy="magic")])
    cases["non-core-edge"] = compiled(rules=[rule(edgeType="depends_on")])
    cases["declared-edge"] = compiled(rules=[rule(edgeType="depends_on")], edgeTypes={"depends_on": {"title": "Depends on"}})
    cases["undeclared-title"] = compiled(rules=[rule(edgeType="depends_on")], edgeTypes={"depends_on": {}})
    cases["duplicate-rule"] = compiled(rules=[rule(), rule()])
    cases["owner-reference"] = compiled(rules=[rule(strategy="owner_reference", params={})])
    cases["owner-reference-params"] = compiled(rules=[rule(strategy="owner_reference")])
    return cases


@pytest.mark.skipif(not PAPI_DIR, reason="PAPI_BACKEND_DIR (papi's backend-services-v2, with a .venv) is unset")
@pytest.mark.parametrize("name", sorted(_mutations()))
def test_agrees_with_papi_validator(name):
    doc = _mutations()[name]
    assert (check_compiled(doc) == []) == papi_accepts(doc), name
