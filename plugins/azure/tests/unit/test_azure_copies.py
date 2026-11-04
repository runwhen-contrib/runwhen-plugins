"""The shared files stay copies: every azure skill's errors.py and az.py equal azure-base's, and
azure-base's sync.py is the postgres copy of k8s-base's but for its one errors import.

Skills do not import each other (a task sees only its own skill's lib/), so the shared code is
copied; these tests fail when a copy drifts."""

import ast
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
POSTGRES_SYNC = PLUGIN.parent / "postgres" / "skills" / "pg-base" / "lib" / "pg_base" / "sync.py"
K8S_SYNC = PLUGIN.parent / "kubernetes" / "skills" / "k8s-base" / "lib" / "k8s_base" / "sync.py"
SHARED = ("errors.py", "az.py")

# every azure skill that runs az carries the shared files; a skill missing here is untested
SKILLS_WITH_COPIES = {"azure-base", "azure-kv-health"}


def copies(name):
    return sorted(SKILLS.glob(f"*/lib/*/{name}"))


@pytest.mark.parametrize("name", SHARED)
def test_every_skills_copy_of_a_shared_file_is_identical_to_azure_bases(name):
    reference = (SKILLS / "azure-base" / "lib" / "azure_base" / name).read_bytes()
    for path in copies(name):
        assert path.read_bytes() == reference, f"{path} differs from azure-base's {name}"


@pytest.mark.parametrize("name", SHARED)
def test_each_skill_that_runs_az_has_a_copy_and_none_is_unregistered(name):
    assert {p.parents[2].name for p in copies(name)} == SKILLS_WITH_COPIES


@pytest.mark.parametrize("name", SHARED)
def test_shared_files_import_only_their_siblings_and_the_sdk(name):
    tree = ast.parse((SKILLS / "azure-base" / "lib" / "azure_base" / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module.split(".")[0] in {"runwhen_capability", "json", "re", "subprocess"}
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module in {"errors", None}


def own_package_import(text, package):
    return text.replace(f"from {package}.errors import RwError", "from <pkg>.errors import RwError")


def test_sync_py_is_the_postgres_copy_but_for_its_one_errors_import():
    ours = (SKILLS / "azure-base" / "lib" / "azure_base" / "sync.py").read_text()
    theirs = POSTGRES_SYNC.read_text()
    assert "from azure_base.errors import RwError" in ours
    assert "from pg_base.errors import RwError" in theirs
    assert own_package_import(ours, "azure_base") == own_package_import(theirs, "pg_base"), (
        "azure-base's sync.py drifted from pg-base's: re-copy it, changing only the errors import"
    )
    assert (
        "pg_base" not in ours and "postgres" not in ours
    )  # the copy imports nothing across plugins


def test_sync_py_is_still_the_k8s_base_copy_too():
    ours = (SKILLS / "azure-base" / "lib" / "azure_base" / "sync.py").read_text()
    theirs = K8S_SYNC.read_text()
    assert own_package_import(ours, "azure_base") == own_package_import(theirs, "k8s_base")


def test_budget_py_is_the_postgres_copy_with_the_package_renamed():
    ours = (SKILLS / "azure-base" / "lib" / "azure_base" / "budget.py").read_text()
    theirs = (POSTGRES_SYNC.parent / "budget.py").read_text()
    assert ours == theirs.replace("pg_base", "azure_base")
