"""No recorded fixture or captured unit data may hold a secret-looking env value.

A `kubectl get -o json` of a workload or pod returns its container `env`, and a recording keeps
what the tool printed. This scans every skill's `tests/**/*.yaml` fixtures and
`tests/unit/data/**` captures (JSON inside a recorded `stdout`, in an annotation such as
`last-applied-configuration`, or plain) for env entries whose `name` looks like a credential and
that carry a literal `value` (a `valueFrom` reference is not a value). Re-record with a narrowed
fetch (`-o jsonpath`) rather than editing a fixture by hand.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]  # plugins/kubernetes
SENSITIVE_NAME = re.compile(r"(?i)pass|secret|token|key|credential|seal|auth")

# Env names that match SENSITIVE_NAME and are known to carry no secret, by exact name, each with
# why. Add a name here only when its value is a path, a switch or a reference, never a credential.
BENIGN_NAMES = {
    # (none yet)
}


def scanned_files():
    files = []
    for skill in sorted((PLUGIN / "skills").iterdir()):
        files += sorted((skill / "tests").glob("**/*.yaml")) if (skill / "tests").is_dir() else []
    files += sorted((PLUGIN / "tests" / "unit" / "data").glob("**/*.json"))
    files += sorted((PLUGIN / "tests" / "unit" / "data").glob("**/*.yaml"))
    return files


def load(path):
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def env_hits(node, where="$"):
    """[(where, env name)] of every env entry with a sensitive name and a literal value."""
    hits = []
    if isinstance(node, dict):
        env = node.get("env")
        if isinstance(env, list):
            for entry in env:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and SENSITIVE_NAME.search(entry["name"])
                    and entry["name"] not in BENIGN_NAMES
                    and entry.get("value") not in (None, "")
                ):
                    hits.append((where, entry["name"]))
        for key, value in node.items():
            hits += env_hits(value, f"{where}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits += env_hits(value, f"{where}[{i}]")
    elif isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
        try:  # a recorded stdout, or an annotation that embeds the manifest
            hits += env_hits(json.loads(node), where)
        except ValueError:
            pass
    return hits


# Skills whose fetches are narrowed (`-o jsonpath`): their recordings hold no env entry at all, of
# any name (env_names also reads the manifest a `last-applied-configuration` annotation repeats).
NARROWED_SKILLS = ("k8s-redis-health", "k8s-vault-health")


def env_names(node):
    names = []
    if isinstance(node, dict):
        if isinstance(node.get("env"), list):
            names += [e.get("name") for e in node["env"] if isinstance(e, dict)]
        for value in node.values():
            names += env_names(value)
    elif isinstance(node, list):
        for value in node:
            names += env_names(value)
    elif isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
        try:
            names += env_names(json.loads(node))
        except ValueError:
            pass
    return names


@pytest.mark.parametrize("skill", NARROWED_SKILLS)
def test_narrowed_skills_record_no_env_at_all(skill):
    files = sorted((PLUGIN / "skills" / skill / "tests").glob("**/*.yaml"))
    files += sorted((PLUGIN / "tests" / "unit" / "data" / skill.replace("-", "_")).glob("*.json"))
    assert files
    for path in files:
        assert not env_names(load(path)), f"{path.relative_to(PLUGIN)} records an env entry"


def test_the_scan_covers_the_fixtures_and_the_unit_data():
    files = scanned_files()
    assert any(f.parts[-2] == "tests" or "tests" in f.parts for f in files)
    assert any("unit" in f.parts and "data" in f.parts for f in files)


def test_the_scanner_finds_a_literal_secret_env_in_each_place_it_can_hide():
    env = [{"name": "DB_PASSWORD", "value": "hunter2"}, {"name": "MODE", "value": "x"}]
    manifest = {"spec": {"template": {"spec": {"containers": [{"name": "c", "env": env}]}}}}
    assert env_hits(manifest)
    assert env_hits({"stdout": json.dumps(manifest)})  # a recorded kubectl stdout
    annotated = {"metadata": {"annotations": {"last-applied": json.dumps(manifest)}}}
    assert env_hits(annotated)
    assert env_hits(yaml.safe_load(yaml.safe_dump({"calls": [{"stdout": json.dumps(manifest)}]})))
    from_secret = [
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}
    ]
    assert not env_hits({"env": from_secret})  # a reference is not a value
    assert not env_hits({"env": [{"name": "MODE", "value": "x"}]})
    assert not env_hits({"env": [{"name": "DB_PASSWORD", "value": ""}]})


@pytest.mark.parametrize("path", scanned_files(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_no_fixture_holds_a_literal_secret_env_value(path):
    hits = env_hits(load(path))
    assert not hits, (
        f"{path.relative_to(PLUGIN)} records env values for credential-looking names "
        f"({sorted({name for _, name in hits})}); narrow the fetch and re-record"
    )
