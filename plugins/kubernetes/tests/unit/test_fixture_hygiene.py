"""No recorded fixture or captured unit data may hold a secret-looking env value, or kubectl's
client debug output.

A `kubectl get -o json` of a workload or pod returns its container `env`, and a recording keeps
what the tool printed. This scans every skill's `tests/**/*.yaml` fixtures and
`tests/unit/data/**` captures (`*.json`, `*.yaml` and `*.txt`; JSON inside a recorded `stdout`, in
an annotation such as `last-applied-configuration`, or plain) for env entries whose `name` looks
like a credential and that carry a literal `value` (a `valueFrom` reference is not a value).
Re-record with a narrowed fetch (`-o jsonpath`) rather than editing a fixture by hand.

It also scans every fixture and every file of the unit data for what `kubectl -v=8` and the like
leave in a recording: the API server's `127.0.0.1:<port>` address, a `kubectl/v...` user agent and
an `Audit-Id` header. Those are client debug output, which a fixture has no business holding.
`GRANDFATHERED` lists the files that held such text before the scan existed (other lanes'
recordings, and application data that is legitimately an address): it only shrinks.
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


def fixture_files():
    files = []
    for skill in sorted((PLUGIN / "skills").iterdir()):
        files += sorted((skill / "tests").glob("**/*.yaml")) if (skill / "tests").is_dir() else []
    return files


def data_files():
    """Every file of the unit data, whatever its suffix."""
    return sorted(f for f in (PLUGIN / "tests" / "unit" / "data").rglob("*") if f.is_file())


def scanned_files():
    """The files the env scan reads: fixtures, and the unit data's json, yaml and txt."""
    files = fixture_files()
    for suffix in ("json", "yaml", "txt"):
        files += sorted((PLUGIN / "tests" / "unit" / "data").glob(f"**/*.{suffix}"))
    return files


def load(path):
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text) if path.suffix == ".yaml" else text


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
NARROWED_SKILLS = (
    "k8s-jaeger-health",
    "k8s-litellm-health",
    "k8s-redis-health",
    "k8s-vault-health",
)


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


# What client debug output (`kubectl -v=8`, `--v`) leaves in a recording.
LEAKS = {
    "the API server's 127.0.0.1:<port> address": re.compile(r"127\.0\.0\.1:\d+"),
    "a kubectl user agent": re.compile(r"kubectl/v\d"),
    "an Audit-Id header": re.compile(r"(?i)\baudit-id\b"),
}

# Files that held one of those before the scan existed, by path under plugins/kubernetes: the
# loopback addresses are mostly application data (Loki ring members, a Redis sentinel's replica
# list) or kind's API server URL in a recorded `kubectl config view`. Fix a file by re-recording
# it and delete its entry; never add one.
GRANDFATHERED = {
    "skills/k8s-base/tests/discover/forbidden-namespace-scoped.yaml": ("127.0.0.1",),
    "skills/k8s-base/tests/discover/forbidden.yaml": ("127.0.0.1",),
    "skills/k8s-base/tests/discover/namespace-scoped.yaml": ("127.0.0.1",),
    "skills/k8s-base/tests/discover/unreachable.yaml": ("127.0.0.1",),
    "skills/k8s-loki-health/tests/ring/broken.yaml": ("127.0.0.1",),
    "skills/k8s-loki-health/tests/ring/healthy.yaml": ("127.0.0.1",),
    "skills/k8s-loki-health/tests/ring/unserved-ring.yaml": ("127.0.0.1",),
    "skills/k8s-node-health/tests/overcommit/cluster.yaml": ("127.0.0.1",),
    "skills/k8s-node-health/tests/overcommit/healthy.yaml": ("127.0.0.1",),
    "skills/k8s-redis-health/tests/ping/sentinel.yaml": ("127.0.0.1",),
    "tests/unit/data/k8s_loki_health/compactor-ring.json": ("127.0.0.1",),
    "tests/unit/data/k8s_loki_health/distributor-ring.json": ("127.0.0.1",),
    "tests/unit/data/k8s_loki_health/ring-stale.json": ("127.0.0.1",),
    "tests/unit/data/k8s_loki_health/ring.json": ("127.0.0.1",),
    "tests/unit/data/k8s_node_health/pods.json": ("127.0.0.1",),
    "tests/unit/data/k8s_redis_health/info_sentinel.txt": ("127.0.0.1",),
}


def leak_hits(text):
    """The names of the LEAKS patterns that `text` contains."""
    return [name for name, pattern in LEAKS.items() if pattern.search(text)]


def leak_files():
    return fixture_files() + data_files()


def grandfathered(path):
    return GRANDFATHERED.get(path.relative_to(PLUGIN).as_posix())


def test_the_leak_scan_finds_each_kind_of_client_debug_output():
    assert leak_hits('url="https://127.0.0.1:6443/api/v1/namespaces"') == [
        "the API server's 127.0.0.1:<port> address"
    ]
    assert leak_hits("User-Agent: kubectl/v1.36.1 (darwin/arm64)") == ["a kubectl user agent"]
    assert leak_hits("Audit-Id: 6f306683-9dfa-4839-8bc9-366883ba5858") == ["an Audit-Id header"]
    assert leak_hits("audit-id: x") == ["an Audit-Id header"]
    assert not leak_hits("Error from server (Forbidden): <html>Authentication required</html>")
    assert not leak_hits("addr 127.0.0.1 and 10.0.0.1:8080 and kubectl exited 1")


def test_the_leak_scan_covers_fixtures_and_every_unit_data_suffix():
    suffixes = {f.suffix for f in leak_files()}
    assert {".yaml", ".json", ".txt"} <= suffixes
    assert any(f.suffix == ".html" for f in data_files())


@pytest.mark.parametrize(
    "path",
    [f for f in leak_files() if not grandfathered(f)],
    ids=lambda p: str(p.relative_to(PLUGIN)),
)
def test_no_fixture_or_unit_data_holds_client_debug_output(path):
    hits = leak_hits(path.read_text(errors="replace"))
    assert not hits, (
        f"{path.relative_to(PLUGIN)} holds {hits}: client debug output (kubectl -v=8); "
        "record without it"
    )


def test_the_grandfather_list_names_only_files_that_still_hold_a_leak():
    for rel in GRANDFATHERED:
        path = PLUGIN / rel
        assert path.is_file(), f"{rel} is gone: delete its GRANDFATHERED entry"
        assert leak_hits(path.read_text(errors="replace")), (
            f"{rel} no longer holds a leak: delete its GRANDFATHERED entry"
        )
