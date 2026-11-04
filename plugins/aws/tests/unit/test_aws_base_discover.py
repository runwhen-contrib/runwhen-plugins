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


def inst(instance_id, region="eu-west-1", **kw):
    return {
        "InstanceId": instance_id,
        "InstanceType": "t3.small",
        "State": "running",
        "LaunchTime": "2026-10-28T09:14:03+00:00",
        "AvailabilityZone": f"{region}a",
        "Name": None,
        **kw,
    }


def instances(*ids, region="eu-west-1", token=None):
    return {"Instances": [inst(i, region) for i in ids], "NextToken": token}


def answer(fake, by_region=None, regions=("eu-west-1", "us-east-1"), instances_by_region=None):
    fake.on("sts", "get-caller-identity", body=IDENTITY)
    fake.on("ec2", "describe-regions", body=region_rows(regions))
    by_region = {"eu-west-1": listing("orders-api", "report-batch"), **(by_region or {})}
    by_instance = {"eu-west-1": instances("i-0123456789abcdef0"), **(instances_by_region or {})}
    for region in regions:
        fake.on("lambda", "list-functions", region=region, body=by_region.get(region, listing()))
        fake.on(
            "ec2", "describe-instances", region=region,
            body=by_instance.get(region, instances()),
        )  # fmt: skip


@pytest.fixture(autouse=True)
def home(monkeypatch):
    # AWS_REGION is not part of the credential (it is not materialized) and is never read
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)


# the two calls with no region of their own; describe-instances is regional like list-functions
ACCOUNT_LEVEL = (["sts", "get-caller-identity"], ["ec2", "describe-regions"])


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
            {"name": "eu-west-1", "status": "ok", "functions": 2, "instances": 1},
            {"name": "us-east-1", "status": "ok", "functions": 0, "instances": 0},
        ],
        "total": 2,
        "omitted": 0,
    }
    assert out["summary"]["counts"] == {
        "account": 1,
        "region": 2,
        "lambda-function": 2,
        "ec2-instance": 1,
    }
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
        [
            ("account", "123456789012"),
            ("region", "eu-west-1"),
            ("ec2-instance", "i-0123456789abcdef0"),
        ],
        [("account", "123456789012"), ("region", "us-east-1")],
    ]  # us-east-1 has nothing and was read fine: still a region item
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
            "type": "ec2-instance",
            "parentPath": "aws/accounts/123456789012/regions/eu-west-1",
            "status": "complete",
            "count": 1,
        },
        {
            "type": "lambda-function",
            "parentPath": "aws/accounts/123456789012/regions/us-east-1",
            "status": "complete",
            "count": 0,
        },
        {
            "type": "ec2-instance",
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
        "instances": 0,  # lambda is denied there, ec2 is not: each list stands on its own
    }
    summary = env["outputs"]["summary"]
    assert summary["partitions"] == {"complete": 6, "failed": 0, "forbidden": 1, "excluded": 0}
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
        "instances": 1,
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
    assert {c["region"] for c in fake_aws.calls if c["words"][:2] in ACCOUNT_LEVEL} == {"us-east-1"}
    fake_aws.calls.clear()
    fake_aws.handlers.clear()
    answer(fake_aws)
    run = discover(run_script, settings={"defaultRegion": "eusc-de-east-1"})
    assert run.envelope["outputs"]["account"]["homeRegion"] == "eusc-de-east-1"
    assert {c["region"] for c in fake_aws.calls if c["words"][:2] in ACCOUNT_LEVEL} == {
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
    assert (
        sum(len(b["items"]) for b in sync["batches"]) == 1 + 5 + 5 + 1
    )  # the account, five regions, five functions and the one instance of eu-west-1
    assert names  # five regions, one function each


def test_over_500_items_go_in_ordered_batches_of_at_most_500(run_script, fake_aws):
    token = "next"
    first = {"Functions": [fn(f"f{i:04d}") for i in range(1000)], "NextToken": token}
    second = {"Functions": [fn(f"g{i:04d}") for i in range(300)], "NextToken": None}
    fake_aws.on("lambda", "list-functions", "--query", dc.FUNCTIONS_QUERY, "--max-items", "1000",
                "--starting-token", token, region="eu-west-1", body=second)  # fmt: skip
    answer(fake_aws, {"eu-west-1": first})
    sync = json.loads(discover(run_script).files["sync.json"])
    assert [len(b["items"]) for b in sync["batches"]] == [500, 500, 304]
    assert [b["seq"] for b in sync["batches"]] == [1, 2, 3]
    assert validate("rw.discovery.v1", sync) == []


def test_an_account_with_nothing_in_it_syncs_its_regions_and_sweeps_what_was_there(
    run_script, fake_aws
):
    answer(fake_aws, {"eu-west-1": listing()}, instances_by_region={"eu-west-1": instances()})
    run = discover(run_script)
    assert run.envelope["status"] == "ok"
    sync = json.loads(run.files["sync.json"])
    assert [len(b["items"]) for b in sync["batches"]] == [3]  # the account and its two regions
    base = "aws/accounts/123456789012"
    assert sync["commit"]["partitions"] == [
        {"type": "region", "parentPath": base, "status": "complete", "count": 2},
        *[
            {
                "type": kind,
                "parentPath": f"{base}/regions/{region}",
                "status": "complete",
                "count": 0,
            }
            for region in ("eu-west-1", "us-east-1")
            for kind in ("lambda-function", "ec2-instance")
        ],
    ]  # a complete partition of count 0: what an earlier run found there is swept


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
        "ec2-instance": 3,
    }


def test_discover_against_the_stub_partial_paged_throttled_forbidden_expired(run_with_stub):
    expected = {
        "partial": ("partial", ["permission_denied"]),
        "ec2-denied": ("partial", ["permission_denied"]),
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
    assert run.envelope["outputs"]["summary"]["counts"]["ec2-instance"] == 3  # two pages


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
    assert needed == {"ec2:DescribeRegions", "lambda:ListFunctions", "ec2:DescribeInstances"}


def test_the_entry_points_name_tasks_that_exist():
    integration = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    names = {t["name"] for t in tasks_doc()["tasks"]}
    for entry in integration["entryPoints"].values():
        skill, _, name = entry["task"].partition("/")
        assert skill == "aws-base" and name in names


def test_the_root_declares_the_key_discover_reports():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["account"]["canonicalKey"] == dc.CANONICAL_KEY
    assert types["region"]["parent"] == "account"
    assert all(types[k.type]["parent"] == "region" for k in dc.KINDS)
    reported = {"account", "region", *(k.type for k in dc.KINDS)}
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
    assert declared == narrowed == {"lambda:ListFunctions", "ec2:DescribeInstances"}
    assert "ec2:DescribeRegions" in task["description"] or "ec2:DescribeRegions" in json.dumps(
        tasks_doc()["settings"]["regions"]
    )


# --- EC2 instances: the second kind under a region ----------------------------------------------------

UNAUTHORIZED = (
    "\naws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the DescribeInstances "
    "operation: You are not authorized to perform this operation.\n"
)
RATE = (
    "\naws: [ERROR]: An error occurred (RequestLimitExceeded) when calling the DescribeInstances "
    "operation (reached max retries: 2): Request limit exceeded.\n"
)


def sync_items(run):
    return [i for b in json.loads(run.files["sync.json"])["batches"] for i in b["items"]]


def instance_items(run):
    return [i for i in sync_items(run) if i["identity"]["chain"][-1]["type"] == "ec2-instance"]


def test_instances_are_synced_under_their_region_with_an_arn_and_their_state(run_script, fake_aws):
    rows = instances("i-0a1b2c3d4e5f60718", "i-0b7c1d2e3f4a5b6c7")
    rows["Instances"][0]["Name"] = "orders-web-1"
    rows["Instances"][1]["State"] = "stopped"
    answer(fake_aws, instances_by_region={"eu-west-1": rows})
    run = discover(run_script)
    assert run.envelope["status"] == "ok"
    web, batch = instance_items(run)
    assert [c["type"] for c in web["identity"]["chain"]] == ["account", "region", "ec2-instance"]
    assert web["identity"]["chain"][-1]["name"] == "i-0a1b2c3d4e5f60718"
    assert web["providerUid"] == f"arn:aws:ec2:eu-west-1:{ACCT}:instance/i-0a1b2c3d4e5f60718"
    assert web["displayName"] == "orders-web-1" and web["document"]["name"] == "orders-web-1"
    assert web["document"]["instanceType"] == "t3.small" and web["status"] == {"state": "running"}
    assert batch["displayName"] == "i-0b7c1d2e3f4a5b6c7" and "name" not in batch["document"]
    assert batch["status"] == {"state": "stopped"}


def test_the_instance_list_leaves_out_terminated_instances_and_asks_for_a_page(
    run_script, fake_aws
):
    answer(fake_aws)
    discover(run_script)
    lists = [c["words"] for c in fake_aws.calls if c["words"][:2] == ["ec2", "describe-instances"]]
    assert len(lists) == 2
    for words in lists:
        assert words[words.index("--max-items") + 1] == "1000"
        assert "NextToken: NextToken" in words[words.index("--query") + 1]
        flt = words[words.index("--filters") + 1]
        assert flt.startswith("Name=instance-state-name,Values=") and "terminated" not in flt
        assert "stopped" in flt and "running" in flt


def test_the_instance_list_never_asks_for_user_data_tags_other_than_name_or_the_key_pair(
    run_script, fake_aws
):
    answer(fake_aws)
    discover(run_script)
    words = next(
        c["words"] for c in fake_aws.calls if c["words"][:2] == ["ec2", "describe-instances"]
    )
    query = words[words.index("--query") + 1]
    for banned in (
        "UserData",
        "KeyName",
        "KeyMaterial",
        "NetworkInterfaces",
        "BlockDeviceMappings",
    ):
        assert banned not in query
    assert "Tags[?Key=='Name']" in query and "Tags:" not in query.replace("Tags[?Key=='Name']", "")


def test_a_name_tag_that_looks_like_a_secret_is_not_synced(run_script, fake_aws):
    rows = instances("i-0a1b2c3d4e5f60718")
    rows["Instances"][0]["Name"] = "AKIAIOSFODNN7EXAMPLE"
    answer(fake_aws, instances_by_region={"eu-west-1": rows})
    run = discover(run_script)
    (item,) = instance_items(run)
    assert item["displayName"] == "i-0a1b2c3d4e5f60718" and "name" not in item["document"]
    assert "AKIAIOSFODNN7EXAMPLE" not in run.files["sync.json"] + json.dumps(run.envelope)


def test_a_region_where_only_ec2_is_denied_keeps_its_functions_and_is_forbidden(
    run_script, fake_aws
):
    for region in ("eu-west-1", "us-east-1"):
        fake_aws.on("ec2", "describe-instances", region=region, rc=254, stderr=UNAUTHORIZED)
    answer(fake_aws)
    run = discover(run_script)
    env = run.envelope
    assert env["status"] == "partial"
    assert scoped(env) == [("permission_denied", "regions:ec2-instance")]
    assert "ec2:DescribeInstances is denied in 2 region(s)" in env["errors"][0]["message"]
    assert env["outputs"]["regions"]["items"][0] == {
        "name": "eu-west-1", "status": "forbidden", "functions": 2, "instances": None,
    }  # fmt: skip
    sync = json.loads(run.files["sync.json"])
    assert validate("rw.discovery.v1", sync) == []
    parts = {
        (p["type"], p["parentPath"].rsplit("/", 1)[-1]): p["status"]
        for p in sync["commit"]["partitions"]
        if p["type"] != "region"
    }
    assert parts[("lambda-function", "eu-west-1")] == "complete"  # the other list stands
    assert parts[("ec2-instance", "eu-west-1")] == "forbidden"  # and sweeps nothing
    assert [i["identity"]["chain"][-1]["type"] for i in sync_items(run) if len(i["identity"]["chain"]) == 3] == [
        "lambda-function", "lambda-function",
    ]  # fmt: skip
    region_item = next(i for i in sync_items(run) if i["displayName"] == "eu-west-1")
    assert region_item["status"] == {"discovery": "forbidden"}


def test_a_throttled_instance_list_is_failed_not_empty(run_script, fake_aws):
    fake_aws.on("ec2", "describe-instances", region="eu-west-1", rc=254, stderr=RATE)
    answer(fake_aws)
    run = discover(run_script)
    assert run.envelope["status"] == "partial"
    assert scoped(run.envelope) == [("rate_limited", "eu-west-1")]
    part = next(
        p for p in json.loads(run.files["sync.json"])["commit"]["partitions"]
        if p["type"] == "ec2-instance" and p["parentPath"].endswith("eu-west-1")
    )  # fmt: skip
    assert (part["status"], part["reason"], part["count"]) == ("failed", "rate_limited", 0)


def test_the_instance_list_follows_pages_and_a_cut_list_is_failed_never_complete(
    run_script, fake_aws, monkeypatch
):
    from aws_base import awscli

    token = "eyJOZXh0VG9rZW4iOiAiYWJjIn0="
    fake_aws.on("ec2", "describe-instances", "--filters", dc.INSTANCES_FILTER, "--query",
                dc.INSTANCES_QUERY, "--max-items", "1000", "--starting-token", token,
                region="eu-west-1", body=instances("i-0b7c1d2e3f4a5b6c7"))  # fmt: skip
    answer(
        fake_aws, instances_by_region={"eu-west-1": instances("i-0a1b2c3d4e5f60718", token=token)}
    )
    run = discover(run_script)
    assert run.envelope["outputs"]["summary"]["counts"]["ec2-instance"] == 2
    fake_aws.handlers.clear()
    monkeypatch.setattr(awscli, "MAX_PAGES", 3)
    answer(
        fake_aws, instances_by_region={"eu-west-1": instances("i-0a1b2c3d4e5f60718", token="more")}
    )
    run = discover(run_script)
    assert scoped(run.envelope) == [("internal", "eu-west-1")]
    assert "instances than could be read" in run.envelope["errors"][0]["message"]


def test_a_paged_read_stops_when_the_budget_is_used_up_between_pages(
    run_script, fake_aws, monkeypatch
):
    clock = {"now": 0.0}
    monkeypatch.setattr(dc.time, "monotonic", lambda: clock["now"])
    original = fake_aws.__call__

    def slow(argv, **kw):
        clock["now"] += 300.0  # each call takes 300 s of the 420 s budget
        return original(argv, **kw)

    monkeypatch.setattr(type(fake_aws), "__call__", lambda self, argv, **kw: slow(argv, **kw))
    token = "more"
    answer(
        fake_aws, instances_by_region={"eu-west-1": instances("i-0a1b2c3d4e5f60718", token=token)}
    )
    fake_aws.on("ec2", "describe-instances", "--starting-token", token, region="eu-west-1",
                body=instances("i-0b7c1d2e3f4a5b6c7"))  # fmt: skip
    env = discover(run_script).envelope
    assert env["status"] == "partial"
    assert "timeout" in {e["code"] for e in env["errors"]}


def test_an_unread_region_is_one_timeout_error_however_many_kinds_it_holds(
    run_script, fake_aws, monkeypatch
):
    monkeypatch.setattr(dc, "READ_BUDGET", 0)
    answer(fake_aws)
    env = discover(run_script).envelope
    assert scoped(env) == [("timeout", "eu-west-1"), ("timeout", "us-east-1")]
    assert {(r["functions"], r["instances"]) for r in env["outputs"]["regions"]["items"]} == {
        (None, None)
    }
    assert [
        w for w in fake_aws.words() if w[0] in ("lambda", "ec2") and w[1] != "describe-regions"
    ] == []


def scoped(envelope):
    return [(e["code"], e.get("scope")) for e in envelope["errors"]]
