"""Each runtime-<name>.yml workflow builds, checks and publishes its own runtime and no other.

check_runtime.py defaults to runtimes/kubernetes, so a workflow copied for another runtime that
keeps a bare `validate` or `compare` validates the kubernetes descriptor and passes."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("runtime-*.yml"))


def name_of(path):
    return path.stem.removeprefix("runtime-")


def test_the_runtime_workflows_are_found():
    assert {name_of(p) for p in WORKFLOWS} >= {"kubernetes", "aws"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=name_of)
def test_every_validate_and_compare_names_the_workflows_own_runtime_dir(path):
    name = name_of(path)
    text = path.read_text()
    calls = re.findall(r"check_runtime\.py (validate|compare)([^\n]*)", text)
    assert calls, f"{path.name} never runs check_runtime.py validate or compare"
    for verb, rest in calls:
        if name == "kubernetes":  # the script's default runtime
            assert "--runtime-dir" not in rest or f"--runtime-dir runtimes/{name}" in rest
            continue
        assert f"--runtime-dir runtimes/{name}" in rest, f"{path.name}: `{verb}` checks the default"


@pytest.mark.parametrize("path", WORKFLOWS, ids=name_of)
def test_a_workflow_mentions_only_its_own_runtime_directory(path):
    name = name_of(path)
    others = set(re.findall(r"runtimes/([a-z][a-z0-9-]*)", path.read_text())) - {name}
    assert not others, f"{path.name} refers to runtimes/{sorted(others)}"
