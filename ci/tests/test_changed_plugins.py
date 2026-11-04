import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from changed_plugins import changed_plugins  # noqa: E402

ALL = ["kubernetes", "postgres", "azure", "aws", "gcp", "misc"]


def test_skill_file_maps_to_its_plugin():
    assert changed_plugins(["plugins/kubernetes/skills/x/SKILL.md"], ALL) == ["kubernetes"]


def test_multiple_plugins_sorted():
    paths = ["plugins/postgres/plugin.yaml", "plugins/aws/integration/types.yaml"]
    assert changed_plugins(paths, ALL) == ["aws", "postgres"]


def test_ci_tooling_change_selects_all_plugins():
    assert changed_plugins(["ci/changed_plugins.py"], ALL) == sorted(ALL)
    assert changed_plugins(["ci/papi_register_check.py"], ALL) == sorted(ALL)
    assert changed_plugins([".github/workflows/plugins.yml"], ALL) == sorted(ALL)


def test_runtime_change_selects_nothing():
    assert changed_plugins(["runtimes/kubernetes/Dockerfile"], ALL) == []


def test_readme_selects_nothing():
    assert changed_plugins(["README.md"], ALL) == []


def test_unknown_plugin_dir_ignored():
    assert changed_plugins(["plugins/nope/plugin.yaml"], ALL) == []


def test_k8s_base_sync_py_also_selects_the_plugins_that_copy_it():
    path = "plugins/kubernetes/skills/k8s-base/lib/k8s_base/sync.py"
    assert changed_plugins([path], ALL) == ["aws", "kubernetes", "postgres"]
    # a plugin that is not installed is not selected
    assert changed_plugins([path], ["kubernetes", "postgres"]) == ["kubernetes", "postgres"]


def test_another_k8s_base_file_selects_only_kubernetes():
    path = "plugins/kubernetes/skills/k8s-base/lib/k8s_base/discovery.py"
    assert changed_plugins([path], ALL) == ["kubernetes"]
