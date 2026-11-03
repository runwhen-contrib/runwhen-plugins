"""The shared files, the integration's data and the tasks' declared permissions agree.

Skills do not import each other, so every postgres skill carries a verbatim copy of
`errors.py` and `pg.py`; these tests fail when a copy drifts from pg-base's. They also check
that the permissions each task declares are exactly the ones its SQL needs."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from pg_base import access as ax
from pg_base import discovery as dc

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
SHARED = ("errors.py", "pg.py")


def copies(name):
    return sorted(SKILLS.glob(f"*/lib/*/{name}"))


@pytest.mark.parametrize("name", SHARED)
def test_every_skills_copy_of_a_shared_file_is_identical_to_pg_bases(name):
    reference = (SKILLS / "pg-base" / "lib" / "pg_base" / name).read_bytes()
    for path in copies(name):
        assert path.read_bytes() == reference, f"{path} differs from pg-base's {name}"


@pytest.mark.parametrize("name", SHARED)
def test_shared_files_import_only_their_siblings_and_the_sdk(name):
    import ast

    tree = ast.parse((SKILLS / "pg-base" / "lib" / "pg_base" / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module.split(".")[0] in {"runwhen_capability", "json", "re", "subprocess"}
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert node.module in {"errors", None}


def task(name):
    doc = yaml.safe_load((SKILLS / "pg-base" / "tasks.yaml").read_text())
    return next(t for t in doc["tasks"] if t["name"] == name)


def test_every_declared_permission_is_a_valid_postgres_permission():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "pg-permission.json").read_text())
    for name in ("discover", "check-permissions"):
        for permission in task(name)["permissions"]:
            jsonschema.validate(permission, schema)
            assert ax.parse_rule(permission)[0] is not None


# The functions whose EXECUTE a task depends on and an admin may revoke; each must be declared
# exactly when the task's SQL calls it.
TRACKED = (
    "pg_control_system",
    "pg_database_size",
    "pg_has_role",
    "has_database_privilege",
    "has_function_privilege",
)


def declared_functions(name):
    return {p["function"].split(".")[1] for p in task(name)["permissions"] if "function" in p}


def tracked_in(sql):
    return {f for f in TRACKED if f + "(" in sql}


def test_discover_declares_the_functions_its_sql_calls():
    sql = dc.INSTANCE_SQL + dc.SYSTEM_IDENTIFIER_SQL + dc.DATABASES_SQL
    # pg_has_role and has_database_privilege appear only to decide whether pg_database_size may run
    assert declared_functions("discover") == tracked_in(sql) - {
        "pg_has_role",
        "has_database_privilege",
    }


def test_discover_declares_connect_because_pg_database_size_needs_it():
    assert {"database": "*", "privilege": "CONNECT"} in task("discover")["permissions"]


def test_check_permissions_declares_the_functions_its_sql_calls():
    sql = ax.facts_sql({"r"}, {"pg_catalog.f"}) + dc.SYSTEM_IDENTIFIER_SQL
    assert declared_functions("check-permissions") == tracked_in(sql)


def test_the_entry_points_name_tasks_that_exist():
    integration = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    for entry in integration["entryPoints"].values():
        skill, _, name = entry["task"].partition("/")
        doc = yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())
        assert name in {t["name"] for t in doc["tasks"]}


def test_the_root_declares_the_key_discover_reports():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["instance"]["canonicalKey"] == dc.CANONICAL_KEY
    assert types["instance"]["rootKey"] == ["host", "port"]
    assert types["database"]["parent"] == "instance"
    assert set(types) == {"instance", "database"}
