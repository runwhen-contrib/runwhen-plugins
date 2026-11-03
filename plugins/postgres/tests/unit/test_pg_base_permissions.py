"""pg-base/check-permissions through its script, with psql answered by a fake: the report's
shape (rw.access.v1), its order and hashes, the root's canonical key and how failures surface."""

import json

import pytest
from runwhen_capability.contracts import permission_hash, validate

FACTS = {
    "user": "rw_noperm",
    "superuser": False,
    "databases": [
        {
            "name": "orders",
            "template": False,
            "allowConn": True,
            "CONNECT": False,
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
    ],
    "roles": [{"role": "pg_monitor", "exists": True, "usage": False, "member": False}],
    "functions": [
        {
            "function": "pg_catalog.pg_control_system",
            "overloads": [{"signature": "pg_control_system()", "execute": True}],
        }
    ],
}
SID = '{"systemIdentifier": "7704057543498637352"}'
RULES = [
    {"role": "pg_monitor"},
    {"database": "*", "privilege": "CONNECT"},
    {"function": "pg_catalog.pg_control_system", "privilege": "EXECUTE"},
    {"table": "orders"},
    {"database": "orders", "privilege": "CONNECT"},
]


def answer(fake, facts=FACTS, sid=SID):
    fake.on("'superuser'", stdout=json.dumps(facts))
    fake.on("pg_control_system()\n", stdout=sid) if False else fake.on(
        "FROM pg_control_system()", stdout=sid
    )


def test_one_entry_per_rule_in_input_order_with_the_rules_hash(run_script, fake_psql):
    answer(fake_psql)
    run = run_script("check_permissions", rules=RULES)
    env = run.envelope
    assert env["status"] == "ok" and env["errors"] == []
    access = env["outputs"]["access"]
    assert validate("rw.access.v1", access) == []
    assert [e["rule"] for e in access["rules"]] == RULES
    assert [e["hash"] for e in access["rules"]] == [permission_hash(r) for r in RULES]
    assert [e["verdict"] for e in access["rules"]] == [
        "denied", "partial", "granted", "unverified", "denied",
    ]  # fmt: skip
    assert access["rules"][1]["scope"] == ["postgres"]
    assert all(e["evidence"] for e in access["rules"])


def test_the_root_reports_the_system_identifier_as_the_canonical_key(run_script, fake_psql):
    answer(fake_psql)
    access = run_script("check_permissions", rules=RULES[:1]).envelope["outputs"]["access"]
    assert access["root"] == {
        "canonicalKey": {"name": "systemIdentifier", "value": "7704057543498637352"}
    }
    assert access["checkedAt"].endswith("Z")


def test_a_role_that_may_not_read_pg_control_system_gets_no_root_but_a_report(
    run_script, fake_psql
):
    fake_psql.on("'superuser'", stdout=json.dumps(FACTS))
    fake_psql.on(
        "FROM pg_control_system()",
        rc=1,
        stderr="ERROR:  permission denied for function pg_control_system",
    )
    run = run_script("check_permissions", rules=RULES[:1])
    assert run.envelope["status"] == "ok"
    assert "root" not in run.envelope["outputs"]["access"]


def test_a_system_identifier_query_that_fails_for_another_reason_fails_the_task(
    run_script, fake_psql
):
    fake_psql.on("'superuser'", stdout=json.dumps(FACTS))
    fake_psql.on(
        "FROM pg_control_system()",
        rc=1,
        stderr="psql: error: connection to server failed: timeout expired",
    )
    run = run_script("check_permissions", rules=RULES[:1])
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == ["timeout"]


def test_rules_that_cannot_be_evaluated_do_not_need_a_database_for_the_facts(run_script, fake_psql):
    fake_psql.on("FROM pg_control_system()", stdout=SID)
    access = run_script("check_permissions", rules=[{"table": "x"}]).envelope["outputs"]["access"]
    assert [e["verdict"] for e in access["rules"]] == ["unverified"]
    assert len(fake_psql.calls) == 1  # only the system identifier


def test_the_facts_are_one_query_for_all_rules(run_script, fake_psql):
    answer(fake_psql)
    run_script("check_permissions", rules=RULES * 50)
    facts_calls = [s for s in fake_psql.sqls() if "'superuser'" in s]
    assert len(facts_calls) == 1


@pytest.mark.parametrize(
    "stderr,code",
    [
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  password authentication failed for user "app"',
            "permission_denied",
        ),
        (
            'psql: error: connection to server at "h", port 5432 failed: Connection refused',
            "unreachable",
        ),
        (
            'psql: error: connection to server at "h", port 5432 failed: FATAL:  database "orders" does not exist',
            "not_found",
        ),
    ],
)
def test_a_connection_failure_fails_the_task_with_its_code_and_no_report(
    run_script, fake_psql, stderr, code
):
    fake_psql.on("'superuser'", rc=2, stderr=stderr)
    run = run_script("check_permissions", rules=RULES)
    env = run.envelope
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == [code]
    assert env.get("outputs", {}) == {}


def test_more_than_500_rules_is_invalid_input(run_script, fake_psql):
    run = run_script("check_permissions", rules=[{"role": f"r{i}"} for i in range(501)])
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]
    assert fake_psql.calls == []


def test_exactly_500_rules_are_evaluated(run_script, fake_psql):
    roles = [{"role": f"r{i}"} for i in range(500)]
    answer(
        fake_psql, facts={**FACTS, "roles": [{"role": r["role"], "exists": False} for r in roles]}
    )
    access = run_script("check_permissions", rules=roles).envelope["outputs"]["access"]
    assert len(access["rules"]) == 500


def test_a_rule_that_is_not_an_object_is_invalid_input(run_script, fake_psql):
    run = run_script("check_permissions", rules=["pg_monitor"])
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]


def test_duplicate_rules_each_get_an_entry(run_script, fake_psql):
    answer(fake_psql)
    access = run_script("check_permissions", rules=[RULES[0], RULES[0]]).envelope["outputs"][
        "access"
    ]
    assert len(access["rules"]) == 2
    assert access["rules"][0]["hash"] == access["rules"][1]["hash"]


def test_the_session_is_read_only_and_bounded(run_script, fake_psql):
    answer(fake_psql)
    run_script("check_permissions", rules=RULES[:1])
    for call in fake_psql.calls:
        assert call["script"].startswith("BEGIN READ ONLY; SET LOCAL statement_timeout = 30000; ")
        assert call["script"].endswith("; COMMIT;")
        assert call["env"] == {"PGCONNECT_TIMEOUT": "10"}


def test_the_password_is_never_in_what_the_script_runs_or_returns(
    run_script, fake_psql, monkeypatch
):
    monkeypatch.setenv("PGPASSWORD", "s3cret-test-value")
    answer(fake_psql)
    run = run_script("check_permissions", rules=RULES)
    blob = json.dumps(run.envelope) + json.dumps([c["argv"] for c in fake_psql.calls])
    assert "s3cret-test-value" not in blob


CAPTURED_RULES = [
    {"role": "pg_monitor"},
    {"role": "pg_read_all_stats"},
    {"role": "pg_signal_backend"},
    {"role": "nosuchrole"},
    {"database": "*", "privilege": "CONNECT"},
    {"database": "orders", "privilege": "CONNECT"},
    {"database": "orders", "privilege": "CREATE"},
    {"function": "pg_catalog.pg_control_system", "privilege": "EXECUTE"},
    {"function": "pg_catalog.nofunc", "privilege": "EXECUTE"},
]


@pytest.mark.parametrize(
    "who,verdicts",
    [
        # facts captured from the local docker primary (tests/docker/up.sh), one per role
        (
            "rw_monitor",
            [
                "granted",
                "granted",
                "denied",
                "denied",
                "granted",
                "granted",
                "denied",
                "granted",
                "denied",
            ],
        ),
        (
            "rw_noperm",
            [
                "denied",
                "denied",
                "denied",
                "denied",
                "partial",
                "denied",
                "denied",
                "granted",
                "denied",
            ],
        ),
    ],
)
def test_facts_captured_from_a_real_server_give_the_expected_verdicts(
    run_script, fake_psql, data, who, verdicts
):
    fake_psql.on("'superuser'", stdout=data(f"facts_{who}.json"))
    fake_psql.on("FROM pg_control_system()", stdout=SID)
    access = run_script("check_permissions", rules=CAPTURED_RULES).envelope["outputs"]["access"]
    assert [e["verdict"] for e in access["rules"]] == verdicts


@pytest.mark.parametrize(
    "rule",
    [
        {"role": "pg_monitor", "n": 1e30},  # a float outside the canonical JSON range
        {"role": "pg_monitor", "n": float("inf")},
        {"role": "pg_monitor", "n": float("nan")},
    ],
)
def test_a_rule_that_cannot_be_hashed_is_unverified_and_the_rest_of_the_report_stands(
    run_script, fake_psql, rule
):
    answer(fake_psql)
    run = run_script("check_permissions", rules=[RULES[0], rule, RULES[2]])
    assert run.envelope["status"] == "ok"
    access = run.envelope["outputs"]["access"]
    assert validate("rw.access.v1", access) == []
    assert [e["verdict"] for e in access["rules"]] == ["denied", "unverified", "granted"]
    assert access["rules"][0]["hash"] == permission_hash(RULES[0])
    assert "cannot be hashed" in access["rules"][1]["evidence"]
    json.dumps(access, allow_nan=False)  # the reported rule is plain JSON


def test_a_malformed_answer_for_one_rule_is_unverified_for_that_rule_only(run_script, fake_psql):
    facts = {
        **FACTS,
        "functions": [
            {"function": "pg_catalog.pg_control_system", "overloads": [{"execute": True}]}
        ],
    }
    answer(fake_psql, facts=facts)
    access = run_script("check_permissions", rules=RULES).envelope["outputs"]["access"]
    assert [e["verdict"] for e in access["rules"]] == [
        "denied", "partial", "unverified", "unverified", "denied",
    ]  # fmt: skip
    assert "malformed" in access["rules"][2]["evidence"]


@pytest.mark.parametrize("answer_text", ['["x"]', '"x"', "1", ""])
def test_a_system_identifier_row_that_is_not_an_object_fails_the_task(
    run_script, fake_psql, answer_text
):
    fake_psql.on("'superuser'", stdout=json.dumps(FACTS))
    fake_psql.on("FROM pg_control_system()", stdout=answer_text)
    run = run_script("check_permissions", rules=RULES[:1])
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == ["internal"]
