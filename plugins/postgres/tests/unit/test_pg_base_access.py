"""check-permissions, pure half: rule parsing, the facts statement, and the verdict per rule."""

import json
from pathlib import Path

import jsonschema
import pytest
from pg_base import access as ax

PLUGIN = Path(__file__).parents[2]
SCHEMA = json.loads((PLUGIN / "integration" / "schemas" / "pg-permission.json").read_text())

VALID = [
    {"role": "pg_monitor"},
    {"role": "pg_signal_backend"},
    {"database": "orders", "privilege": "CONNECT"},
    {"database": "*", "privilege": "CREATE"},
    {"database": "orders", "privilege": "TEMPORARY"},
    {"function": "pg_catalog.pg_control_system", "privilege": "EXECUTE"},
]
INVALID = [
    {},
    {"role": ""},
    {"role": "x" * 64},
    {"role": "pg_monitor", "privilege": "USAGE"},
    {"database": "orders"},
    {"database": "orders", "privilege": "SELECT"},
    {"database": "orders", "privilege": "connect"},
    {"database": "", "privilege": "CONNECT"},
    {"function": "pg_control_system", "privilege": "EXECUTE"},
    {"function": "pg_catalog.pg_control_system()", "privilege": "EXECUTE"},
    {"function": "pg_catalog.pg_control_system", "privilege": "CONNECT"},
    {"function": "pg_catalog.pg_control_system"},
    {"table": "orders"},
    {"role": "a", "database": "b", "privilege": "CONNECT"},
]


@pytest.mark.parametrize("rule", VALID)
def test_the_schema_and_the_parser_accept_the_same_rules(rule):
    jsonschema.validate(rule, SCHEMA)
    parsed, why = ax.parse_rule(rule)
    assert parsed is not None and why is None


@pytest.mark.parametrize("rule", INVALID)
def test_the_schema_and_the_parser_reject_the_same_rules(rule):
    assert not jsonschema.Draft202012Validator(SCHEMA).is_valid(rule)
    parsed, why = ax.parse_rule(rule)
    assert parsed is None and why


def test_a_rule_that_is_not_an_object_has_a_reason():
    assert ax.parse_rule("pg_monitor") == (None, "the rule is not an object")


def facts(**over):
    base = {
        "user": "app",
        "superuser": False,
        "databases": [
            {
                "name": "orders",
                "template": False,
                "allowConn": True,
                "CONNECT": True,
                "CREATE": False,
                "TEMPORARY": True,
            },
            {
                "name": "postgres",
                "template": False,
                "allowConn": True,
                "CONNECT": True,
                "CREATE": False,
                "TEMPORARY": True,
            },
            {
                "name": "template0",
                "template": True,
                "allowConn": False,
                "CONNECT": True,
                "CREATE": False,
                "TEMPORARY": False,
            },
            {
                "name": "sealed",
                "template": False,
                "allowConn": False,
                "CONNECT": True,
                "CREATE": False,
                "TEMPORARY": False,
            },
        ],
        "roles": [
            {"role": "pg_monitor", "exists": True, "usage": True, "member": True},
            {"role": "pg_signal_backend", "exists": True, "usage": False, "member": False},
            {"role": "noinherit", "exists": True, "usage": False, "member": True},
            {"role": "ghost", "exists": False, "usage": False, "member": False},
        ],
        "functions": [
            {"function": "pg_catalog.one", "overloads": [{"signature": "one()", "execute": True}]},
            {
                "function": "pg_catalog.none",
                "overloads": [{"signature": "none(integer)", "execute": False}],
            },
            {
                "function": "pg_catalog.mixed",
                "overloads": [
                    {"signature": "mixed(integer)", "execute": True},
                    {"signature": "mixed(text)", "execute": False},
                ],
            },
            {"function": "pg_catalog.gone", "overloads": []},
        ],
    }
    base.update(over)
    return base


def verdict(rule, f=None):
    parsed, why = ax.parse_rule(rule)
    assert why is None, why
    return ax.verdict(parsed, f or facts())


def test_role_membership_with_inheritance_is_granted():
    v, scope, evidence = verdict({"role": "pg_monitor"})
    assert (v, scope) == ("granted", None)
    assert "member of pg_monitor" in evidence


def test_a_role_the_identity_is_not_in_is_denied():
    assert verdict({"role": "pg_signal_backend"})[0] == "denied"


def test_membership_without_inherit_is_denied_and_says_so():
    v, _, evidence = verdict({"role": "noinherit"})
    assert v == "denied" and "INHERIT" in evidence


def test_a_missing_role_is_denied():
    v, _, evidence = verdict({"role": "ghost"})
    assert v == "denied" and "does not exist" in evidence


def test_a_superuser_holds_every_role_that_exists():
    f = facts(superuser=True)
    assert verdict({"role": "pg_signal_backend"}, f)[0] == "granted"
    assert verdict({"role": "ghost"}, f)[0] == "denied"


def test_a_named_database_privilege_is_read_from_the_facts():
    assert verdict({"database": "orders", "privilege": "CONNECT"})[0] == "granted"
    assert verdict({"database": "orders", "privilege": "CREATE"})[0] == "denied"
    assert verdict({"database": "orders", "privilege": "TEMPORARY"})[0] == "granted"


def test_a_missing_database_is_denied():
    v, _, evidence = verdict({"database": "nope", "privilege": "CONNECT"})
    assert v == "denied" and "does not exist" in evidence


def test_connect_to_a_database_that_refuses_connections_is_denied():
    v, _, evidence = verdict({"database": "sealed", "privilege": "CONNECT"})
    assert v == "denied" and "does not accept connections" in evidence


def test_star_covers_non_template_databases_that_accept_connections():
    v, scope, evidence = verdict({"database": "*", "privilege": "CONNECT"})
    assert (v, scope) == ("granted", None)
    assert "all 2 databases" in evidence


def test_star_is_partial_with_the_granted_databases_as_scope():
    f = facts()
    f["databases"][0]["CONNECT"] = False
    v, scope, evidence = verdict({"database": "*", "privilege": "CONNECT"}, f)
    assert (v, scope) == ("partial", ["postgres"])
    assert "1 of 2" in evidence


def test_star_is_denied_when_no_database_grants_it():
    v, scope, _ = verdict({"database": "*", "privilege": "CREATE"})
    assert (v, scope) == ("denied", None)


def test_star_with_nothing_to_check_is_unverified():
    f = facts(databases=[])
    assert verdict({"database": "*", "privilege": "CONNECT"}, f)[0] == "unverified"


def test_star_scope_is_cut_and_the_evidence_says_so():
    names = [f"db{i:04d}" for i in range(ax.MAX_SCOPE + 20)]
    dbs = [
        {
            "name": n,
            "template": False,
            "allowConn": True,
            "CONNECT": i != 0,
            "CREATE": False,
            "TEMPORARY": False,
        }
        for i, n in enumerate(names)
    ]
    v, scope, evidence = verdict({"database": "*", "privilege": "CONNECT"}, facts(databases=dbs))
    assert v == "partial" and len(scope) == ax.MAX_SCOPE
    assert "scope cut" in evidence


def test_function_verdicts_follow_the_overloads():
    assert verdict({"function": "pg_catalog.one", "privilege": "EXECUTE"})[0] == "granted"
    assert verdict({"function": "pg_catalog.none", "privilege": "EXECUTE"})[0] == "denied"
    v, scope, _ = verdict({"function": "pg_catalog.mixed", "privilege": "EXECUTE"})
    assert (v, scope) == ("partial", ["mixed(integer)"])


def test_a_missing_function_is_denied():
    v, _, evidence = verdict({"function": "pg_catalog.gone", "privilege": "EXECUTE"})
    assert v == "denied" and "does not exist" in evidence
    assert verdict({"function": "pg_catalog.unknown", "privilege": "EXECUTE"})[0] == "denied"


def test_evidence_counts_agree_in_number():
    assert "(1 overload)" in verdict({"function": "pg_catalog.one", "privilege": "EXECUTE"})[2]


def test_the_facts_statement_quotes_every_name_whatever_it_holds():
    sql = ax.facts_sql({"it's", "a$b"}, {"pg_catalog.x"})
    assert "ARRAY[$q0$a$b$q0$, $q0$it's$q0$]::text[]" in sql
    assert "ARRAY[$q0$pg_catalog.x$q0$]::text[]" in sql


def test_the_facts_statement_with_nothing_named_is_still_valid_sql_shape():
    sql = ax.facts_sql(set(), set())
    assert "unnest(ARRAY[]::text[])" in sql
    assert sql.count("ARRAY[]::text[]") == 2


def test_plain_unwraps_nested_mappings():
    from types import MappingProxyType

    wrapped = MappingProxyType({"a": MappingProxyType({"b": (1, 2)})})
    assert ax.plain(wrapped) == {"a": {"b": [1, 2]}}
