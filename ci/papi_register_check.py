"""Check that a compiled plugin's integration registers through papi's pack validator.

papi (V13 lane B) registers a plugin's integration at install time: `pack_from_integration(compiled)` turns
it into a resource pack and `validate_pack` checks it. CI cannot reach papi, so this re-implements the rules
that registration enforces, without importing it:

1. A dependency rule's `strategy` is one papi knows, and its `params` hold every required name and no
   unknown one (`reference` requires `values` and `scope`; `scope` is sameParent, sameRoot or any).
2. A rule's `edgeType` is a core edge type or is declared in the integration's `edgeTypes` (each with a title).
3. A facet's `appliesTo` types belong to the integration's own platform (unqualified, or `<platform>/<type>`).
4. The document shape papi reads: `integration.definition.platform`, with `types` and `rules` where papi finds
   them (`rootKey` is read from the compiled doc, never from the pack).

Also the cheap structural checks the same validator makes: type and plural names match papi's grammar and are
unique, rule ids and facet keys are unique, and facet keys match papi's facet-key grammar.

Drift: the tables below are copies of papi's (`shared/resources/packs.py`). ci/tests/test_papi_register_check.py
compares this checker with papi's real validator when PAPI_BACKEND_DIR points at a papi checkout
(`backend-services-v2`, with a venv); run it when lane B changes the pack format.

    papi_register_check.py <compiled.json> [<compiled.json> ...]      (rwtask compile PLUGIN --out DIR)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

CORE_EDGE_TYPES = frozenset(
    {
        "owns",
        "routes_to",
        "uses_config",
        "uses_secret",
        "mounts",
        "runs_as",
        "bound_to",
        "provisioned_by",
        "scaled_by",
        "protected_by",
        "governed_by",
        "authorized_by",
        "grants",
        "managed_by",
        "calls",
        "assumes_identity",
        "uses",
        "hosted_by",
        "exposed_by",
    }
)

SCOPES = frozenset({"sameParent", "sameRoot", "any"})

# strategy -> {param: required}
STRATEGY_PARAMS: dict[str, dict[str, bool]] = {
    "reference": {"values": True, "scope": True, "namePattern": False, "typeFrom": False},
    "owner_reference": {},
    "label_selector": {"selector": True, "targetLabels": False, "scope": True, "emptySelector": False},
    "field_join": {"fromValue": True, "toValue": True, "scope": True},
    "path_template": {"values": True, "match": True, "template": True},
    "dns_reference": {"sources": False, "confidence": False, "hostKeys": False},
}

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_FACET_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


def _split(ref: str) -> tuple[str | None, str]:
    """(platform qualifier or None, type name) of a type ref."""
    head, sep, tail = ref.partition("/")
    return (head, tail) if sep else (None, ref)


def check_compiled(compiled: dict[str, Any]) -> list[str]:
    """Violations of papi's integration registration; empty when the integration registers (or has none)."""
    integration = compiled.get("integration")
    if not integration:
        return []
    definition = integration.get("definition") or {}
    platform = definition.get("platform")
    types = definition.get("types") or {}
    facets = definition.get("facets") or []
    rules = definition.get("rules") or []
    if not (types or facets or rules):
        return []
    errors: list[str] = []
    if not platform:
        return ["integration.definition.platform: missing (papi registers nothing without it)"]
    if not _NAME_RE.match(platform):
        errors.append(f"platform: {platform!r} must match {_NAME_RE.pattern}")

    plurals: dict[str, str] = {}
    for name, spec in types.items():
        if not _NAME_RE.match(name):
            errors.append(f"types.{name}: name must match {_NAME_RE.pattern}")
        plural = spec.get("plural")
        if not isinstance(plural, str) or not _NAME_RE.match(plural):
            errors.append(f"types.{name}.plural: {plural!r} must match {_NAME_RE.pattern}")
        elif plural.lower() in plurals:
            errors.append(f"types.{name}.plural: {plural!r} is also the plural of {plurals[plural.lower()]!r}")
        else:
            plurals[plural.lower()] = name

    edge_types = definition.get("edgeTypes") or {}
    for name, spec in edge_types.items():
        if not isinstance(spec, dict) or not spec.get("title"):
            errors.append(f"edgeTypes.{name}: needs a title")
    allowed_edges = CORE_EDGE_TYPES | set(edge_types)

    seen_keys: set[str] = set()
    for i, facet in enumerate(facets):
        key = facet.get("key", "")
        where = f"facets[{i}] ({key})"
        if not _FACET_KEY_RE.match(key):
            errors.append(f"{where}.key: must match {_FACET_KEY_RE.pattern}")
        if key in seen_keys:
            errors.append(f"{where}.key: declared more than once")
        seen_keys.add(key)
        for ref in (facet.get("appliesTo") or {}).get("types") or []:
            qualifier, _ = _split(ref)
            if qualifier is not None and qualifier != platform:
                errors.append(f"{where}.appliesTo: {ref!r} is not a {platform} type (facets apply to the own platform)")

    seen_ids: set[str] = set()
    for i, rule in enumerate(rules):
        rid = rule.get("id", "")
        where = f"rules[{i}] ({rid})"
        if rid in seen_ids:
            errors.append(f"{where}.id: declared more than once")
        seen_ids.add(rid)
        edge = rule.get("edgeType")
        if edge not in allowed_edges:
            errors.append(f"{where}.edgeType: {edge!r} is not a core edge type; map it to one or declare it in edgeTypes")
        strategy = rule.get("strategy")
        spec = STRATEGY_PARAMS.get(strategy)
        if spec is None:
            errors.append(f"{where}.strategy: {strategy!r} is not one of {sorted(STRATEGY_PARAMS)}")
            continue
        params = rule.get("params") or {}
        for param, required in spec.items():
            if required and param not in params:
                errors.append(f"{where}.params: strategy {strategy!r} requires {param!r}")
        for param in params:
            if param not in spec:
                errors.append(f"{where}.params.{param}: strategy {strategy!r} has no param {param!r}")
        if "scope" in spec and "scope" in params and params["scope"] not in SCOPES:
            errors.append(f"{where}.params.scope: {params['scope']!r} must be one of {sorted(SCOPES)}")
    return errors


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if not paths:
        print("usage: papi_register_check.py <compiled.json> [...]", file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        errors = check_compiled(json.loads(Path(path).read_text()))
        for error in errors:
            print(f"{path}: {error}")
        if errors:
            failed = True
        else:
            print(f"{path}: integration registers through papi's pack validator")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
