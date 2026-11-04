"""aws-base/discover through its script, with aws answered by a fake and by the CLI stub: the
account, region and function facts, the rw.discovery.v1 sync it writes, and how a region the
credential cannot read, a failing call and a truncated list surface."""

import json

import pytest
import yaml
from aws_base import discovery as dc
from conftest import ACCOUNT, FUNCTION, PLUGIN, iam_action
from runwhen_capability.contracts import validate

ACCT = "123456789012"
IDENTITY = {"UserId": "AIDAEXAMPLE", "Account": ACCT, "Arn": f"arn:aws:iam::{ACCT}:user/rw-monitor"}
DENIED = (
    "\naws: [ERROR]: An error occurred (AccessDeniedException) when calling the ListFunctions "
    "operation: User: arn:aws:iam::123456789012:user/rw-monitor is not authorized to perform: "
    "lambda:ListFunctions on resource: * with an explicit deny in a service control policy\n"
)
THROTTLED = (
    "\naws: [ERROR]: An error occurred (TooManyRequestsException) when calling the ListFunctions "
    "operation (reached max retries: 2): Rate exceeded\n"
)


def region_rows(names):
    return {"Regions": [{"RegionName": n, "OptInStatus": "opt-in-not-required"} for n in names]}


def fn(name, region="eu-west-1", **kw):
    return {
        "FunctionName": name,
        "FunctionArn": f"arn:aws:lambda:{region}:{ACCT}:function:{name}",
        "Runtime": "python3.12",
        "PackageType": "Zip",
        "MemorySize": 256,
        "Timeout": 30,
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "LastModified": "2026-10-28T09:14:03.000+0000",
        **kw,
    }


def listing(*names, region="eu-west-1", token=None):
    return {"Functions": [fn(n, region) for n in names], "NextToken": token}


def answer(fake, by_region=None, regions=("eu-west-1", "us-east-1")):
    fake.on("sts", "get-caller-identity", body=IDENTITY)
    fake.on("ec2", "describe-regions", body=region_rows(regions))
    by_region = {"eu-west-1": listing("orders-api", "report-batch"), **(by_region or {})}
    for region in regions:
        fake.on("lambda", "list-functions", region=region, body=by_region.get(region, listing()))


@pytest.fixture(autouse=True)
def home(monkeypatch):
    # AWS_REGION is not part of the credential (it is not materialized) and is never read
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)


def discover(run_script, **kw):
    return run_script("aws-base", "discover", resource=ACCOUNT, **kw)


def test_a_healthy_account_is_ok_with_account_regions_and_a_sync(run_script, fake_aws):
    answer(fake_aws)
    run = discover(run_script)
    env = run.envelope
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]
    assert out["account"] == {
        "accountId": ACCT,
        "callerArn": IDENTITY["Arn"],
        "homeRegion": "us-east-1",
        "regionsRead": 2,
        "regionsComplete": True,
    }
    assert out["regions"] == {
        "items": [
            {"name": "eu-west-1", "status": "ok", "functions": 2},
            {"name": "us-east-1", "status": "ok", "functions": 0},
        ],
        "total": 2,
        "omitted": 0,
    }
    assert out["summary"]["counts"] == {"account": 1, "region": 2, "lambda-function": 2}
    assert out["summary"]["root"] == {"canonicalKey": {"name": "accountId", "value": ACCT}}
    assert validate("rw.discovery.summary.v1", out["summary"]) == []


def test_the_sync_artifact_is_a_valid_rw_discovery_v1_document(run_script, fake_aws):
    answer(fake_aws)
    sync = json.loads(discover(run_script).files["sync.json"])
    assert validate("rw.discovery.v1", sync) == []
    assert sync["open"] == {
        "source": "aws-base/discover",
        "scopePath": "aws/accounts/123456789012",
        "mode": "full",
    }
    items = [i for b in sync["batches"] for i in b["items"]]
    chains = [[(c["type"], c["name"]) for c in i["identity"]["chain"]] for i in items]
    assert chains == [
        [("account", "123456789012")],
        [("account", "123456789012"), ("region", "eu-west-1")],
        [("account", "123456789012"), ("region", "eu-west-1"), ("lambda-function", "orders-api")],
        [("account", "123456789012"), ("region", "eu-west-1"), ("lambda-function", "report-batch")],
        [("account", "123456789012"), ("region", "us-east-1")],
    ]  # us-east-1 has no function and was read fine: still a region item
    assert items[0]["providerUid"] == ACCT
    assert items[2]["providerUid"] == f"arn:aws:lambda:eu-west-1:{ACCT}:function:orders-api"
    assert (
        items[2]["document"]["runtime"] == "python3.12" and items[2]["status"]["state"] == "Active"
    )
    # every region that was read is a region item with its own function partition, count 0 included
    assert sync["commit"]["partitions"] == [
        {
            "type": "region",
            "parentPath": "aws/accounts/123456789012",
            "status": "complete",
            "count": 2,
        },
        {
            "type": "lambda-function",
            "parentPath": "aws/accounts/123456789012/regions/eu-west-1",
            "status": "complete",
            "count": 2,
        },
        {
            "type": "lambda-function",
            "parentPath": "aws/accounts/123456789012/regions/us-east-1",
            "status": "complete",
            "count": 0,
        },
    ]
    assert sync["commit"]["summary"]["root"]["canonicalKey"]["value"] == ACCT


def test_no_environment_value_or_credential_reaches_the_sync_or_the_result(run_script, fake_aws):
    # the real list-functions response holds Environment.Variables; the projection never asks for it
    body = listing("orders-api")
    body["Functions"][0]["Environment"] = {"Variables": {"DB_PASSWORD": "hunter2"}}
    answer(fake_aws, {"eu-west-1": body})
    run = discover(run_script)
    shown = run.files["sync.json"] + json.dumps(run.envelope)
    assert "hunter2" not in shown and "Environment" not in shown
    query = next(c for c in fake_aws.calls if c["words"][:2] == ["lambda", "list-functions"])[
        "words"
    ]
    assert "Environment" not in query[query.index("--query") + 1]


def test_every_list_call_asks_for_a_page_and_selects_next_token(run_script, fake_aws):
    answer(fake_aws)
    discover(run_script)
    lists = [c["words"] for c in fake_aws.calls if c["words"][:2] == ["lambda", "list-functions"]]
    assert len(lists) == 2
    for words in lists:
        assert words[words.index("--max-items") + 1] == "1000"
        assert "NextToken: NextToken" in words[words.index("--query") + 1]


def test_a_region_the_credential_cannot_list_is_forbidden_and_the_others_stand(
    run_script, fake_aws
):
    fake_aws.on("lambda", "list-functions", region="ap-south-1", rc=254, stderr=DENIED)
    answer(fake_aws, regions=("ap-south-1", "eu-west-1", "us-east-1"))
    # the first matching handler wins: the denied one was registered first
    run = discover(run_script)
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [
        ("permission_denied", "regions")
    ]
    assert (
        "ap-south-1" in env["errors"][0]["message"]
        and "regions setting" in env["errors"][0]["message"]
    )
    assert env["outputs"]["regions"]["items"][0] == {
        "name": "ap-south-1",
        "status": "forbidden",
        "functions": None,
    }
    summary = env["outputs"]["summary"]
    assert summary["partitions"] == {"complete": 3, "failed": 0, "forbidden": 1, "excluded": 0}
    assert validate("rw.discovery.summary.v1", summary) == []
    sync = json.loads(run.files["sync.json"])
    assert validate("rw.discovery.v1", sync) == []
    parts = {
        p["parentPath"].rsplit("/", 1)[-1]: p
        for p in sync["commit"]["partitions"]
        if p["type"] == "lambda-function"
    }
    assert (
        parts["ap-south-1"]["status"] == "forbidden"
        and parts["ap-south-1"]["reason"] == "permission_denied"
    )
    # the forbidden region keeps its item, so a complete sweep of regions does not delete it
    region_items = [
        i
        for b in sync["batches"]
        for i in b["items"]
        if i["identity"]["chain"][-1]["type"] == "region"
    ]
    assert [i["identity"]["chain"][-1]["name"] for i in region_items] == [
        "ap-south-1", "eu-west-1", "us-east-1",
    ]  # fmt: skip
    assert region_items[0]["status"] == {"discovery": "forbidden"}


def test_a_throttled_region_is_failed_not_empty(run_script, fake_aws):
    fake_aws.on("lambda", "list-functions", region="eu-west-1", rc=254, stderr=THROTTLED)
    answer(fake_aws)
    run = discover(run_script)
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e.get("scope")) for e in env["errors"]] == [("rate_limited", "eu-west-1")]
    assert env["outputs"]["regions"]["items"][0] == {
        "name": "eu-west-1",
        "status": "failed",
        "functions": None,
    }
    sync = json.loads(run.files["sync.json"])
    part = next(p for p in sync["commit"]["partitions"] if p["type"] == "lambda-function")
    assert part["status"] == "failed" and part["reason"] == "rate_limited" and part["count"] == 0


def test_a_second_page_is_followed_and_every_function_is_synced(run_script, fake_aws):
    token = "eyJNYXJrZXIiOiBudWxsLCAiYm90b190cnVuY2F0ZV9hbW91bnQiOiAxMDAwfQ=="
    first = listing("orders-api", token=token)
    second = listing("report-batch", "thumbnailer")
    fake_aws.on("lambda", "list-functions", "--query", dc.FUNCTIONS_QUERY, "--max-items", "1000",
                "--starting-token", token, region="eu-west-1", body=second)  # fmt: skip
    answer(fake_aws, {"eu-west-1": first})
    run = discover(run_script)
    assert run.envelope["status"] == "ok"
    assert run.envelope["outputs"]["summary"]["counts"]["lambda-function"] == 3
    calls = [c["words"] for c in fake_aws.calls if c["region"] == "eu-west-1"]
    assert calls[1][-2:] == ["--starting-token", token]


def test_a_list_cut_by_the_page_limit_is_failed_never_complete(run_script, fake_aws, monkeypatch):
    from aws_base import awscli

    monkeypatch.setattr(awscli, "MAX_PAGES", 3)
    # every page says there is more
    fake_aws.on("lambda", "list-functions", region="eu-west-1", body=listing("f", token="more"))
    answer(fake_aws)
    run = discover(run_script)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.envelope["errors"]] == [("internal", "eu-west-1")]
    part = next(
        p
        for p in json.loads(run.files["sync.json"])["commit"]["partitions"]
        if p["type"] == "lambda-function"
    )
    assert part["status"] == "failed"


def test_a_credential_sts_rejects_fails_the_run_before_any_other_call(run_script, fake_aws):
    stderr = "\naws: [ERROR]: An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired\n"
    fake_aws.on("sts", "get-caller-identity", rc=254, stderr=stderr)
    env = discover(run_script).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["permission_denied"]
    assert env["outputs"] == {}
    assert fake_aws.words() == [["sts", "get-caller-identity"]]


def test_a_forbidden_region_list_fails_the_run_with_nothing_to_report(run_script, fake_aws):
    stderr = "\naws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the DescribeRegions operation: You are not authorized to perform this operation.\n"
    fake_aws.on("sts", "get-caller-identity", body=IDENTITY)
    fake_aws.on("ec2", "describe-regions", rc=254, stderr=stderr)
    env = discover(run_script).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["permission_denied"]
    assert "UnauthorizedOperation" in env["errors"][0]["message"]


def test_the_regions_setting_skips_describe_regions_and_stops_the_sweep_outside_it(
    run_script, fake_aws
):
    answer(fake_aws)
    run = discover(run_script, settings={"regions": ["eu-west-1", "eu-west-1"]})
    assert ["ec2", "describe-regions"] not in [w[:2] for w in fake_aws.words()]
    assert [c["region"] for c in fake_aws.calls if c["words"][1] == "list-functions"] == [
        "eu-west-1"
    ]
    out = run.envelope["outputs"]
    assert out["account"]["regionsComplete"] is False and out["account"]["regionsRead"] == 1
    sync = json.loads(run.files["sync.json"])
    assert sync["commit"]["partitions"][0]["status"] == "excluded"
    assert out["summary"]["partitions"]["excluded"] == 1


@pytest.mark.parametrize("bad", ["us_east_1", "EU-WEST-1", "eu-west", "eu-west-1;rm", "x" * 70])
def test_a_bad_region_in_the_setting_is_invalid_input_before_any_call(run_script, fake_aws, bad):
    env = discover(run_script, settings={"regions": [bad]}).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


def test_the_account_level_calls_use_the_default_region_setting_not_the_environment(
    run_script, fake_aws
):
    answer(fake_aws)
    run = discover(run_script)
    assert run.envelope["outputs"]["account"]["homeRegion"] == "us-east-1"
    assert {c["region"] for c in fake_aws.calls if c["words"][0] in ("sts", "ec2")} == {"us-east-1"}
    fake_aws.calls.clear()
    fake_aws.handlers.clear()
    answer(fake_aws)
    run = discover(run_script, settings={"defaultRegion": "eusc-de-east-1"})
    assert run.envelope["outputs"]["account"]["homeRegion"] == "eusc-de-east-1"
    assert {c["region"] for c in fake_aws.calls if c["words"][0] in ("sts", "ec2")} == {
        "eusc-de-east-1"
    }


@pytest.mark.parametrize("bad", ["us_east_1", "", "US-EAST-1", "us-east-1; rm"])
def test_a_bad_default_region_is_invalid_input_before_any_call(run_script, fake_aws, bad):
    env = discover(run_script, settings={"defaultRegion": bad}).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


def test_regions_beyond_max_items_are_counted_and_still_synced(run_script, fake_aws):
    names = [f"r{i}" for i in range(5)]
    regions = ("ap-south-1", "ap-south-2", "eu-west-1", "eu-west-2", "us-east-1")
    answer(fake_aws, {r: listing("f", region=r) for r in regions}, regions=regions)
    run = discover(run_script, settings={"maxItems": 2})
    listing_out = run.envelope["outputs"]["regions"]
    assert (len(listing_out["items"]), listing_out["total"], listing_out["omitted"]) == (2, 5, 3)
    sync = json.loads(run.files["sync.json"])
    assert sum(len(b["items"]) for b in sync["batches"]) == 1 + 5 + 5
    assert names  # five regions, one function each


def test_over_500_items_go_in_ordered_batches_of_at_most_500(run_script, fake_aws):
    token = "next"
    first = {"Functions": [fn(f"f{i:04d}") for i in range(1000)], "NextToken": token}
    second = {"Functions": [fn(f"g{i:04d}") for i in range(300)], "NextToken": None}
    fake_aws.on("lambda", "list-functions", "--query", dc.FUNCTIONS_QUERY, "--max-items", "1000",
                "--starting-token", token, region="eu-west-1", body=second)  # fmt: skip
    answer(fake_aws, {"eu-west-1": first})
    sync = json.loads(discover(run_script).files["sync.json"])
    assert [len(b["items"]) for b in sync["batches"]] == [500, 500, 303]
    assert [b["seq"] for b in sync["batches"]] == [1, 2, 3]
    assert validate("rw.discovery.v1", sync) == []


def test_an_account_with_no_functions_syncs_its_regions_and_sweeps_what_was_there(
    run_script, fake_aws
):
    answer(fake_aws, {"eu-west-1": listing()})
    run = discover(run_script)
    assert run.envelope["status"] == "ok"
    sync = json.loads(run.files["sync.json"])
    assert [len(b["items"]) for b in sync["batches"]] == [3]  # the account and its two regions
    assert sync["commit"]["partitions"] == [
        {
            "type": "region",
            "parentPath": "aws/accounts/123456789012",
            "status": "complete",
            "count": 2,
        },
        {
            "type": "lambda-function",
            "parentPath": "aws/accounts/123456789012/regions/eu-west-1",
            "status": "complete",
            "count": 0,
        },
        {
            "type": "lambda-function",
            "parentPath": "aws/accounts/123456789012/regions/us-east-1",
            "status": "complete",
            "count": 0,
        },
    ]  # a complete partition of count 0: functions an earlier run found there are swept


def test_a_failed_or_forbidden_region_keeps_a_partition_that_sweeps_nothing(run_script, fake_aws):
    fake_aws.on("lambda", "list-functions", region="ap-south-1", rc=254, stderr=DENIED)
    fake_aws.on("lambda", "list-functions", region="eu-west-1", rc=254, stderr=THROTTLED)
    answer(fake_aws, regions=("ap-south-1", "eu-west-1", "us-east-1"))
    sync = json.loads(discover(run_script).files["sync.json"])
    status = {
        p["parentPath"].rsplit("/", 1)[-1]: p["status"]
        for p in sync["commit"]["partitions"]
        if p["type"] == "lambda-function"
    }
    assert status == {"ap-south-1": "forbidden", "eu-west-1": "failed", "us-east-1": "complete"}


@pytest.mark.parametrize(
    "resource", [FUNCTION, {"type": "aws/region", "name": "x", "path": "p"}, None]
)
def test_discover_refuses_a_target_that_is_not_an_account(run_script, fake_aws, resource):
    env = run_script("aws-base", "discover", resource=resource).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["invalid_input"]
    assert fake_aws.calls == []


def test_a_malformed_identity_is_internal_not_an_empty_account(run_script, fake_aws):
    fake_aws.on("sts", "get-caller-identity", body={"UserId": "x"})
    env = discover(run_script).envelope
    assert env["status"] == "failed" and [e["code"] for e in env["errors"]] == ["internal"]


# --- against the CLI stub, as `rwtask test --record` runs it ---------------------------------------


def test_discover_against_the_stub_healthy(run_with_stub):
    run_with_stub.use("healthy")
    run = run_with_stub("aws-base", "discover", resource=ACCOUNT)
    assert run.envelope["status"] == "ok", run.envelope["errors"]
    assert run.envelope["outputs"]["summary"]["counts"] == {
        "account": 1,
        "region": 3,
        "lambda-function": 2,
    }


def test_discover_against_the_stub_partial_paged_throttled_forbidden_expired(run_with_stub):
    expected = {
        "partial": ("partial", ["permission_denied"]),
        "throttled": ("partial", ["rate_limited"]),
        "paged": ("ok", []),
        "forbidden": ("failed", ["permission_denied"]),
        "expired": ("failed", ["permission_denied"]),
    }
    for scenario, (status, codes) in expected.items():
        run_with_stub.use(scenario)
        run = run_with_stub("aws-base", "discover", resource=ACCOUNT)
        assert run.envelope["status"] == status, (scenario, run.envelope["errors"])
        assert [e["code"] for e in run.envelope["errors"]] == codes, scenario
    run_with_stub.use("paged")
    run = run_with_stub("aws-base", "discover", resource=ACCOUNT)
    assert run.envelope["outputs"]["summary"]["counts"]["lambda-function"] == 3


# --- what the task declares ------------------------------------------------------------------------


def tasks_doc():
    return yaml.safe_load((PLUGIN / "skills" / "aws-base" / "tasks.yaml").read_text())


def test_discover_declares_exactly_the_iam_actions_its_calls_need(run_script, fake_aws):
    answer(fake_aws)
    discover(run_script)
    needed = {iam_action(c["words"]) for c in fake_aws.calls} - {None}
    task = next(t for t in tasks_doc()["tasks"] if t["name"] == "discover")
    # ec2:DescribeRegions is needed only while the regions setting is empty: see
    # test_describe_regions_is_needed_only_without_the_regions_setting
    assert {p["action"] for p in task["permissions"]} == needed - {"ec2:DescribeRegions"}
    assert needed == {"ec2:DescribeRegions", "lambda:ListFunctions"}


def test_the_entry_points_name_tasks_that_exist():
    integration = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    names = {t["name"] for t in tasks_doc()["tasks"]}
    for entry in integration["entryPoints"].values():
        skill, _, name = entry["task"].partition("/")
        assert skill == "aws-base" and name in names


def test_the_root_declares_the_key_discover_reports():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["account"]["canonicalKey"] == dc.CANONICAL_KEY
    assert types["region"]["parent"] == "account" and types["lambda-function"]["parent"] == "region"
    reported = {"account", "region", "lambda-function"}
    assert set(types) == reported


def test_discover_declares_the_sync_credential_beside_the_connection():
    task = next(t for t in tasks_doc()["tasks"] if t["name"] == "discover")
    assert task["credentials"] == ["aws/sdk", "runwhen/sync"] and task["readOnly"] is True
    assert tasks_doc()["runtime"] == "aws@1"
    assert tasks_doc()["defaults"]["credentials"] == "aws/sdk"


def test_every_declared_permission_fits_the_permission_schema():
    import jsonschema

    schema = json.loads((PLUGIN / "integration" / "schemas" / "aws-permission.json").read_text())
    for task in tasks_doc()["tasks"]:
        for permission in task["permissions"]:
            jsonschema.validate(permission, schema)


def test_output_schemas_are_valid_2020_12_and_name_no_verdict():
    import jsonschema

    for task in tasks_doc()["tasks"]:
        for out_name, out in task["outputs"].items():
            assert out_name not in {"status", "errors"}
            if "schema" in out:
                schema = json.loads((PLUGIN / "skills" / "aws-base" / out["schema"]).read_text())
                jsonschema.Draft202012Validator.check_schema(schema)
                assert not {"severity", "title", "nextSteps"} & set(schema["properties"])


def test_the_account_and_region_outputs_fit_their_schemas(run_script, fake_aws):
    import jsonschema

    answer(fake_aws, {"eu-west-1": listing("a")})
    out = discover(run_script).envelope["outputs"]
    for name in ("account", "regions"):
        schema = json.loads(
            (PLUGIN / "skills" / "aws-base" / "schemas" / f"{name}.v1.json").read_text()
        )
        jsonschema.validate(out[name], schema)


# --- the credential is the target's, M5 --------------------------------------------------------------


def test_a_credential_for_another_account_than_the_target_fails_before_any_region_is_read(
    run_script, fake_aws
):
    other = {"path": "aws/accounts/210987654321", "type": "aws/account", "name": "210987654321"}
    answer(fake_aws)
    env = run_script("aws-base", "discover", resource=other).envelope
    assert env["status"] == "failed" and env["outputs"] == {}
    assert [e["code"] for e in env["errors"]] == ["permission_denied"]
    assert (
        env["errors"][0]["message"]
        == "credential is for account 123456789012, target is 210987654321"
    )
    assert fake_aws.words() == [["sts", "get-caller-identity"]]  # no region, no sync


def test_a_target_named_by_an_alias_cannot_be_compared_and_is_let_through(run_script, fake_aws):
    alias = {"path": "aws/accounts/prod", "type": "aws/account", "name": "prod"}
    answer(fake_aws)
    env = run_script("aws-base", "discover", resource=alias).envelope
    assert env["status"] == "ok"


# --- the read deadline, many denied regions, ec2:DescribeRegions only when needed -------------------


def test_the_region_loop_has_a_deadline_and_the_unread_regions_are_failed_not_empty(
    run_script, fake_aws, monkeypatch
):
    monkeypatch.setattr(dc, "READ_BUDGET", 0)
    answer(fake_aws)
    run = discover(run_script)
    env = run.envelope
    assert env["status"] == "partial"
    assert {(e["code"], e["scope"]) for e in env["errors"]} == {
        ("timeout", "eu-west-1"), ("timeout", "us-east-1"),
    }  # fmt: skip
    assert [w for w in fake_aws.words() if w[1] == "list-functions"] == []
    assert {r["status"] for r in env["outputs"]["regions"]["items"]} == {"failed"}


def test_each_region_call_gets_at_most_what_is_left_of_the_read_budget(
    run_script, fake_aws, monkeypatch
):
    monkeypatch.setattr(dc, "READ_BUDGET", 20)
    answer(fake_aws)
    discover(run_script)
    timeouts = [c["timeout"] for c in fake_aws.calls if c["words"][1] == "list-functions"]
    assert timeouts and all(0 < t <= 20 for t in timeouts)


def test_many_denied_regions_are_one_error_that_lists_them(run_script, fake_aws):
    names = ("ap-south-1", "ap-southeast-1", "eu-north-1", "eu-west-1", "us-east-1")
    for region in names[:4]:
        fake_aws.on("lambda", "list-functions", region=region, rc=254, stderr=DENIED)
    answer(fake_aws, regions=names)
    env = discover(run_script).envelope
    assert env["status"] == "partial" and len(env["errors"]) == 1
    error = env["errors"][0]
    assert (error["code"], error["scope"]) == ("permission_denied", "regions")
    assert (
        all(name in error["message"] for name in names[:4]) and "us-east-1" not in error["message"]
    )
    assert "4 region(s)" in error["message"]
    assert env["outputs"]["summary"]["partitions"]["forbidden"] == 4
    assert [e["scope"] for e in env["outputs"]["summary"]["errors"]] == ["regions"]


def test_describe_regions_is_needed_only_without_the_regions_setting(run_script, fake_aws):
    task = next(t for t in tasks_doc()["tasks"] if t["name"] == "discover")
    declared = {p["action"] for p in task["permissions"]}
    answer(fake_aws)
    discover(run_script)
    all_regions = {iam_action(c["words"]) for c in fake_aws.calls} - {None}
    fake_aws.calls.clear()
    discover(run_script, settings={"regions": ["eu-west-1"]})
    narrowed = {iam_action(c["words"]) for c in fake_aws.calls} - {None}
    # a static list cannot say "when": ec2:DescribeRegions is a conditional note, not declared
    assert all_regions - narrowed == {"ec2:DescribeRegions"}
    assert declared == narrowed == {"lambda:ListFunctions"}
    assert "ec2:DescribeRegions" in task["description"] or "ec2:DescribeRegions" in json.dumps(
        tasks_doc()["settings"]["regions"]
    )
