"""The plugin's own shape: what CI's validate does not say, and what the registry used to enforce."""

import re

import yaml
from conftest import PLUGIN
from plugin_tools import TOOLS_VERSION

SKILL = PLUGIN / "skills" / "plugin-tools"


def test_tools_version_is_the_plugin_version():
    doc = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert TOOLS_VERSION == f"plugin-tools@{doc['version']}" and doc["name"] == "plugin-tools"
    assert doc["version"] == "1.0.0"


def test_every_task_runs_on_universal_and_declares_no_write_permission():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    assert doc["runtime"] == "universal@1"
    assert [t["name"] for t in doc["tasks"]] == ["sync", "validate-upload"]
    for task in doc["tasks"]:
        assert "runtime" not in task or task["runtime"] == "universal@1"
        assert task["readOnly"] is True
        assert (
            not task.get("permissions") and not task.get("effects") and not task.get("destructive")
        )


def test_git_write_is_not_wired_to_any_task():
    for script in (SKILL / "scripts").glob("*.py"):
        assert "git_write" not in script.read_text()
    # and nothing but its own tests imports it
    users = [
        p.name
        for p in (SKILL / "lib").rglob("*.py")
        if "git_write" in p.read_text() and p.name != "git_write.py"
    ]
    assert users == []


def test_integration_defines_the_two_credential_types_and_the_repository_type():
    integ = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())
    assert set(integ["credentialTypes"]) == {"git/https-token", "git/github-app"}
    assert integ["credentialTypes"]["git/https-token"]["materialize"]["env"] == [
        "GIT_USERNAME",
        "GIT_TOKEN",
    ]
    assert types["types"]["repository"]["root"] is True and integ["platform"] == "git"


def test_the_token_is_never_in_a_task_declaration_or_a_url():
    for path in (SKILL / "tasks.yaml", PLUGIN / "integration" / "integration.yaml"):
        assert not re.search(r"https://[^\s/]*:[^\s/]*@", path.read_text())
