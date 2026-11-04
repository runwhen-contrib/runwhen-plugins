"""The shared files and the cross-plugin copy of sync.py agree.

Skills do not import each other, so every aws skill that calls the CLI carries a verbatim copy of
`errors.py` and `awscli.py`; these tests fail when a copy drifts from aws-base's. sync.py is the
postgres plugin's copy of kubernetes' k8s-base client, with the one errors import changed."""

import ast
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
SHARED = ("errors.py", "awscli.py")
SYNC = {
    "postgres": PLUGIN.parent / "postgres" / "skills" / "pg-base" / "lib" / "pg_base" / "sync.py",
    "kubernetes": PLUGIN.parent
    / "kubernetes"
    / "skills"
    / "k8s-base"
    / "lib"
    / "k8s_base"
    / "sync.py",
}
PACKAGE = {"postgres": "pg_base", "kubernetes": "k8s_base"}

# every aws skill that runs the CLI carries the shared files; a skill missing here is untested
SKILLS_WITH_COPIES = {"aws-base", "aws-lambda-health", "aws-ec2-health"}


def copies(name):
    return sorted(SKILLS.glob(f"*/lib/*/{name}"))


@pytest.mark.parametrize("name", SHARED)
def test_every_skills_copy_of_a_shared_file_is_identical_to_aws_bases(name):
    reference = (SKILLS / "aws-base" / "lib" / "aws_base" / name).read_bytes()
    for path in copies(name):
        assert path.read_bytes() == reference, f"{path} differs from aws-base's {name}"


@pytest.mark.parametrize("name", SHARED)
def test_each_skill_that_runs_the_cli_has_a_copy_and_none_is_unregistered(name):
    assert {p.parents[2].name for p in copies(name)} == SKILLS_WITH_COPIES


@pytest.mark.parametrize("name", SHARED)
def test_shared_files_import_only_their_siblings_and_the_sdk(name):
    tree = ast.parse((SKILLS / "aws-base" / "lib" / "aws_base" / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module.split(".")[0] in {"runwhen_capability", "json", "re", "subprocess"}
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module in {"errors", None}


def own_package_import(text, package):
    return text.replace(f"from {package}.errors import RwError", "from <pkg>.errors import RwError")


@pytest.mark.parametrize("origin", sorted(SYNC))
def test_sync_py_is_the_origins_copy_but_for_its_one_errors_import(origin):
    ours = (SKILLS / "aws-base" / "lib" / "aws_base" / "sync.py").read_text()
    theirs = SYNC[origin].read_text()
    assert "from aws_base.errors import RwError" in ours
    assert f"from {PACKAGE[origin]}.errors import RwError" in theirs
    assert own_package_import(ours, "aws_base") == own_package_import(theirs, PACKAGE[origin]), (
        f"aws-base's sync.py drifted from {origin}'s: re-copy it, changing only the errors import"
    )
    assert "pg_base" not in ours and "k8s_base" not in ours  # nothing is imported across plugins


def test_budget_py_is_the_postgres_one_with_the_package_name_changed():
    ours = (SKILLS / "aws-base" / "lib" / "aws_base" / "budget.py").read_text()
    theirs = SYNC["postgres"].with_name("budget.py").read_text()
    assert ours == theirs.replace("pg_base", "aws_base")


def test_text_py_is_aws_bases_in_every_skill_that_shows_tags():
    """The tag secret check is one rule: aws-base names the Name tag of an instance in discovery
    and aws-ec2-health lists the tags, with the same verdict."""
    reference = (SKILLS / "aws-base" / "lib" / "aws_base" / "text.py").read_bytes()
    holders = {p.parents[2].name for p in SKILLS.glob("*/lib/*/text.py")}
    assert holders == {"aws-base", "aws-ec2-health"}
    for path in SKILLS.glob("*/lib/*/text.py"):
        assert path.read_bytes() == reference, f"{path} differs from aws-base's text.py"
