"""pg-base/discover through its script, with psql answered by a fake: the instance and database
facts, the rw.discovery.v1 sync it writes, and how permission gaps and failures surface."""

import json

import pytest
from runwhen_capability.contracts import validate

SID = "7704057543498637352"
INSTANCE_ROW = {
    "version": "16.15",
    "versionNum": 160015,
    "inRecovery": False,
    "currentUser": "rw_monitor",
}


def db(name, size=1000, allowed=True, owner="postgres"):
    return {
        "name": name,
        "owner": owner,
        "encoding": "UTF8",
        "sizeAllowed": allowed,
        "sizeBytes": size if allowed else None,
    }


def answer(fake, databases=None, instance=INSTANCE_ROW, sid=SID):
    fake.on("current_setting('server_version')", stdout=json.dumps(instance))
    if sid is None:
        fake.on(
            "FROM pg_control_system()",
            rc=1,
            stderr="ERROR:  permission denied for function pg_control_system",
        )
    else:
        fake.on("FROM pg_control_system()", stdout=json.dumps({"systemIdentifier": sid}))
    fake.on(
        "FROM pg_database d",
        stdout=json.dumps([db("orders"), db("postgres")] if databases is None else databases),
    )


@pytest.fixture(autouse=True)
def libpq_env(monkeypatch):
    monkeypatch.setenv("PGHOST", "orders-db.shop.svc")
    monkeypatch.setenv("PGPORT", "5432")


def test_a_healthy_instance_is_ok_with_instance_databases_and_a_sync(run_script, fake_psql):
    answer(fake_psql)
    run = run_script("discover")
    env = run.envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]
    assert out["instance"] == {
        "host": "orders-db.shop.svc",
        "port": 5432,
        "version": "16.15",
        "versionNum": 160015,
        "inRecovery": False,
        "systemIdentifier": SID,
        "currentUser": "rw_monitor",
    }
    assert out["databases"] == {
        "items": [
            {"name": "orders", "owner": "postgres", "encoding": "UTF8", "sizeBytes": 1000},
            {"name": "postgres", "owner": "postgres", "encoding": "UTF8", "sizeBytes": 1000},
        ],
        "total": 2,
        "omitted": 0,
    }
    assert out["summary"]["counts"] == {"instance": 1, "database": 2}
    assert out["summary"]["root"] == {"canonicalKey": {"name": "systemIdentifier", "value": SID}}
    assert validate("rw.discovery.summary.v1", out["summary"]) == []


def test_the_sync_artifact_is_a_valid_rw_discovery_v1_document(run_script, fake_psql):
    answer(fake_psql)
    sync = json.loads(run_script("discover").files["sync.json"])
    assert validate("rw.discovery.v1", sync) == []
    assert sync["open"] == {
        "source": "pg-base/discover",
        "scopePath": "postgres/instances/orders-db",
        "mode": "full",
    }
    items = sync["batches"][0]["items"]
    assert items[0]["identity"] == {
        "platform": "postgres",
        "chain": [{"type": "instance", "name": "orders-db"}],
    }
    assert items[0]["providerUid"] == SID
    assert items[1]["identity"]["chain"] == [
        {"type": "instance", "name": "orders-db"},
        {"type": "database", "name": "orders"},
    ]
    assert sync["commit"]["partitions"] == [
        {
            "type": "database",
            "parentPath": "postgres/instances/orders-db",
            "status": "complete",
            "count": 2,
        }
    ]
    assert sync["commit"]["summary"]["root"]["canonicalKey"]["value"] == SID


def test_the_instance_is_the_roots_host_and_port_for_its_rootkey(run_script, fake_psql):
    answer(fake_psql)
    sync = json.loads(run_script("discover").files["sync.json"])
    doc = sync["batches"][0]["items"][0]["document"]
    assert (doc["host"], doc["port"]) == (
        "orders-db.shop.svc",
        5432,
    )  # types.yaml rootKey: [host, port]


def test_a_standby_is_reported_as_in_recovery(run_script, fake_psql):
    answer(fake_psql, instance={**INSTANCE_ROW, "inRecovery": True})
    assert run_script("discover").envelope["outputs"]["instance"]["inRecovery"] is True


def test_a_database_the_role_cannot_size_is_a_scoped_error_and_the_run_is_partial(
    run_script, fake_psql
):
    answer(fake_psql, databases=[db("orders", allowed=False), db("postgres")])
    run = run_script("discover")
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [("permission_denied", "orders")]
    items = env["outputs"]["databases"]["items"]
    assert items[0] == {"name": "orders", "owner": "postgres", "encoding": "UTF8"}
    assert items[1]["sizeBytes"] == 1000
    assert env["outputs"]["summary"]["errors"][0]["scope"] == "orders"
    assert validate("rw.discovery.summary.v1", env["outputs"]["summary"]) == []


def test_a_role_that_cannot_read_the_system_identifier_gets_a_scoped_error_and_no_root(
    run_script, fake_psql
):
    answer(fake_psql, sid=None)
    env = run_script("discover").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [
        ("permission_denied", "systemIdentifier")
    ]
    assert env["outputs"]["instance"]["systemIdentifier"] is None
    assert "root" not in env["outputs"]["summary"]
    assert [d["name"] for d in env["outputs"]["databases"]["items"]] == ["orders", "postgres"]


def test_databases_beyond_max_items_are_counted_and_still_synced(run_script, fake_psql):
    many = [db(f"db{i:03d}") for i in range(7)]
    answer(fake_psql, databases=many)
    run = run_script("discover", settings={"maxItems": 3})
    listing = run.envelope["outputs"]["databases"]
    assert (len(listing["items"]), listing["total"], listing["omitted"]) == (3, 7, 4)
    sync = json.loads(run.files["sync.json"])
    assert sum(len(b["items"]) for b in sync["batches"]) == 8  # the instance and all 7 databases


def test_a_thousand_databases_sync_in_batches_of_at_most_500(run_script, fake_psql):
    answer(fake_psql, databases=[db(f"db{i:04d}") for i in range(1000)])
    sync = json.loads(run_script("discover").files["sync.json"])
    assert [len(b["items"]) for b in sync["batches"]] == [500, 500, 1]
    assert [b["seq"] for b in sync["batches"]] == [1, 2, 3]
    assert validate("rw.discovery.v1", sync) == []


def test_an_instance_with_no_user_databases_still_syncs_the_instance(run_script, fake_psql):
    answer(fake_psql, databases=[])
    run = run_script("discover")
    assert run.envelope["outputs"]["databases"] == {"items": [], "total": 0, "omitted": 0}
    assert (
        json.loads(run.files["sync.json"])["batches"][0]["items"][0]["identity"]["chain"][0]["type"]
        == "instance"
    )


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
        ('psql: error: connection to server at "h", port 5432 failed: timeout expired', "timeout"),
        (
            'psql: error: could not translate host name "h" to address: Name or service not known',
            "unreachable",
        ),
    ],
)
def test_a_connection_failure_fails_the_run_with_its_code_and_nothing_else(
    run_script, fake_psql, stderr, code
):
    fake_psql.on("current_setting('server_version')", rc=2, stderr=stderr)
    run = run_script("discover")
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == [code]
    assert run.files == {}  # no half-written sync


def test_a_failure_listing_databases_fails_instead_of_reporting_none(run_script, fake_psql):
    fake_psql.on("current_setting('server_version')", stdout=json.dumps(INSTANCE_ROW))
    fake_psql.on("FROM pg_control_system()", stdout=json.dumps({"systemIdentifier": SID}))
    fake_psql.on(
        "FROM pg_database d", rc=1, stderr="ERROR:  canceling statement due to statement timeout"
    )
    run = run_script("discover")
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.envelope["errors"]] == ["timeout"]
    assert run.files == {}


def test_a_target_that_is_not_an_instance_is_invalid_input(run_script, fake_psql):
    resource = {
        "path": "postgres/instances/orders-db/databases/orders",
        "type": "postgres/database",
        "name": "orders",
        "instance": "orders-db",
    }
    run = run_script("discover", resource=resource)
    assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"]
    assert fake_psql.calls == []


def test_bad_settings_are_invalid_input_before_any_query(run_script, fake_psql):
    for settings in ({"maxItems": 0}, {"statementTimeout": "soon"}, {"connectTimeout": "0s"}):
        run = run_script("discover", settings=settings)
        assert [e["code"] for e in run.envelope["errors"]] == ["invalid_input"], settings
    assert fake_psql.calls == []


def test_pgport_unset_is_the_default_port(run_script, fake_psql, monkeypatch):
    monkeypatch.delenv("PGPORT")
    answer(fake_psql)
    assert run_script("discover").envelope["outputs"]["instance"]["port"] == 5432


def test_the_password_is_never_in_what_the_script_runs_or_returns(
    run_script, fake_psql, monkeypatch
):
    monkeypatch.setenv("PGPASSWORD", "s3cret-test-value")
    answer(fake_psql)
    run = run_script("discover")
    blob = (
        json.dumps(run.envelope)
        + "".join(run.files.values())
        + json.dumps([c["argv"] for c in fake_psql.calls])
    )
    assert "s3cret-test-value" not in blob


def test_the_queries_it_issues_are_read_only_selects(run_script, fake_psql):
    answer(fake_psql)
    run_script("discover")
    assert len(fake_psql.calls) == 3
    for call in fake_psql.calls:
        assert call["sql"].lstrip().upper().startswith("SELECT")


def test_databases_captured_from_a_real_server_for_a_role_that_cannot_size_orders(
    run_script, fake_psql, data
):
    fake_psql.on("current_setting('server_version')", stdout=data("instance_rw_noperm.json"))
    fake_psql.on("FROM pg_control_system()", stdout=json.dumps({"systemIdentifier": SID}))
    fake_psql.on("FROM pg_database d", stdout=data("databases_rw_noperm.json"))
    env = run_script("discover").envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "orders")]
    sizes = {d["name"]: d.get("sizeBytes") for d in env["outputs"]["databases"]["items"]}
    assert sizes["orders"] is None and sizes["postgres"] > 0
    assert "sizeBytes" not in next(
        d for d in env["outputs"]["databases"]["items"] if d["name"] == "orders"
    )


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("psql: error: connection to server failed: timeout expired", "timeout"),
        (
            "psql: error: connection to server at 'h', port 5432 failed: Connection refused",
            "unreachable",
        ),
        ("FATAL:  sorry, too many clients already", "rate_limited"),
        ("ERROR:  something odd", "internal"),
    ],
)
def test_only_permission_denied_is_soft_when_reading_the_system_identifier(
    run_script, fake_psql, stderr, code
):
    fake_psql.on("current_setting('server_version')", stdout=json.dumps(INSTANCE_ROW))
    fake_psql.on("FROM pg_control_system()", rc=1, stderr=stderr)
    fake_psql.on("FROM pg_database d", stdout="[]")
    env = run_script("discover").envelope
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == [code]


@pytest.mark.parametrize("rows", ['["orders"]', '[{"name": "a"}, 1]', '{"name": "a"}', "1", ""])
def test_a_database_list_that_is_not_a_list_of_objects_fails_the_task(run_script, fake_psql, rows):
    fake_psql.on("current_setting('server_version')", stdout=json.dumps(INSTANCE_ROW))
    fake_psql.on("FROM pg_control_system()", stdout=json.dumps({"systemIdentifier": SID}))
    fake_psql.on("FROM pg_database d", stdout=rows)
    env = run_script("discover").envelope
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == ["internal"]


@pytest.mark.parametrize("answer_text", ['["x"]', '"x"', "1"])
def test_a_system_identifier_row_that_is_not_an_object_fails_the_task(
    run_script, fake_psql, answer_text
):
    fake_psql.on("current_setting('server_version')", stdout=json.dumps(INSTANCE_ROW))
    fake_psql.on("FROM pg_control_system()", stdout=answer_text)
    fake_psql.on("FROM pg_database d", stdout="[]")
    env = run_script("discover").envelope
    assert env["status"] == "failed"
    assert [e["code"] for e in env["errors"]] == ["internal"]


def test_unknown_values_are_left_out_of_the_sync_and_the_output_not_sent_as_null(
    run_script, fake_psql
):
    answer(fake_psql, databases=[db("orders", allowed=False), db("postgres")], sid=None)
    run = run_script("discover")
    sync = json.loads(run.files["sync.json"])
    root, orders, postgres = sync["batches"][0]["items"]
    assert "providerUid" not in root
    assert "systemIdentifier" not in root["document"]
    assert "sizeBytes" not in orders["document"]
    assert postgres["document"]["sizeBytes"] == 1000
    assert not any(v is None for v in orders["document"].values())
    assert "sizeBytes" not in run.envelope["outputs"]["databases"]["items"][0]
