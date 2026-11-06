"""gcp-bucket-health: bucket-config, public-access and bucket-size, read from the stub's canned
gcloud and curl answers (the same data the fixtures are recorded from; gcloud's own output, see
tests/stub/capture), the classic bundle's false-passes, the permissions each task declares against
the calls it makes, and the bounds."""

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml
from conftest import PLUGIN, SETTINGS, bucket_target, load_script
from gcp_bucket_health import access as ax
from gcp_bucket_health import buckets as bk
from gcp_bucket_health import config as cf
from gcp_bucket_health import monitoring as mon
from gcp_bucket_health import size as sz
from gcp_bucket_health.errors import RwError
from gcp_bucket_health.settings import read_settings
from runwhen_capability.testing import make_context

SKILL = PLUGIN / "skills" / "gcp-bucket-health"
P = "shop-prod-1234"
GIB = 1024**3
TIB = 1024**4
SKILL_NAME = "gcp-bucket-health"
LIST = ["storage", "buckets", "list", f"--project={P}", "--raw", f"--billing-project={P}"]
TOKEN_CALL = ["auth", "print-access-token", f"--billing-project={P}"]


def describe(name):
    return ["storage", "buckets", "describe", f"gs://{name}", "--raw", f"--billing-project={P}"]


def iam(name):
    return ["storage", "buckets", "get-iam-policy", f"gs://{name}", f"--billing-project={P}"]


def du(name):
    return ["storage", "du", "--summarize", f"gs://{name}", f"--billing-project={P}"]


def run(run_script, task, stub, **kwargs):
    return run_script(task, skill=SKILL_NAME, stub=stub, **kwargs)


def items(env, output):
    return {i["name"]: i for i in env["outputs"][output]["items"]}


def codes(env):
    return [e["code"] for e in env["errors"]]


def stderr_403(command, permission):
    return (
        f"ERROR: (gcloud.{command}) HTTPError 403: rw-reader@{P}.iam.gserviceaccount.com does not "
        f"have {permission} access to the Google Cloud Storage bucket. Permission '{permission}' "
        "denied on resource (or it may not exist).\n"
    )


def rows_of(stub, scenario_file="buckets-list.json"):
    return json.loads((stub.directory / scenario_file).read_text())


# ---- bucket-config ----------------------------------------------------------------------------


def test_bucket_config_reports_each_buckets_settings_from_the_list_rows(run_script, stub_run):
    stub = stub_run("healthy")
    env = run(run_script, "bucket_config", stub).envelope
    assert env["status"] == "ok" and env["errors"] == []
    assert stub.asked() == [LIST], "the list rows are the full bucket resources: no call per bucket"
    buckets = env["outputs"]["buckets"]
    assert (buckets["total"], buckets["omitted"]) == (3, 0)
    by_name = items(env, "buckets")
    assert list(by_name) == ["shop-assets", "shop-backups", "shop-logs"]  # by name
    assets = by_name["shop-assets"]
    assert assets["versioning"] is True and assets["uniformBucketLevelAccess"] is True
    assert assets["publicAccessPrevention"] == "enforced" and assets["retention"] is None
    assert assets["lifecycleRuleCount"] == 1 and assets["softDeleteSeconds"] == 604800
    assert assets["labels"] == {"env": "prod", "team": "web"} and assets["cmek"] is False
    # gcloud renders timestamps itself (microseconds, +00:00), whatever the API sent
    assert assets["created"] == "2025-03-14T09:21:05.123000+00:00"
    backups = by_name["shop-backups"]
    assert backups["cmek"] is True and backups["kmsKey"].endswith("cryptoKeys/backups")
    assert (backups["location"], backups["locationType"], backups["storageClass"]) == (
        "US",
        "multi-region",
        "COLDLINE",
    )
    logs = by_name["shop-logs"]
    assert logs["retention"] == {"periodSeconds": 2592000, "locked": False}
    assert logs["versioning"] is False and logs["lifecycleRuleCount"] == 2
    assert logs["publicAccessPrevention"] == "inherited"


def test_bucket_config_totals_say_what_was_covered(run_script, stub_run):
    env = run(run_script, "bucket_config", stub_run("broken")).envelope
    assert env["outputs"]["totals"] == {"examined": 6, "total": 6, "omitted": 0, "complete": True}
    env = run(run_script, "bucket_config", stub_run("broken"), settings={"maxItems": 2}).envelope
    assert env["outputs"]["totals"] == {"examined": 2, "total": 6, "omitted": 4, "complete": False}


def test_bucket_config_on_a_bucket_target_reads_only_that_bucket(run_script, stub_run):
    stub = stub_run("broken")
    env = run(run_script, "bucket_config", stub, resource=bucket_target("shop-legacy")).envelope
    assert env["status"] == "ok" and list(items(env, "buckets")) == ["shop-legacy"]
    assert items(env, "buckets")["shop-legacy"]["uniformBucketLevelAccess"] is False
    assert stub.asked() == [describe("shop-legacy")]
    assert env["outputs"]["totals"]["complete"] is True


def test_the_config_outputs_fit_their_schemas(run_script, stub_run):
    env = run(run_script, "bucket_config", stub_run("broken")).envelope
    for name, file in (("buckets", "bucket-config.v1.json"), ("totals", "config-totals.v1.json")):
        jsonschema.validate(
            env["outputs"][name], json.loads((SKILL / "schemas" / file).read_text())
        )
    assert env["outputs"]["buckets"]["total"] == 6


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("forbidden", "permission_denied"),
        ("unauthenticated", "permission_denied"),
        ("throttled", "rate_limited"),
        ("api-disabled", "dependency"),
    ],
)
@pytest.mark.parametrize("task", ["bucket_config", "public_access", "bucket_size"])
def test_a_failed_bucket_list_fails_every_task_with_the_classified_code(
    run_script, stub_run, task, scenario, code
):
    env = run(run_script, task, stub_run(scenario)).envelope
    assert env["status"] == "failed" and codes(env) == [code]
    assert list(env["outputs"]) == []  # nothing is reported as if it were healthy


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("forbidden", "permission_denied"),
        ("throttled", "rate_limited"),
        ("unauthenticated", "permission_denied"),
        ("api-disabled", "dependency"),
    ],
)
@pytest.mark.parametrize("task", ["bucket_config", "public_access", "bucket_size"])
def test_a_bucket_target_that_cannot_be_read_fails(run_script, stub_run, task, scenario, code):
    env = run(run_script, task, stub_run(scenario), resource=bucket_target("shop-assets")).envelope
    assert env["status"] == "failed" and codes(env) == [code]


@pytest.mark.parametrize("task", ["bucket_config", "public_access", "bucket_size"])
def test_a_bucket_that_does_not_exist_is_not_found(run_script, stub_run, task):
    env = run(run_script, task, stub_run("not-found"), resource=bucket_target("nope")).envelope
    assert env["status"] == "failed" and codes(env) == ["not_found"]
    assert "gs://nope not found: 404" in env["errors"][0]["message"]


def test_an_empty_project_is_an_empty_list(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(LIST, stdout="[]\n")
    for task, output in (
        ("bucket_config", "buckets"),
        ("public_access", "access"),
        ("bucket_size", "sizes"),
    ):
        env = run(run_script, task, stub).envelope
        assert env["status"] == "ok" and env["outputs"][output] == {
            "items": [],
            "total": 0,
            "omitted": 0,
        }
        assert (
            env["outputs"]["totals"]["complete"] is True and env["outputs"]["totals"]["total"] == 0
        )


def test_max_items_limits_how_many_buckets_are_looked_at(run_script, stub_run):
    stub = stub_run("broken")
    env = run(run_script, "bucket_config", stub, settings={"maxItems": 2}).envelope
    buckets = env["outputs"]["buckets"]
    assert (len(buckets["items"]), buckets["total"], buckets["omitted"]) == (2, 6, 4)
    stub = stub_run("broken")
    run(run_script, "public_access", stub, settings={"maxItems": 2})
    assert [a[:3] for a in stub.asked()].count(["storage", "buckets", "get-iam-policy"]) == 2, (
        "the buckets beyond maxItems are not even read"
    )


def test_labels_and_texts_are_bounded():
    row = {
        "name": "b",
        "labels": {f"k{i:03d}": "v" * 2000 for i in range(80)},
        "location": "L" * 2000,
        "encryption": {"defaultKmsKeyName": "k" * 3000},
    }
    entry = cf.bucket_config(row)
    assert len(entry["labels"]) == 50 and entry["labelsTruncated"] is True
    assert all(len(v) <= 1024 for v in entry["labels"].values())
    assert len(entry["location"]) == 1024 and len(entry["kmsKey"]) == 1024


def test_config_of_a_bare_bucket_resource_has_no_surprises():
    entry = cf.bucket_config({"name": "b"})
    assert entry["versioning"] is False and entry["retention"] is None
    assert entry["lifecycleRuleCount"] == 0 and entry["softDeleteSeconds"] is None
    assert entry["cmek"] is False and entry["kmsKey"] is None and entry["labels"] == {}
    assert entry["publicAccessPrevention"] is None and entry["uniformBucketLevelAccess"] is False


# ---- per-bucket loops: errors capped, time budget ---------------------------------------------


def many_buckets(count, uniform=True):
    return [
        {
            "name": f"bucket-{i:03d}",
            "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": uniform}},
        }
        for i in range(count)
    ]


def test_per_bucket_errors_are_capped_at_50_and_the_rest_counted_in_one(run_script, stub_run):
    stub = stub_run("healthy")
    rows = many_buckets(61)
    stub.set(LIST, stdout=json.dumps(rows))
    for row in rows[1:]:  # all but the first fail for good (a bucket deleted since the list)
        stub.set(iam(row["name"]), rc=1,
                 stderr=f"ERROR: (gcloud.storage.buckets.get-iam-policy) gs://{row['name']} not found: 404.\n")  # fmt: skip
    policy = json.loads((stub.directory / "iam-shop-assets.json").read_text())
    stub.set(iam("bucket-000"), stdout=json.dumps(policy))
    run_ = run(run_script, "public_access", stub, settings={"maxItems": 100})
    env = run_.envelope
    assert env["status"] == "partial"
    scoped = [e for e in env["errors"] if e["scope"] != "buckets"]
    assert len(scoped) == 50 and {e["code"] for e in scoped} == {"not_found"}
    (rest,) = [e for e in env["errors"] if e["scope"] == "buckets"]
    assert rest["code"] == "not_found" and rest["details"] == {
        "count": 10,
        "codes": {"not_found": 10},
    }
    assert "10 more buckets failed" in rest["message"]
    assert len(env["outputs"]["access"]["items"]) == 1 and env["outputs"]["totals"]["total"] == 61
    assert env["outputs"]["totals"]["complete"] is False


def test_a_per_bucket_budget_skips_the_buckets_it_cannot_start_and_says_so(stub_run):
    stub = stub_run("broken")
    resource = {"path": f"gcp/projects/{P}", "type": "gcp/project", "name": P}

    def started():
        return len([a for a in stub.asked() if a[:3] == ["storage", "buckets", "get-iam-policy"]])

    def clock():  # the deadline is 102: after two buckets the clock reads 100, under the 5s floor
        return 100.0 if started() >= 2 else 0.0

    with make_context(resource=resource, settings=SETTINGS) as tc:
        tc.ctx.run = stub
        st = read_settings(tc.ctx.settings, "public_bucket_threshold")
        out = ax.run(tc.ctx, st, deadline=102.0, clock=clock)
        errors = list(tc.errors)
    assert out["totals"]["examined"] == 2 and out["totals"]["total"] == 6
    assert out["totals"]["complete"] is False
    assert [e["code"] for e in errors] == ["timeout"] * 4
    started_names = {
        a[3][5:] for a in stub.asked() if a[:3] == ["storage", "buckets", "get-iam-policy"]
    }
    assert {e["scope"] for e in errors} == {
        "shop-assets",
        "shop-backups",
        "shop-legacy",
        "shop-logs",
        "shop-archive",
        "shop-public-assets",
    } - started_names
    assert all("time budget" in e["message"] for e in errors)
    assert started() == 2


def test_the_access_budget_matches_the_task_timeout():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "public-access")
    assert task["timeout"] == f"{ax.TASK_BUDGET}s"
    assert ax.deadline_from(1000.0) == 1000.0 + ax.TASK_BUDGET - ax.RESERVE


def test_report_failures_caps_and_aggregates():
    class Ctx:
        def __init__(self):
            self.errors = []

        def error(self, code, message, scope=None, details=None):
            self.errors.append((code, message, scope, details))

    ctx = Ctx()
    failures = [(f"b{i}", RwError("rate_limited" if i % 3 else "timeout", "x")) for i in range(75)]
    bk.report_failures(ctx, failures)
    assert len(ctx.errors) == 51 and ctx.errors[-1][2] == "buckets"
    assert ctx.errors[-1][3]["count"] == 25
    assert sum(ctx.errors[-1][3]["codes"].values()) == 25
    ctx = Ctx()
    bk.report_failures(ctx, failures[:50])
    assert len(ctx.errors) == 50 and all(e[2] != "buckets" for e in ctx.errors)


# ---- public-access ----------------------------------------------------------------------------


def test_healthy_buckets_have_no_public_grant(run_script, stub_run):
    stub = stub_run("healthy")
    env = run(run_script, "public_access", stub).envelope
    assert env["status"] == "ok"
    assert {n: e["publicGrant"] for n, e in items(env, "access").items()} == {
        "shop-assets": False, "shop-backups": False, "shop-logs": False,
    }  # fmt: skip
    assert env["outputs"]["totals"] == {
        "examined": 3, "total": 3, "omitted": 0, "complete": True,
        "publicBuckets": 0, "unknown": 0, "threshold": 0, "overThreshold": False,
    }  # fmt: skip
    # the ACLs come from the list rows: one list, then only the policy of each bucket
    assert stub.asked() == [LIST, iam("shop-assets"), iam("shop-backups"), iam("shop-logs")]


def test_public_grants_are_found_in_iam_and_in_legacy_acls(run_script, stub_run):
    env = run(run_script, "public_access", stub_run("broken")).envelope
    assert env["status"] == "ok"
    by_name = items(env, "access")
    pub = by_name["shop-public-assets"]
    assert (pub["publicGrant"], pub["publicViaIam"], pub["publicViaAcl"]) == (True, True, False)
    assert pub["iamBindings"] == [
        {"role": "roles/storage.objectViewer", "members": ["allUsers"], "conditional": False}
    ]
    legacy = by_name["shop-legacy"]
    assert legacy["uniformBucketLevelAccess"] is False
    assert (legacy["publicViaAcl"], legacy["publicViaIam"]) == (True, True)
    assert legacy["aclEntries"] == [{"entity": "allUsers", "role": "READER"}]
    assert legacy["publicViaDefaultObjectAcl"] is False and legacy["defaultObjectAclEntries"] == []
    assert legacy["iamBindings"] == [  # the conditional allAuthenticatedUsers binding is a fact
        {
            "role": "roles/storage.objectViewer",
            "members": ["allAuthenticatedUsers"],
            "conditional": True,
        }
    ]
    assert by_name["shop-archive"]["publicGrant"] is False
    totals = env["outputs"]["totals"]
    assert (totals["examined"], totals["publicBuckets"], totals["overThreshold"]) == (6, 2, True)


def test_the_default_object_acl_of_a_non_uniform_bucket_is_checked_too(run_script, stub_run):
    # new objects start public although the bucket ACL and the IAM policy are clean
    stub = stub_run("broken")
    rows = rows_of(stub)
    for row in rows:
        if row["name"] == "shop-legacy":
            row["acl"] = [e for e in row["acl"] if e["entity"] != "allUsers"]
            row["defaultObjectAcl"] = [
                {"entity": "allAuthenticatedUsers", "role": "READER"},
                {"entity": "project-owners-123456789012", "role": "OWNER"},
            ]
    stub.set(LIST, stdout=json.dumps(rows))
    env = run(run_script, "public_access", stub).envelope
    legacy = items(env, "access")["shop-legacy"]
    assert legacy["publicViaAcl"] is False and legacy["publicViaDefaultObjectAcl"] is True
    assert legacy["defaultObjectAclEntries"] == [
        {"entity": "allAuthenticatedUsers", "role": "READER"}
    ]
    # drop the IAM grant as well: the default ACL alone makes the grant
    stub.set(iam("shop-legacy"), stdout=json.dumps({"bindings": [], "etag": "CAI="}))
    env = run(run_script, "public_access", stub).envelope
    assert items(env, "access")["shop-legacy"]["publicGrant"] is True


def test_default_object_acls_do_not_count_with_uniform_access(run_script, stub_run):
    stub = stub_run("healthy")
    rows = rows_of(stub)
    rows[0]["defaultObjectAcl"] = [{"entity": "allUsers", "role": "READER"}]
    stub.set(LIST, stdout=json.dumps(rows))
    env = run(run_script, "public_access", stub).envelope
    assert items(env, "access")["shop-assets"]["publicViaDefaultObjectAcl"] is False


def test_the_public_bucket_threshold_setting_decides_over_threshold(run_script, stub_run):
    env = run(
        run_script, "public_access", stub_run("broken"), settings={"publicBucketThreshold": 2}
    ).envelope
    assert env["outputs"]["totals"]["overThreshold"] is False
    assert env["outputs"]["totals"]["threshold"] == 2


def test_classic_false_pass_iam_is_read_on_buckets_without_uniform_access_too(run_script, stub_run):
    # classic check_security.sh read the IAM policy only with uniform access on and the ACL only
    # otherwise: a legacy bucket with a public IAM binding and clean ACLs passed
    stub = stub_run("healthy")
    legacy = json.loads((stub.directory / "bucket-shop-assets.json").read_text())
    legacy["iamConfiguration"]["uniformBucketLevelAccess"]["enabled"] = False
    legacy["iamConfiguration"]["bucketPolicyOnly"]["enabled"] = False
    legacy["acl"] = [{"entity": "project-owners-123", "role": "OWNER"}]
    policy = {"bindings": [{"members": ["allUsers"], "role": "roles/storage.objectViewer"}]}
    stub.set(describe("shop-assets"), stdout=json.dumps(legacy))
    stub.set(iam("shop-assets"), stdout=json.dumps(policy))
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    entry = items(env, "access")["shop-assets"]
    assert (entry["publicGrant"], entry["publicViaIam"], entry["publicViaAcl"]) == (
        True,
        True,
        False,
    )


def test_acls_do_not_count_on_a_bucket_with_uniform_access(run_script, stub_run):
    stub = stub_run("healthy")
    row = json.loads((stub.directory / "bucket-shop-assets.json").read_text())
    row["acl"] = [{"entity": "allUsers", "role": "READER"}]  # ignored by the service
    stub.set(describe("shop-assets"), stdout=json.dumps(row))
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    assert items(env, "access")["shop-assets"]["publicViaAcl"] is False


def test_public_access_prevention_makes_a_grant_ineffective_and_says_so(run_script, stub_run):
    stub = stub_run("healthy")
    policy = {"bindings": [{"members": ["allUsers"], "role": "roles/storage.objectViewer"}]}
    stub.set(iam("shop-assets"), stdout=json.dumps(policy))
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    entry = items(env, "access")["shop-assets"]  # shop-assets has publicAccessPrevention enforced
    assert (entry["publicGrant"], entry["blockedByPrevention"]) == (True, True)


def test_a_throttled_policy_read_is_unknown_not_private_and_partial(run_script, stub_run):
    # classic: `get-iam-policy ... 2>/dev/null || echo '{}'` -> a failed read meant "not public"
    env = run(run_script, "public_access", stub_run("partial-throttled")).envelope
    assert env["status"] == "partial"
    by_name = items(env, "access")
    assert (
        by_name["shop-logs"]["publicGrant"] is None and by_name["shop-logs"]["publicViaIam"] is None
    )
    assert by_name["shop-legacy"]["publicGrant"] is True
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("rate_limited", "shop-logs")]
    totals = env["outputs"]["totals"]
    assert (totals["publicBuckets"], totals["unknown"], totals["examined"]) == (2, 1, 6)
    assert totals["complete"] is False


def test_a_denied_policy_read_is_unknown_not_private_and_partial(run_script, stub_run):
    # Unreachable with the FULL projection (the list already needs storage.buckets.getIamPolicy),
    # but a policy read that is denied must still never read as "not public"
    stub = stub_run("healthy")
    for name in ("shop-assets", "shop-logs", "shop-backups"):
        stub.set(
            iam(name),
            rc=1,
            stderr=stderr_403("storage.buckets.get-iam-policy", "storage.buckets.getIamPolicy"),
        )
    env = run(run_script, "public_access", stub).envelope
    assert env["status"] == "partial"
    assert {e["code"] for e in env["errors"]} == {"permission_denied"}
    assert {e["publicGrant"] for e in items(env, "access").values()} == {None}


def test_a_missing_bucket_on_the_policy_read_still_fails_that_bucket(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(iam("shop-assets"), rc=1,
             stderr="ERROR: (gcloud.storage.buckets.get-iam-policy) gs://shop-assets not found: 404.\n")  # fmt: skip
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    assert env["status"] == "failed" and codes(env) == ["not_found"]


def test_the_access_outputs_fit_their_schemas(run_script, stub_run):
    env = run(run_script, "public_access", stub_run("broken")).envelope
    for name, schema_file in (
        ("access", "public-access.v1.json"),
        ("totals", "public-totals.v1.json"),
    ):
        jsonschema.validate(
            env["outputs"][name], json.loads((SKILL / "schemas" / schema_file).read_text())
        )


def test_public_member_matching_is_exact():
    policy = {"bindings": [
        {"members": ["user:allUsers@example.com", "domain:example.com"], "role": "r"},
        {"members": ["allUsers", "allUsers", "serviceAccount:x"], "role": "roles/storage.objectViewer"},
        "not a binding",
    ]}  # fmt: skip
    assert ax.public_iam_bindings(policy) == [
        {"role": "roles/storage.objectViewer", "members": ["allUsers"], "conditional": False}
    ]
    assert ax.public_iam_bindings({}) == []
    row = {
        "acl": [
            {"entity": "allAuthenticatedUsers", "role": "READER"},
            {"entity": "user-x@y.z", "role": "OWNER"},
        ]
    }
    assert ax.public_acl_entries(row) == [{"entity": "allAuthenticatedUsers", "role": "READER"}]
    assert ax.public_acl_entries(row, "defaultObjectAcl") == []


# ---- bucket-size: the du line -----------------------------------------------------------------


def du_line(size, bucket="shop-assets"):
    """What `gcloud storage du --summarize` prints: du_command_util.py's '{size:<13}{url}'."""
    return "{size:<13}{url}".format(size=size, url=f"gs://{bucket}") + "\n"


@pytest.mark.parametrize("size", [0, 1, 734003200, 10**12 - 1, 10**12, int(1.5 * TIB), 10**15])
def test_du_totals_are_read_from_the_real_format_whatever_the_padding(size):
    line = du_line(size)
    assert sz.parse_du(line, "shop-assets") == size
    if size >= 10**12:  # 13 digits fill the column: the size runs into gs://
        assert line.startswith(f"{size}gs://"), line


def test_the_formats_that_broke_the_old_parser_are_pinned():
    assert du_line(0) == "0            gs://shop-assets\n"
    assert du_line(10**12) == "1000000000000gs://shop-assets\n"
    assert du_line(int(1.5 * TIB)) == "1649267441664gs://shop-assets\n"


@pytest.mark.parametrize(
    "text",
    [du_line(5) + "100  gs://shop-assets/a.txt\n", "  734003200\tgs://shop-assets  \n",
     "734003200 gs://shop-assets/\n"],
)  # fmt: skip
def test_du_totals_tolerate_whitespace_a_trailing_slash_and_other_lines(text):
    assert sz.parse_du(text, "shop-assets") in (5, 734003200)


@pytest.mark.parametrize(
    "text",
    [
        "", "\n", "no total here\n", "12  gs://other-bucket\n", "abc  gs://shop-assets\n",
        "-5  gs://shop-assets\n", "100  gs://shop-assets/a.txt\n",
    ],
)  # fmt: skip
def test_classic_false_pass_a_du_without_a_total_is_an_error_never_zero_bytes(text):
    with pytest.raises(RwError) as caught:
        sz.parse_du(text, "shop-assets")
    assert caught.value.code == "internal"


# ---- bucket-size: Cloud Monitoring ------------------------------------------------------------


def test_sizes_come_from_one_monitoring_query_largest_first(run_script, stub_run):
    stub = stub_run("broken")
    result = run(run_script, "bucket_size", stub)
    env = result.envelope
    assert env["status"] == "ok" and env["errors"] == []
    # one list, one token, one Monitoring query for the whole project: no du, no call per bucket
    assert stub.asked() == [LIST, TOKEN_CALL]
    assert len(stub.asked("curl")) == 1
    sizes = env["outputs"]["sizes"]["items"]
    assert [s["name"] for s in sizes] == [
        "shop-archive", "shop-backups", "shop-logs", "shop-legacy", "shop-public-assets", "shop-assets",
    ]  # fmt: skip
    assert sizes[0] == {
        "name": "shop-archive", "sizeBytes": int(1.5 * TIB), "sizeTiB": 1.5, "overThreshold": True,
        "source": "monitoring", "byType": {"live-object": int(1.5 * TIB)},
    }  # fmt: skip
    assert [s["overThreshold"] for s in sizes] == [True] + [False] * 5
    totals = env["outputs"]["totals"]
    assert totals == {
        "examined": 6, "total": 6, "omitted": 0, "complete": True, "notMeasured": 0,
        "thresholdTiB": 0.5, "overThreshold": 1, "totalBytes": sum(s["sizeBytes"] for s in sizes),
    }  # fmt: skip


def test_a_buckets_size_is_summed_over_its_object_types_and_reported_per_type(run_script, stub_run):
    env = run(run_script, "bucket_size", stub_run("broken")).envelope
    assets = items(env, "sizes")["shop-assets"]
    assert assets["byType"] == {"live-object": 660602880, "noncurrent-object": 73400320}
    assert assets["sizeBytes"] == 734003200  # live + noncurrent
    assert items(env, "sizes")["shop-backups"]["sizeBytes"] == 193273528320 + 21474836480


def test_the_token_goes_to_curl_only_through_a_private_header_file(run_script, stub_run):
    stub = stub_run("healthy")
    result = run(run_script, "bucket_size", stub)
    (call,) = [c for c in stub.log if c["tool"] == "curl"]
    argv = call["argv"]
    assert argv[:7] == [
        "curl",
        "-sS",
        "--fail-with-body",
        "--max-redirs",
        "0",
        "-H",
        "@.gcp-monitoring-header",
    ]
    assert argv[7].startswith(
        "https://monitoring.googleapis.com/v3/projects/shop-prod-1234/timeSeries?"
    )
    token = json.loads((PLUGIN / "tests" / "stub" / "identities.json").read_text())["fakeToken"]
    assert not any(token in a for a in argv), "the token is never on the command line"
    # StubRun checked the file at call time: mode 0600, Authorization and the user project, only
    (header,) = stub.headers
    assert header == f"Authorization: Bearer {token}\nX-Goog-User-Project: {P}\n"
    assert result.workdir_dotfiles == [], "the header file is deleted"
    everything = (
        json.dumps(result.envelope) + json.dumps(result.errors) + "".join(result.files.values())
    )
    assert token not in everything


@pytest.mark.parametrize("scenario", ["monitoring-disabled"])
def test_the_header_file_is_deleted_when_the_call_fails_too(run_script, stub_run, scenario):
    result = run(run_script, "bucket_size", stub_run(scenario))
    assert result.envelope["status"] == "failed" and result.workdir_dotfiles == []


def test_a_disabled_monitoring_api_is_a_dependency_failure(run_script, stub_run):
    env = run(run_script, "bucket_size", stub_run("monitoring-disabled")).envelope
    assert env["status"] == "failed" and codes(env) == ["dependency"]
    assert "Cloud Monitoring" in env["errors"][0]["message"]
    assert "ya29" not in env["errors"][0]["message"]


def test_the_monitoring_query_is_one_project_filter_in_a_six_hour_window(run_script, stub_run):
    stub = stub_run("healthy")
    run(run_script, "bucket_size", stub)
    (curl,) = stub.asked("curl")
    assert curl[-1] == (
        "https://monitoring.googleapis.com/v3/projects/shop-prod-1234/timeSeries"
        "?filter=metric.type%20%3D%20%22storage.googleapis.com%2Fstorage%2Fv2%2Ftotal_bytes%22"
        "%20AND%20resource.type%20%3D%20%22gcs_bucket%22"
        "%20AND%20resource.labels.project_id%20%3D%20%22shop-prod-1234%22"
        "&interval.endTime=<T>&interval.startTime=<T>"
        "&aggregation.alignmentPeriod=3600s&aggregation.perSeriesAligner=ALIGN_MAX&pageSize=1000"
    )
    now = datetime(2026, 11, 4, 10, 30, 15, 999, tzinfo=UTC)
    assert mon.window(now) == ("2026-11-04T04:30:15Z", "2026-11-04T10:30:15Z")
    narrow = mon.url(P, now, bucket="shop-assets", page_token="tok/1")
    assert "resource.labels.bucket_name%20%3D%20%22shop-assets%22" in narrow
    assert narrow.endswith("&pageToken=tok%2F1")
    assert "interval.endTime=2026-11-04T10%3A30%3A15Z" in narrow


def test_the_query_time_is_the_runs_reference_time_so_a_replay_asks_the_same(stub_run):
    stub = stub_run("healthy")
    resource = {"path": f"gcp/projects/{P}", "type": "gcp/project", "name": P}
    urls = []
    for _ in range(2):
        with make_context(
            resource=resource, settings=SETTINGS, now=datetime(2026, 11, 4, 1, 2, 3, tzinfo=UTC)
        ) as tc:
            tc.ctx.run = stub
            stub.workdir = tc.ctx.workdir
            st = read_settings(tc.ctx.settings, "usage_threshold_tib", "size_timeout")
            sz.run(tc.ctx, st, deadline=10**9)
        urls.append([c["argv"][-1] for c in stub.log if c["tool"] == "curl"][-1])
    assert urls[0] == urls[1] and "interval.endTime=2026-11-04T01%3A02%3A03Z" in urls[0]


def test_buckets_without_a_series_are_unknown_not_zero_and_named_in_one_error(run_script, stub_run):
    stub = stub_run("broken")
    doc = json.loads((stub.directory / "monitoring-project.json").read_text())
    doc["timeSeries"] = [
        s
        for s in doc["timeSeries"]
        if s["resource"]["labels"]["bucket_name"] not in ("shop-logs", "shop-legacy")
    ]
    (curl,) = [
        c
        for c in stub.calls
        if c.get("tool") == "curl" and c["stdout"] == "monitoring-project.json"
    ]
    stub.set(curl["argv"], stdout=json.dumps(doc), tool="curl")
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "partial"
    assert sorted(items(env, "sizes")) == [
        "shop-archive", "shop-assets", "shop-backups", "shop-public-assets",
    ]  # fmt: skip
    (error,) = env["errors"]
    assert (error["code"], error["scope"]) == ("not_found", "monitoring")
    assert error["details"] == {"count": 2, "buckets": ["shop-legacy", "shop-logs"]}
    totals = env["outputs"]["totals"]
    assert (totals["notMeasured"], totals["examined"], totals["total"], totals["complete"]) == (
        2,
        4,
        6,
        False,
    )
    assert [a for a in stub.asked() if a[:2] == ["storage", "du"]] == [], (
        "no du on a project target"
    )


def test_a_bucket_target_with_a_series_is_measured_without_du(run_script, stub_run):
    stub = stub_run("broken")
    resource = bucket_target("shop-archive")
    env = run(run_script, "bucket_size", stub, resource=resource).envelope
    assert env["status"] == "ok" and items(env, "sizes")["shop-archive"]["source"] == "monitoring"
    assert stub.asked() == [describe("shop-archive"), TOKEN_CALL]


def test_du_is_the_fallback_for_a_single_bucket_monitoring_has_no_series_for(run_script, stub_run):
    stub = stub_run("du-fallback")
    env = run(run_script, "bucket_size", stub, resource=bucket_target("shop-assets")).envelope
    assert env["status"] == "ok" and env["errors"] == []
    assert stub.asked() == [describe("shop-assets"), TOKEN_CALL, du("shop-assets")]
    assert items(env, "sizes")["shop-assets"] == {
        "name": "shop-assets", "sizeBytes": 10**12 + 5, "sizeTiB": round((10**12 + 5) / TIB, 4),
        "overThreshold": True, "source": "du", "byType": {"live-object": 10**12 + 5},
    }  # fmt: skip
    # the real du line of a 13-digit size runs into gs://
    assert (stub.directory / "du-shop-assets.txt").read_text() == f"{10**12 + 5}gs://shop-assets\n"


def test_du_gets_its_own_longer_timeout_and_monitoring_the_command_timeout(run_script, stub_run):
    stub = stub_run("du-fallback")
    run(run_script, "bucket_size", stub, resource=bucket_target("shop-assets"))
    by_call = {
        (c["tool"], c["asked"][0] if c["tool"] == "curl" else " ".join(c["asked"][:2])): c[
            "timeout"
        ]
        for c in stub.log
    }
    assert by_call[("gcloud", "storage du")] == 300.0
    assert by_call[("gcloud", "storage buckets")] == 60.0


def test_a_du_that_fails_on_the_fallback_fails_the_task(run_script, stub_run):
    stub = stub_run("du-fallback")
    stub.set(du("shop-assets"), rc=1, stderr=stderr_403("storage.du", "storage.objects.list"))
    env = run(run_script, "bucket_size", stub, resource=bucket_target("shop-assets")).envelope
    assert env["status"] == "failed" and codes(env) == ["permission_denied"]


def test_a_du_without_a_total_on_the_fallback_is_an_error_never_zero(run_script, stub_run):
    stub = stub_run("du-fallback")
    stub.set(du("shop-assets"), stdout="")
    env = run(run_script, "bucket_size", stub, resource=bucket_target("shop-assets")).envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]


def test_a_failed_token_is_classified_and_stops_before_any_monitoring_call(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(TOKEN_CALL, rc=1, stderr="ERROR: (gcloud.auth.print-access-token) There was a problem refreshing "
             "your current auth tokens: Reauthentication required.\n")  # fmt: skip
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "failed" and codes(env) == ["permission_denied"]
    assert stub.asked("curl") == []


@pytest.mark.parametrize("token", ["", "two words", "a\nb", "short", "x" * 5000])
def test_an_unusable_token_is_internal_and_never_echoed(run_script, stub_run, token):
    stub = stub_run("healthy")
    stub.set(TOKEN_CALL, stdout=token + "\n")
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "failed" and codes(env) == ["internal"]
    assert token.strip() == "" or token.strip()[:6] not in env["errors"][0]["message"]
    assert stub.asked("curl") == []


@pytest.mark.parametrize(
    "rc, stdout, stderr, code",
    [
        (28, "", "curl: (28) Operation timed out\n", "timeout"),
        (6, "", "curl: (6) Could not resolve host: monitoring.googleapis.com\n", "unreachable"),
        (7, "", "curl: (7) Failed to connect\n", "unreachable"),
        (60, "", "curl: (60) SSL certificate problem\n", "unreachable"),
        (22, '{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Permission monitoring.timeSeries.list denied"}}',
         "curl: (22) The requested URL returned error: 403\n", "permission_denied"),
        (22, '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}',
         "curl: (22) The requested URL returned error: 429\n", "rate_limited"),
        (22, '{"error": {"code": 404, "message": "gone"}}', "curl: (22) The requested URL returned error: 404\n", "not_found"),
        (22, '{"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad filter"}}',
         "curl: (22) The requested URL returned error: 400\n", "invalid_input"),
        (22, "<html>", "curl: (22) The requested URL returned error: 503\n", "unreachable"),
        (22, '{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API disabled", '
             '"details": [{"reason": "SERVICE_DISABLED"}]}}', "curl: (22) The requested URL returned error: 403\n", "dependency"),
        (2, "", "curl: (2) odd\n", "internal"),
    ],
)  # fmt: skip
def test_a_failed_monitoring_call_is_classified(run_script, stub_run, rc, stdout, stderr, code):
    stub = stub_run("healthy")
    (curl,) = [
        c
        for c in stub.calls
        if c.get("tool") == "curl" and c["stdout"] == "monitoring-project.json"
    ]
    stub.set(curl["argv"], rc=rc, stdout=stdout, stderr=stderr, tool="curl")
    result = run(run_script, "bucket_size", stub)
    assert result.envelope["status"] == "failed" and codes(result.envelope) == [code]
    assert result.workdir_dotfiles == []


def test_pages_are_followed_and_a_series_on_a_later_page_counts(run_script, stub_run):
    stub = stub_run("broken")
    doc = json.loads((stub.directory / "monitoring-project.json").read_text())
    half = len(doc["timeSeries"]) // 2
    first = {"timeSeries": doc["timeSeries"][:half], "nextPageToken": "PAGE-2"}
    second = {"timeSeries": doc["timeSeries"][half:]}
    (curl,) = [
        c
        for c in stub.calls
        if c.get("tool") == "curl" and c["stdout"] == "monitoring-project.json"
    ]
    stub.set(curl["argv"], stdout=json.dumps(first), tool="curl")
    stub.set(
        [*curl["argv"][:-1], curl["argv"][-1] + "&pageToken=PAGE-2"],
        stdout=json.dumps(second),
        tool="curl",
    )
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "ok" and len(stub.asked("curl")) == 2
    assert len(env["outputs"]["sizes"]["items"]) == 6


def test_monitoring_pages_stop_at_the_deadline_and_say_so(stub_run):
    stub = stub_run("broken")
    doc = json.loads((stub.directory / "monitoring-project.json").read_text())
    stub.set(
        next(c for c in stub.calls if c.get("tool") == "curl")["argv"],
        stdout=json.dumps({**doc, "nextPageToken": "MORE"}), tool="curl",
    )  # fmt: skip
    resource = {"path": f"gcp/projects/{P}", "type": "gcp/project", "name": P}
    clock_values = iter([0.0, 100.0])  # the first page starts, the second has no time left
    with make_context(resource=resource, settings=SETTINGS) as tc:
        tc.ctx.run = stub
        stub.workdir = tc.ctx.workdir
        st = read_settings(tc.ctx.settings, "usage_threshold_tib", "size_timeout")
        out = sz.run(tc.ctx, st, deadline=102.0, clock=lambda: next(clock_values))
        errors = list(tc.errors)
    assert [(e["code"], e["scope"]) for e in errors] == [("timeout", "monitoring")]
    assert out["totals"]["examined"] == 6 and len(stub.asked("curl")) == 1


def test_series_are_summed_per_bucket_and_type_with_the_newest_point(stub_run):
    def series(bucket, kind, points, project=P, storage_class="STANDARD"):
        return {
            "metric": {"labels": {"storage_class": storage_class, "type": kind}},
            "resource": {"labels": {"bucket_name": bucket, "project_id": project}},
            "points": [{"interval": {"endTime": end}, "value": value} for end, value in points],
        }

    got = mon.sizes_by_bucket(
        [
            series(
                "b-one",
                "live-object",
                [
                    ("2026-11-04T01:00:00Z", {"doubleValue": 100.0}),
                    ("2026-11-04T02:00:00Z", {"doubleValue": 300.0}),
                ],
            ),
            series(
                "b-one",
                "live-object",
                [("2026-11-04T02:00:00Z", {"int64Value": "50"})],
                storage_class="COLDLINE",
            ),
            series("b-one", "noncurrent-object", [("2026-11-04T02:00:00Z", {"doubleValue": 7.0})]),
            series("b-two", "live-object", [("2026-11-04T02:00:00Z", {"doubleValue": 0.0})]),
            series(
                "b-other",
                "live-object",
                [("2026-11-04T02:00:00Z", {"doubleValue": 9.0})],
                project="another-project-1",
            ),
            series("B-BAD", "live-object", [("2026-11-04T02:00:00Z", {"doubleValue": 9.0})]),
            series(
                "b-nan", "live-object", [("2026-11-04T02:00:00Z", {"doubleValue": float("nan")})]
            ),
            series("b-neg", "live-object", [("2026-11-04T02:00:00Z", {"doubleValue": -1.0})]),
            series("b-none", "live-object", []),
            {"resource": "not a dict"},
        ],
        P,
    )
    assert got == {
        "b-one": {"live-object": 350, "noncurrent-object": 7},  # newest point, summed over classes
        "b-two": {"live-object": 0},
    }


# ---- bucket-size: the rest -------------------------------------------------------------------


def test_the_threshold_setting_moves_the_flag(run_script, stub_run):
    def flagged(threshold):
        env = run(
            run_script, "bucket_size", stub_run("broken"), settings={"usageThresholdTib": threshold}
        ).envelope
        return {s["name"] for s in env["outputs"]["sizes"]["items"] if s["overThreshold"]}

    assert flagged(0.5) == {"shop-archive"}
    assert flagged(0.1) == {"shop-archive", "shop-backups"}  # 200 GiB is 0.195 TiB
    assert flagged(2) == set()


def test_max_items_limits_the_buckets_whose_size_is_reported(run_script, stub_run):
    env = run(run_script, "bucket_size", stub_run("broken"), settings={"maxItems": 2}).envelope
    assert sorted(items(env, "sizes")) == ["shop-archive", "shop-assets"]  # the first two by name
    totals = env["outputs"]["totals"]
    assert (totals["examined"], totals["total"], totals["omitted"], totals["complete"]) == (
        2,
        6,
        4,
        False,
    )


def test_the_size_budget_matches_the_task_timeout():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "bucket-size")
    assert task["timeout"] == f"{sz.TASK_BUDGET}s"
    assert sz.deadline_from(1000.0) == 1000.0 + sz.TASK_BUDGET - sz.RESERVE


def test_the_size_outputs_fit_their_schemas(run_script, stub_run):
    for scenario in ("broken", "du-fallback"):
        resource = bucket_target("shop-assets") if scenario == "du-fallback" else None
        kwargs = {"resource": resource} if resource else {}
        env = run(run_script, "bucket_size", stub_run(scenario), **kwargs).envelope
        for name, schema_file in (
            ("sizes", "bucket-size.v1.json"),
            ("totals", "size-totals.v1.json"),
        ):
            jsonschema.validate(
                env["outputs"][name], json.loads((SKILL / "schemas" / schema_file).read_text())
            )


# ---- settings, calls and permissions ----------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [{"maxItems": 0}, {"maxItems": "5"}, {"commandTimeout": "soon"}, {"commandTimeout": "0s"}],
)
def test_bad_settings_are_invalid_input_before_any_call(run_script, stub_run, settings):
    stub = stub_run("healthy")
    env = run(run_script, "bucket_config", stub, settings=settings).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"] and stub.log == []


@pytest.mark.parametrize(
    "task, settings",
    [("public_access", {"publicBucketThreshold": -1}), ("public_access", {"publicBucketThreshold": 1.5}),
     ("bucket_size", {"usageThresholdTib": -1}), ("bucket_size", {"usageThresholdTib": "big"}),
     ("bucket_size", {"sizeTimeout": "never"})],
)  # fmt: skip
def test_bad_task_settings_are_invalid_input(run_script, stub_run, task, settings):
    stub = stub_run("healthy")
    env = run(run_script, task, stub, settings=settings).envelope
    assert env["status"] == "failed" and codes(env) == ["invalid_input"] and stub.log == []


@pytest.mark.parametrize("task", ["bucket_config", "public_access", "bucket_size"])
@pytest.mark.parametrize(
    "resource",
    [{"path": "kubernetes/clusters/k/namespaces/n", "type": "kubernetes/namespace", "name": "n"},
     {"path": "gcp/projects/Bad", "type": "gcp/project", "name": "Bad"}],
)  # fmt: skip
def test_another_target_is_invalid_input_before_gcloud_runs(run_script, stub_run, task, resource):
    stub = stub_run("healthy")
    env = run(run_script, task, stub, resource=resource).envelope
    assert codes(env) == ["invalid_input"] and stub.log == []


def test_a_target_that_carries_only_its_path_works(run_script, stub_run):
    stub = stub_run("healthy")
    env = run(run_script, "bucket_config", stub, resource={"path": f"gcp/projects/{P}"}).envelope
    assert env["status"] == "ok" and env["outputs"]["buckets"]["total"] == 3
    path = f"gcp/projects/{P}/buckets/shop-assets"
    env = run(run_script, "bucket_config", stub_run("healthy"), resource={"path": path}).envelope
    assert env["status"] == "ok" and list(items(env, "buckets")) == ["shop-assets"]


# The IAM permissions each kind of call needs (storage list and describe ask for the FULL
# projection, which the JSON API gates on storage.buckets.getIamPolicy:
# https://cloud.google.com/storage/docs/json_api/v1/buckets/list "Required permissions").
NEEDS = {
    ("gcloud", "storage buckets list"): {"storage.buckets.list", "storage.buckets.getIamPolicy"},
    ("gcloud", "storage buckets describe"): {"storage.buckets.get", "storage.buckets.getIamPolicy"},
    ("gcloud", "storage buckets get-iam-policy"): {"storage.buckets.getIamPolicy"},
    ("gcloud", "storage du"): {"storage.objects.list"},
    ("gcloud", "auth print-access-token"): set(),
    ("curl", "monitoring"): {"monitoring.timeSeries.list"},
}


def needed(stub):
    out = set()
    for call in stub.log:
        argv = call["argv"]
        if call["tool"] == "curl":
            key = ("curl", "monitoring")
        else:
            words = [a for a in argv[1:] if not a.startswith(("-", "gs://"))]
            key = ("gcloud", " ".join(words[:3]) if words[:3] in (
                ["storage", "buckets", "list"], ["storage", "buckets", "describe"],
                ["storage", "buckets", "get-iam-policy"]) else " ".join(words[:2]) if words[:2] == ["storage", "du"]
                else " ".join(words[:2]))  # fmt: skip
            assert any(a == f"--billing-project={P}" for a in argv), argv
        out |= NEEDS[key] | {"serviceusage.services.use"}  # every call is billed to the project
    return out


@pytest.mark.parametrize(
    "task, name, runs",
    [
        ("bucket_config", "bucket-config", [("broken", None), ("broken", "shop-assets")]),
        ("public_access", "public-access", [("broken", None), ("broken", "shop-assets")]),
        (
            "bucket_size",
            "bucket-size",
            [("broken", None), ("broken", "shop-assets"), ("du-fallback", "shop-assets")],
        ),
    ],
)
def test_every_call_is_a_read_and_the_declared_permissions_are_exactly_what_they_need(
    run_script, stub_run, task, name, runs
):
    declared_task = next(
        t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"] if t["name"] == name
    )
    declared = {p["permission"] for p in declared_task["permissions"]}
    assert declared_task["readOnly"] is True
    union = set()
    for scenario, bucket in runs:
        stub = stub_run(scenario)
        kwargs = {"resource": bucket_target(bucket)} if bucket else {}
        assert run(run_script, task, stub, **kwargs).envelope["status"] == "ok", (scenario, bucket)
        got = needed(stub)
        assert got <= declared, (scenario, bucket, got - declared)
        if bucket:  # a bucket target never lists the project
            assert "storage.buckets.list" not in got
        union |= got
    # nothing extra is declared: the project target needs list, the bucket target get, and so on
    assert declared == union


def test_the_declared_permissions_are_valid_iam_permission_names():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "gcp-permission.json").read_text())
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    for t in doc["tasks"]:
        assert t["permissions"], t["name"]
        assert {"permission": "serviceusage.services.use"} in t["permissions"], t["name"]
        for permission in t["permissions"]:
            jsonschema.validate(permission, schema)


def test_the_scripts_are_thin():
    for name in ("bucket_config", "public_access", "bucket_size"):
        text = Path(load_script(name, SKILL_NAME).__file__).read_text()
        assert len(text.splitlines()) < 15, name


def test_no_function_here_runs_a_subprocess_or_a_thread_of_its_own():
    # ctx.run is the only way out: a replay (rwtask test) can only shim what goes through it,
    # and no threads keep the order of the calls the same on every run
    import ast

    for path in (SKILL / "lib" / "gcp_bucket_health").glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {
            n.module if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree)
            for a in (n.names if isinstance(n, ast.Import | ast.ImportFrom) else [])
            if isinstance(n, ast.Import | ast.ImportFrom)
        }
        assert not {"threading", "concurrent.futures", "multiprocessing", "asyncio"} & imported, (
            path.name
        )
