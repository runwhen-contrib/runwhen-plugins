"""The shared files, the integration's data and the tasks' declared permissions agree.

Skills do not import each other, so every gcp skill carries a verbatim copy of `errors.py` and
`gcloud.py`; these tests fail when a copy drifts from gcp-base's. `sync.py` is a copy of the
kubernetes plugin's (and the postgres plugin's), with its one errors import changed."""

import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
PLUGINS = PLUGIN.parent
SHARED = ("errors.py", "gcloud.py")
# read Cloud Functions (list, describe): carried by the skills that do, verbatim like the files above
FUNCTION_READERS = {"gcp-base", "gcp-functions-health"}
# every gcp skill that runs gcloud carries the shared files; a skill missing here is untested
SKILLS_WITH_COPIES = {"gcp-base", "gcp-bucket-health", "gcp-functions-health"}


def copies(name):
    return sorted(SKILLS.glob(f"*/lib/*/{name}"))


@pytest.mark.parametrize("name", SHARED)
def test_every_skills_copy_of_a_shared_file_is_identical_to_gcp_bases(name):
    reference = (SKILLS / "gcp-base" / "lib" / "gcp_base" / name).read_bytes()
    for path in copies(name):
        assert path.read_bytes() == reference, f"{path} differs from gcp-base's {name}"


@pytest.mark.parametrize("name", SHARED)
def test_each_skill_that_runs_gcloud_has_a_copy_and_none_is_unregistered(name):
    assert {p.parents[2].name for p in copies(name)} == SKILLS_WITH_COPIES


def test_every_function_reader_carries_an_identical_copy_of_fnlist():
    paths = sorted(SKILLS.glob("*/lib/*/fnlist.py"))
    assert {p.parents[2].name for p in paths} == FUNCTION_READERS
    reference = (SKILLS / "gcp-base" / "lib" / "gcp_base" / "fnlist.py").read_bytes()
    for path in paths:
        assert path.read_bytes() == reference, f"{path} differs from gcp-base's fnlist.py"


def test_fnlist_imports_only_its_siblings_the_sdk_and_the_stdlib():
    import ast

    tree = ast.parse((SKILLS / "gcp-base" / "lib" / "gcp_base" / "fnlist.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module.split(".")[0] in {"runwhen_capability"}
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module in {"errors", "gcloud"}


@pytest.mark.parametrize("name", SHARED)
def test_shared_files_import_only_their_siblings_the_sdk_and_the_stdlib(name):
    import ast

    tree = ast.parse((SKILLS / "gcp-base" / "lib" / "gcp_base" / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module.split(".")[0] in {"runwhen_capability", "json", "re", "subprocess"}
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module in {"errors", None}


# --- sync.py: a cross-plugin copy ---------------------------------------------------------------
# Plugins do not import each other (a task sees only its own skill's lib/), so sync.py is a copy,
# not an import. The one line that names the skill's own package is the only difference.


def own_package_import(text, package):
    return text.replace(f"from {package}.errors import RwError", "from <pkg>.errors import RwError")


@pytest.mark.parametrize(
    "other, package",
    [
        (
            PLUGINS / "kubernetes" / "skills" / "k8s-base" / "lib" / "k8s_base" / "sync.py",
            "k8s_base",
        ),
        (PLUGINS / "postgres" / "skills" / "pg-base" / "lib" / "pg_base" / "sync.py", "pg_base"),
    ],
)
def test_sync_py_is_the_other_plugins_copy_but_for_its_one_errors_import(other, package):
    ours = (SKILLS / "gcp-base" / "lib" / "gcp_base" / "sync.py").read_text()
    theirs = other.read_text()
    assert "from gcp_base.errors import RwError" in ours
    assert f"from {package}.errors import RwError" in theirs
    assert own_package_import(ours, "gcp_base") == own_package_import(theirs, package), (
        f"gcp-base's sync.py drifted from {package}'s: re-copy it, changing only the errors import"
    )
    assert "k8s" not in ours and "pg_" not in ours  # the copy imports nothing across plugins


# --- the integration and the tasks --------------------------------------------------------------


def tasks_doc(skill):
    return yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())


def all_skills():
    return sorted(p.name for p in SKILLS.iterdir() if (p / "tasks.yaml").is_file())


def test_every_declared_permission_is_a_valid_gcp_permission():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "gcp-permission.json").read_text())
    for skill in all_skills():
        for task in tasks_doc(skill)["tasks"]:
            for permission in task["permissions"]:
                jsonschema.validate(permission, schema)


def test_the_entry_points_name_tasks_that_exist():
    integration = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    assert set(integration["entryPoints"]) == {"discovery"}
    for entry in integration["entryPoints"].values():
        skill, _, name = entry["task"].partition("/")
        assert name in {t["name"] for t in tasks_doc(skill)["tasks"]}


def test_the_root_declares_the_key_discover_reports():
    from gcp_base import discovery as dc

    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["project"]["canonicalKey"] == dc.CANONICAL_KEY
    assert types["project"]["rootKey"] == "projectId"
    assert types["bucket"]["parent"] == "project"
    # the same function id can exist in two regions: the region is part of a function's identity
    assert types["region"] == {"parent": "project", "plural": "regions"}
    assert types["cloud-function"] == {"parent": "region", "plural": "cloud-functions"}
    assert set(types) == {"project", "bucket", "region", "cloud-function"}


@pytest.mark.parametrize("skill", all_skills())
def test_every_task_is_read_only_runs_in_the_gcp_runtime_and_declares_the_shared_settings(skill):
    doc = tasks_doc(skill)
    assert doc["runtime"] == "gcp@1" and doc["defaults"]["credentials"] == "gcp/adc"
    assert doc["settings"]["maxItems"]["default"] == 200
    assert doc["settings"]["commandTimeout"]["type"] == "duration"
    runtime = yaml.safe_load((PLUGIN.parents[1] / "runtimes" / "gcp" / "runtime.yaml").read_text())
    assert runtime["name"] == "gcp" and re.fullmatch(r"1\.\d+\.\d+", runtime["version"])
    for task in doc["tasks"]:
        assert task["readOnly"] is True


@pytest.mark.parametrize("skill", all_skills())
def test_each_output_schema_is_valid_2020_12_and_names_no_verdict(skill):
    for task in tasks_doc(skill)["tasks"]:
        for out_name, out in task["outputs"].items():
            assert out_name not in {"status", "errors"}
            if "schema" not in out:
                continue
            schema = json.loads((SKILLS / skill / out["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert not {"severity", "title", "nextSteps"} & set(schema["properties"])
