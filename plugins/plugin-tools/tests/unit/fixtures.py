"""Plugin file trees for tests (shared by the sync, git, upload and snapshot tests)."""

from plugin_tools.archive import FileEntry

_TASKS = """\
apiVersion: runwhen.com/tasks/v1
defaults:
  timeout: 60s
tasks:
  - name: run
    title: Run {skill}
    description: Runs the {skill} check.
    script: scripts/run.sh
    readOnly: true
"""

_SKILL = """\
---
name: {skill}
description: Checks {skill}. Use when {skill} looks unhealthy.
---
Run the task and read its output.
"""


def make_plugin_tree(
    name: str,
    version: str,
    skills: tuple[str, ...] = ("a",),
    integration: bool = True,
    requires: str = "",
) -> dict[str, FileEntry]:
    """A minimal valid plugin: plugin.yaml, one task per skill, optionally an integration.

    `requires` is raw plugin.yaml text (e.g. "requires: {kubernetes: ^0.2}\n") appended as is.
    """
    tree: dict[str, FileEntry] = {
        "plugin.yaml": FileEntry(
            f"apiVersion: runwhen.com/plugin/v1\nname: {name}\nversion: {version}\n"
            f"description: The {name} plugin.\n{requires}".encode()
        ),
        "README.md": FileEntry(f"# {name}\n".encode()),
    }
    if integration:
        tree["integration/integration.yaml"] = FileEntry(
            f"apiVersion: runwhen.com/integration/v1\nplatform: {name}\ntitle: {name}\n".encode()
        )
    for skill in skills:
        base = f"skills/{skill}"
        tree[f"{base}/SKILL.md"] = FileEntry(_SKILL.format(skill=skill).encode())
        tree[f"{base}/tasks.yaml"] = FileEntry(_TASKS.format(skill=skill).encode())
        tree[f"{base}/scripts/run.sh"] = FileEntry(
            f"#!/bin/sh\necho {skill}\n".encode(), executable=True
        )
        tree[f"{base}/lib/helpers.sh"] = FileEntry(b"# helpers\n")
    return tree
