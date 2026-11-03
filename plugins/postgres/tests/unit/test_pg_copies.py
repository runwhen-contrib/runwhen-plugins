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
from pg_connections import connections as cn
from pg_long_running_queries import long_running as lr

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
SHARED = ("errors.py", "pg.py")


# every postgres skill that runs psql carries the shared files; a skill missing here is untested
SKILLS_WITH_COPIES = {
    "pg-base",
    "pg-connections",
    "pg-long-running-queries",
    "pg-locks",
    "pg-replication-lag",
}


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


def settings_doc(skill):
    return yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())


def task(name, skill="pg-base"):
    return next(t for t in settings_doc(skill)["tasks"] if t["name"] == name)


@pytest.mark.parametrize("name", SHARED)
def test_each_skill_that_runs_psql_has_a_copy_and_none_is_unregistered(name):
    assert {p.parents[2].name for p in copies(name)} == SKILLS_WITH_COPIES


SESSION_TASKS = (("pg-connections", "connections"), ("pg-long-running-queries", "long-running"))
HEALTH_SKILLS = (
    "pg-connections",
    "pg-long-running-queries",
    "pg-locks",
    "pg-replication-lag",
)


@pytest.mark.parametrize("skill", HEALTH_SKILLS)
def test_declared_permissions_of_the_health_skills_are_valid_postgres_permissions(skill):
    schema = json.loads((PLUGIN / "integration" / "schemas" / "pg-permission.json").read_text())
    for t in settings_doc(skill)["tasks"]:
        for permission in t["permissions"]:
            jsonschema.validate(permission, schema)
            assert ax.parse_rule(permission)[0] is not None


def test_every_declared_permission_is_a_valid_postgres_permission():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "pg-permission.json").read_text())
    for skill, name in (("pg-base", "discover"), ("pg-base", "check-permissions"), *SESSION_TASKS):
        for permission in task(name, skill)["permissions"]:
            jsonschema.validate(permission, schema)
            assert ax.parse_rule(permission)[0] is not None


# The functions whose EXECUTE a task depends on and an admin may revoke; each must be declared
# exactly when the task's SQL calls it.
TRACKED = (
    "pg_backend_pid",
    "pg_blocking_pids",
    "pg_control_system",
    "pg_database_size",
    "pg_has_role",
    "has_database_privilege",
    "has_function_privilege",
)


def declared_functions(name, skill="pg-base"):
    return {
        p["function"].split(".")[1] for p in task(name, skill)["permissions"] if "function" in p
    }


def tracked_in(sql):
    return {f for f in TRACKED if f + "(" in sql}


def test_discover_declares_exactly_the_functions_its_sql_calls():
    sql = dc.INSTANCE_SQL + dc.SYSTEM_IDENTIFIER_SQL + dc.DATABASES_SQL
    assert declared_functions("discover") == tracked_in(sql)
    assert tracked_in(sql) == {
        "pg_control_system",
        "pg_database_size",
        "pg_has_role",
        "has_database_privilege",
    }


def test_discover_declares_no_database_privilege():
    # pg_database_size is only asked where the role may size the database, so discover works on
    # every instance a role can connect to: it needs no CONNECT on all of them
    assert not [p for p in task("discover")["permissions"] if "database" in p]


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


# --- the session skills -------------------------------------------------------------------------
# pg_stat_activity shows other roles' sessions (state, query, wait event, times) only to a
# superuser and to members of pg_read_all_stats, which pg_monitor includes.


def test_the_session_tasks_declare_the_functions_their_sql_calls():
    assert declared_functions("connections", "pg-connections") == tracked_in(cn.connections_sql(5))
    for include_query_text in (False, True):
        sql = lr.long_running_sql(1000, 5, include_query_text)
        assert declared_functions("long-running", "pg-long-running-queries") == tracked_in(sql)


@pytest.mark.parametrize("skill, name", SESSION_TASKS)
def test_the_session_tasks_declare_the_role_that_unmasks_other_sessions(skill, name):
    permissions = task(name, skill)["permissions"]
    assert {"role": "pg_read_all_stats"} in permissions
    # no database privilege is declared: the task reads the cluster-wide view from the
    # connection's own database, and CONNECT on it is what the credential already needs
    assert not [p for p in permissions if "database" in p]


@pytest.mark.parametrize("skill, name", SESSION_TASKS)
def test_the_session_tasks_are_read_only_and_declare_the_shared_settings(skill, name):
    doc = settings_doc(skill)
    assert task(name, skill)["readOnly"] is True
    assert doc["runtime"] == "kubernetes@1" and doc["defaults"]["credentials"] == "postgres/libpq"
    assert doc["settings"]["maxItems"]["default"] == 200
    for key in ("connectTimeout", "statementTimeout"):
        assert doc["settings"][key]["type"] == "duration"


def test_include_query_text_is_a_setting_that_defaults_to_off():
    setting = settings_doc("pg-long-running-queries")["settings"]["includeQueryText"]
    assert (setting["type"], setting["default"]) == ("boolean", False)
    assert "includeQueryText" not in settings_doc("pg-connections")["settings"]


def test_min_duration_is_an_input_that_defaults_to_five_minutes():
    inputs = task("long-running", "pg-long-running-queries")["inputs"]
    assert inputs["minDuration"]["type"] == "duration" and inputs["minDuration"]["default"] == "5m"


@pytest.mark.parametrize("skill", ["pg-connections", "pg-long-running-queries"])
def test_each_session_skills_output_schema_is_valid_2020_12_and_names_no_verdict(skill):
    doc = settings_doc(skill)
    for t in doc["tasks"]:
        for out_name, out in t["outputs"].items():
            assert out_name not in {"status", "errors"}
            schema = json.loads((SKILLS / skill / out["schema"]).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert not {"severity", "title", "nextSteps"} & set(schema["properties"])
