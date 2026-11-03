"""psql_json: the argv it issues, the environment it sets, and how a failed psql is classified."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from pg_base import pg
from pg_base.errors import RwError

# stderr as psql 17/18 prints it, per failure
CASES = [
    (
        'psql: error: connection to server at "10.0.0.5", port 5432 failed: FATAL:  password authentication failed for user "app"',
        "permission_denied",
    ),
    (
        'psql: error: connection to server at "10.0.0.5", port 5432 failed: FATAL:  no pg_hba.conf entry for host "10.0.0.9", user "app", database "orders", SSL encryption',
        "permission_denied",
    ),
    (
        'psql: error: connection to server at "127.0.0.1", port 55432 failed: FATAL:  role "ghost" does not exist',
        "permission_denied",
    ),
    (
        'psql: error: connection to server at "127.0.0.1", port 55432 failed: FATAL:  permission denied for database "orders"\nDETAIL:  User does not have CONNECT privilege.',
        "permission_denied",
    ),
    ("ERROR:  permission denied for function pg_ls_dir", "permission_denied"),
    (
        "ERROR:  must be superuser or have privileges of pg_read_all_stats to use this",
        "permission_denied",
    ),
    (
        'psql: error: connection to server at "127.0.0.1", port 55432 failed: FATAL:  database "nope" does not exist',
        "not_found",
    ),
    (
        'psql: error: connection to server at "127.0.0.1", port 55439 failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?',
        "unreachable",
    ),
    (
        'psql: error: could not translate host name "db.invalid" to address: Name or service not known',
        "unreachable",
    ),
    (
        'psql: error: connection to server at "10.0.0.5", port 5432 failed: SSL error: certificate verify failed',
        "unreachable",
    ),
    (
        'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory',
        "unreachable",
    ),
    (
        'psql: error: connection to server at "10.0.0.5", port 5432 failed: FATAL:  the database system is starting up',
        "unreachable",
    ),
    (
        'psql: error: connection to server at "10.0.0.5", port 5432 failed: timeout expired',
        "timeout",
    ),
    ("ERROR:  canceling statement due to statement timeout", "timeout"),
    ('ERROR:  syntax error at or near "selec"', "internal"),
    ('ERROR:  relation "pg_stat_statements" does not exist', "internal"),
    ("", "internal"),
]


@pytest.mark.parametrize("stderr,code", CASES)
def test_stderr_is_classified_into_a_d64_code(stderr, code):
    assert pg.classify_error(stderr, 2) == code


def test_exit_127_is_dependency_whatever_stderr_says():
    assert pg.classify_error("sh: psql: not found", 127) == "dependency"
    assert pg.classify_error("password authentication failed", 127) == "dependency"


def test_exit_124_is_timeout_when_stderr_says_nothing_else():
    assert pg.classify_error("", 124) == "timeout"


def test_a_connect_timeout_is_a_timeout_not_unreachable():
    text = 'psql: error: connection to server at "10.0.0.5", port 5432 failed: timeout expired'
    assert pg.classify_error(text, 2) == "timeout"


def test_an_authentication_failure_is_not_unreachable_though_it_says_connection_failed():
    text = 'connection to server at "h", port 5432 failed: FATAL:  password authentication failed'
    assert pg.classify_error(text, 2) == "permission_denied"


@pytest.mark.parametrize(
    "value,seconds", [("30s", 30), ("10s", 10), ("500ms", 0.5), ("1m30s", 90), ("2h", 7200)]
)
def test_durations_parse(value, seconds):
    assert pg.parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "abc", "10", "0s", "-5s", "1 s", "5x", None, 10])
def test_a_bad_duration_is_invalid_input(value):
    with pytest.raises(RwError) as err:
        pg.parse_duration(value, "statementTimeout")
    assert err.value.code == "invalid_input"
    assert "statementTimeout" in err.value.message


@pytest.mark.parametrize(
    "name,arg",
    [
        ("orders", "orders"),
        ("my-db.v2", "my-db.v2"),
        ("a=b", "dbname='a=b'"),
        ("postgresql://u@h/x", "dbname='postgresql://u@h/x'"),
        ("it's", "dbname='it\\'s'"),
        ("with space", "dbname='with space'"),
        ("-flag", "dbname='-flag'"),
    ],
)
def test_a_database_name_that_psql_could_misread_is_passed_as_a_connection_string(name, arg):
    assert pg.dbname_arg(name) == arg


@pytest.mark.parametrize("name", ["", "a\x00b"])
def test_an_empty_or_nul_database_name_is_invalid_input(name):
    with pytest.raises(RwError) as err:
        pg.dbname_arg(name)
    assert err.value.code == "invalid_input"


@pytest.mark.parametrize(
    "text", ["plain", "it's", "$q0$", "x$", "$", "$q0", "a$q0$b", "back\\slash"]
)
def test_dollar_quote_round_trips_whatever_the_text_holds(text):
    quoted = pg.dollar_quote(text)
    tag = quoted[: quoted.index("$", 1) + 1]
    assert quoted.startswith(tag) and quoted.endswith(tag)
    body = quoted[len(tag) : -len(tag)]
    assert body == text
    assert tag not in body


def test_dollar_quote_refuses_nul():
    with pytest.raises(RwError) as err:
        pg.dollar_quote("a\x00b")
    assert err.value.code == "invalid_input"


def ctx_with(fake, **settings):
    values = {"connect_timeout": "10s", "statement_timeout": "30s", **settings}
    return SimpleNamespace(run=fake, settings=SimpleNamespace(**values))


def test_the_argv_is_the_read_only_one_shot_psql_with_a_statement_timeout(fake_psql):
    fake_psql.on("SELECT 1", stdout='{"a": 1}\n')
    out = pg.psql_json(ctx_with(fake_psql), "SELECT 1")
    assert out == {"a": 1}
    call = fake_psql.calls[0]
    assert call["argv"] == [
        "psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
        "-c", "SET statement_timeout = 30000; SET default_transaction_read_only = on",
        "-c", "SELECT 1",
    ]  # fmt: skip
    assert call["database"] is None
    assert call["env"] == {"PGCONNECT_TIMEOUT": "10"}
    assert call["timeout"] == pytest.approx(10 + 30 + 5)


def test_database_selects_dash_d_and_the_settings_set_the_timeouts(fake_psql):
    fake_psql.on("SELECT 1", stdout="[]")
    pg.psql_json(
        ctx_with(fake_psql, connect_timeout="3s", statement_timeout="1m"),
        "SELECT 1",
        database="orders",
    )
    call = fake_psql.calls[0]
    assert call["database"] == "orders"
    assert (
        call["argv"][-3] == "SET statement_timeout = 60000; SET default_transaction_read_only = on"
    )
    assert call["env"] == {"PGCONNECT_TIMEOUT": "3"}


def test_settings_default_when_the_skill_declares_none(fake_psql):
    fake_psql.on("SELECT 1", stdout="1")
    pg.psql_json(SimpleNamespace(run=fake_psql, settings=SimpleNamespace()), "SELECT 1")
    assert fake_psql.calls[0]["env"] == {"PGCONNECT_TIMEOUT": "10"}


def test_multiline_json_is_parsed(fake_psql):
    fake_psql.on("x", stdout=json.dumps([{"a": 1}, {"a": 2}], indent=1) + "\n")
    assert pg.psql_json(ctx_with(fake_psql), "x") == [{"a": 1}, {"a": 2}]


def test_an_empty_result_is_none_not_an_empty_list(fake_psql):
    fake_psql.on("x", stdout="\n")
    assert pg.psql_json(ctx_with(fake_psql), "x") is None


def test_output_that_is_not_json_is_internal(fake_psql):
    fake_psql.on("x", stdout="not json")
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(fake_psql), "x")
    assert err.value.code == "internal"


@pytest.mark.parametrize("stderr,code", [c for c in CASES if c[1] != "internal"])
def test_a_failed_psql_raises_with_the_classified_code(fake_psql, stderr, code):
    fake_psql.on("x", rc=2, stderr=stderr)
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(fake_psql), "x")
    assert err.value.code == code
    assert err.value.message


def test_a_failure_never_comes_back_as_an_empty_result(fake_psql):
    fake_psql.on("x", rc=1, stdout="", stderr="ERROR:  boom")
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(fake_psql), "x")
    assert err.value.code == "internal"
    assert "boom" in err.value.message


def test_no_psql_is_dependency(fake_psql):
    fake_psql.on("x", rc=127, stderr="psql: command not found")
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(fake_psql), "x")
    assert err.value.code == "dependency"


def test_a_missing_executable_is_dependency():
    def run(*a, **k):
        raise FileNotFoundError("psql")

    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(run), "x")
    assert err.value.code == "dependency"


def test_a_hung_process_is_timeout():
    def run(argv, **k):
        raise subprocess.TimeoutExpired(argv, 45)

    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(run), "x")
    assert err.value.code == "timeout"


def test_the_message_is_one_bounded_line(fake_psql):
    fake_psql.on("x", rc=2, stderr="ERROR:  " + "word " * 600)
    with pytest.raises(RwError) as err:
        pg.psql_json(ctx_with(fake_psql), "x")
    assert len(err.value.message) <= pg.MESSAGE_LIMIT
    assert "\n" not in err.value.message


def test_the_module_never_touches_the_password():
    source = open(pg.__file__).read()
    assert "PGPASSWORD" not in source.replace("never reads or prints PGPASSWORD", "")
