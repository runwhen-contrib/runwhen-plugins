"""gcp-bucket-health: bucket-config, public-access and bucket-size, read from the stub's canned
gcloud answers (the same data the fixtures are recorded from), the classic bundle's false-passes,
the permissions each task declares against the gcloud calls it makes, and the bounds."""

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml
from conftest import PLUGIN, SETTINGS, bucket_target, load_script
from gcp_bucket_health import access as ax
from gcp_bucket_health import config as cf
from gcp_bucket_health import size as sz
from gcp_bucket_health.errors import RwError
from gcp_bucket_health.settings import read_settings
from runwhen_capability.testing import make_context

SKILL = PLUGIN / "skills" / "gcp-bucket-health"
P = "shop-prod-1234"
GIB = 1024**3
TIB = 1024**4
SKILL_NAME = "gcp-bucket-health"


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


# ---- bucket-config ----------------------------------------------------------------------------


def test_bucket_config_reports_each_buckets_settings(run_script, stub_run):
    stub = stub_run("healthy")
    env = run(run_script, "bucket_config", stub).envelope
    assert env["status"] == "ok" and env["errors"] == []
    buckets = env["outputs"]["buckets"]
    assert (buckets["total"], buckets["omitted"]) == (3, 0)
    by_name = items(env, "buckets")
    assert list(by_name) == ["shop-assets", "shop-backups", "shop-logs"]  # by name
    assets = by_name["shop-assets"]
    assert assets["versioning"] is True and assets["uniformBucketLevelAccess"] is True
    assert assets["publicAccessPrevention"] == "enforced" and assets["retention"] is None
    assert assets["lifecycleRuleCount"] == 1 and assets["softDeleteSeconds"] == 604800
    assert assets["labels"] == {"env": "prod", "team": "web"} and assets["cmek"] is False
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


def test_bucket_config_on_a_bucket_target_reads_only_that_bucket(run_script, stub_run):
    stub = stub_run("broken")
    env = run(run_script, "bucket_config", stub, resource=bucket_target("shop-legacy")).envelope
    assert env["status"] == "ok" and list(items(env, "buckets")) == ["shop-legacy"]
    assert items(env, "buckets")["shop-legacy"]["uniformBucketLevelAccess"] is False
    assert stub.asked() == [["storage", "buckets", "describe", "gs://shop-legacy", "--raw"]]


def test_the_config_outputs_fit_the_schema(run_script, stub_run):
    env = run(run_script, "bucket_config", stub_run("broken")).envelope
    schema = json.loads((SKILL / "schemas" / "bucket-config.v1.json").read_text())
    jsonschema.validate(env["outputs"]["buckets"], schema)
    assert env["outputs"]["buckets"]["total"] == 6


@pytest.mark.parametrize(
    "scenario, project, code",
    [
        ("forbidden", P, "permission_denied"),
        ("unauthenticated", P, "permission_denied"),
        ("throttled", P, "rate_limited"),
        ("not-found", "no-such-project-1", "not_found"),
    ],
)
@pytest.mark.parametrize("task", ["bucket_config", "public_access", "bucket_size"])
def test_a_failed_bucket_list_fails_every_task_with_the_classified_code(
    run_script, stub_run, task, scenario, project, code
):
    resource = {"path": f"gcp/projects/{project}", "type": "gcp/project", "name": project}
    env = run(run_script, task, stub_run(scenario), resource=resource).envelope
    assert env["status"] == "failed" and codes(env) == [code]
    assert list(env["outputs"]) == []  # nothing is reported as if it were healthy


@pytest.mark.parametrize(
    "scenario, code",
    [
        ("forbidden", "permission_denied"),
        ("throttled", "rate_limited"),
        ("unauthenticated", "permission_denied"),
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


def test_one_unreadable_bucket_among_several_is_partial_and_named(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(
        ["storage", "buckets", "describe", "gs://shop-logs", "--raw"],
        rc=1,
        stderr=stderr_403("storage.buckets.describe", "storage.buckets.get"),
    )
    env = run(run_script, "bucket_config", stub).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "shop-logs")]
    assert list(items(env, "buckets")) == ["shop-assets", "shop-backups"]
    assert env["outputs"]["buckets"]["total"] == 3


def test_every_bucket_unreadable_is_a_failure_not_an_empty_list(run_script, stub_run):
    stub = stub_run("healthy")
    for name in ("shop-assets", "shop-logs", "shop-backups"):
        stub.set(["storage", "buckets", "describe", f"gs://{name}", "--raw"], rc=1,
                 stderr=stderr_403("storage.buckets.describe", "storage.buckets.get"))  # fmt: skip
    env = run(run_script, "bucket_config", stub).envelope
    assert env["status"] == "failed" and set(codes(env)) == {"permission_denied"}


def test_an_empty_project_is_an_empty_list(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(["storage", "buckets", "list", f"--project={P}", "--raw"], stdout="[]\n")
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


def test_max_items_limits_how_many_buckets_are_read(run_script, stub_run):
    stub = stub_run("broken")
    env = run(run_script, "bucket_config", stub, settings={"maxItems": 2}).envelope
    buckets = env["outputs"]["buckets"]
    assert (len(buckets["items"]), buckets["total"], buckets["omitted"]) == (2, 6, 4)
    describes = [a for a in stub.asked() if a[:3] == ["storage", "buckets", "describe"]]
    assert len(describes) == 2, "the buckets beyond maxItems are not even read"


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


# ---- public-access ----------------------------------------------------------------------------


def test_healthy_buckets_have_no_public_grant(run_script, stub_run):
    env = run(run_script, "public_access", stub_run("healthy")).envelope
    assert env["status"] == "ok"
    assert {n: e["publicGrant"] for n, e in items(env, "access").items()} == {
        "shop-assets": False, "shop-backups": False, "shop-logs": False,
    }  # fmt: skip
    assert env["outputs"]["totals"] == {
        "examined": 3, "publicBuckets": 0, "unknown": 0, "threshold": 0, "overThreshold": False,
    }  # fmt: skip


def test_public_grants_are_found_in_iam_and_in_legacy_acls(run_script, stub_run):
    env = run(run_script, "public_access", stub_run("broken")).envelope
    assert env["status"] == "ok"
    by_name = items(env, "access")
    iam = by_name["shop-public-assets"]
    assert (iam["publicGrant"], iam["publicViaIam"], iam["publicViaAcl"]) == (True, True, False)
    assert iam["iamBindings"] == [
        {"role": "roles/storage.objectViewer", "members": ["allUsers"], "conditional": False}
    ]
    legacy = by_name["shop-legacy"]
    assert legacy["uniformBucketLevelAccess"] is False
    assert (legacy["publicViaAcl"], legacy["publicViaIam"]) == (True, True)
    assert legacy["aclEntries"] == [{"entity": "allUsers", "role": "READER"}]
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
    stub.set(
        ["storage", "buckets", "describe", "gs://shop-assets", "--raw"], stdout=json.dumps(legacy)
    )
    stub.set(
        ["storage", "buckets", "get-iam-policy", "gs://shop-assets"], stdout=json.dumps(policy)
    )
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
    stub.set(
        ["storage", "buckets", "describe", "gs://shop-assets", "--raw"], stdout=json.dumps(row)
    )
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    assert items(env, "access")["shop-assets"]["publicViaAcl"] is False


def test_public_access_prevention_makes_a_grant_ineffective_and_says_so(run_script, stub_run):
    stub = stub_run("healthy")
    policy = {"bindings": [{"members": ["allUsers"], "role": "roles/storage.objectViewer"}]}
    stub.set(
        ["storage", "buckets", "get-iam-policy", "gs://shop-assets"], stdout=json.dumps(policy)
    )
    env = run(run_script, "public_access", stub, resource=bucket_target("shop-assets")).envelope
    entry = items(env, "access")["shop-assets"]  # shop-assets has publicAccessPrevention enforced
    assert (entry["publicGrant"], entry["blockedByPrevention"]) == (True, True)


def test_classic_false_pass_an_unreadable_iam_policy_is_unknown_not_private(run_script, stub_run):
    # classic: `get-iam-policy ... 2>/dev/null || echo '{}'` -> a denied read meant "not public"
    env = run(run_script, "public_access", stub_run("partial-iam")).envelope
    assert env["status"] == "partial"
    by_name = items(env, "access")
    assert by_name["shop-assets"]["publicGrant"] is None
    assert by_name["shop-assets"]["publicViaIam"] is None
    assert by_name["shop-legacy"]["publicGrant"] is True  # the ACL grant stands without the policy
    assert sorted((e["code"], e["scope"]) for e in env["errors"]) == sorted(
        ("permission_denied", name) for name in by_name
    )
    totals = env["outputs"]["totals"]
    assert (totals["publicBuckets"], totals["unknown"], totals["examined"]) == (1, 5, 6)


def test_throttling_on_the_policy_read_is_partial_with_rate_limited(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(["storage", "buckets", "get-iam-policy", "gs://shop-logs"], rc=1,
             stderr="ERROR: (gcloud.storage.buckets.get-iam-policy) HTTPError 429: slow down\n")  # fmt: skip
    env = run(run_script, "public_access", stub).envelope
    assert env["status"] == "partial" and codes(env) == ["rate_limited"]
    assert items(env, "access")["shop-logs"]["publicGrant"] is None


def test_a_missing_bucket_on_the_policy_read_still_fails_that_bucket(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(["storage", "buckets", "get-iam-policy", "gs://shop-assets"], rc=1,
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
    assert ax.public_acl_entries({"acl": [{"entity": "allAuthenticatedUsers", "role": "READER"},
                                          {"entity": "user-x@y.z", "role": "OWNER"}]}) == [
        {"entity": "allAuthenticatedUsers", "role": "READER"}
    ]  # fmt: skip


# ---- bucket-size ------------------------------------------------------------------------------


def test_sizes_are_listed_largest_first_with_the_threshold_as_a_boolean(run_script, stub_run):
    stub = stub_run("broken")
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "ok"
    sizes = env["outputs"]["sizes"]["items"]
    assert [s["name"] for s in sizes] == [
        "shop-archive", "shop-backups", "shop-logs", "shop-legacy", "shop-public-assets", "shop-assets",
    ]  # fmt: skip
    assert sizes[0] == {
        "name": "shop-archive", "sizeBytes": int(1.5 * TIB), "sizeTiB": 1.5, "overThreshold": True,
    }  # fmt: skip
    assert [s["overThreshold"] for s in sizes] == [True] + [False] * 5
    assert env["outputs"]["totals"] == {
        "examined": 6, "notMeasured": 0, "thresholdTiB": 0.5, "overThreshold": 1,
        "totalBytes": sum(s["sizeBytes"] for s in sizes),
    }  # fmt: skip


def test_the_threshold_setting_moves_the_flag(run_script, stub_run):
    def flagged(threshold):
        env = run(
            run_script, "bucket_size", stub_run("broken"), settings={"usageThresholdTib": threshold}
        ).envelope
        return {s["name"] for s in env["outputs"]["sizes"]["items"] if s["overThreshold"]}

    assert flagged(0.5) == {"shop-archive"}
    assert flagged(0.1) == {"shop-archive", "shop-backups"}  # 200 GiB is 0.195 TiB
    assert flagged(2) == set()


def test_du_gets_its_own_longer_timeout(run_script, stub_run):
    stub = stub_run("healthy")
    run(run_script, "bucket_size", stub)
    by_verb = {c["asked"][1] if c["asked"][0] == "storage" else "?": c["timeout"] for c in stub.log}
    assert by_verb["buckets"] == 60.0 and by_verb["du"] == 300.0


@pytest.mark.parametrize(
    "text",
    [
        "", "\n", "no total here\n", "12  gs://other-bucket/\n", "abc  gs://shop-assets/\n",
        "-5  gs://shop-assets/\n",
    ],
)  # fmt: skip
def test_classic_false_pass_a_du_without_a_total_is_an_error_never_zero_bytes(text):
    with pytest.raises(RwError) as caught:
        sz.parse_du(text, "shop-assets")
    assert caught.value.code == "internal"


@pytest.mark.parametrize(
    "text",
    ["734003200  gs://shop-assets/\n", "734003200 gs://shop-assets\n", "  734003200\tgs://shop-assets/  \n",
     "100  gs://shop-assets/a.txt\n734003200  gs://shop-assets/\n"],
)  # fmt: skip
def test_du_totals_are_read_from_the_bucket_line(text):
    assert sz.parse_du(text, "shop-assets") == 734003200


def test_a_bucket_whose_du_fails_is_partial_and_the_rest_are_measured(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(["storage", "du", "--summarize", "gs://shop-logs"], rc=1,
             stderr=stderr_403("storage.du", "storage.objects.list"))  # fmt: skip
    env = run(run_script, "bucket_size", stub).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "shop-logs")]
    assert [s["name"] for s in env["outputs"]["sizes"]["items"]] == ["shop-backups", "shop-assets"]


def test_a_du_that_times_out_is_a_timeout_scoped_to_the_bucket(run_script, stub_run):
    class Slow(type(stub_run("healthy"))):
        def __call__(self, argv, *args, **kwargs):
            if argv[1:3] == ["storage", "du"] and argv[-2] == "gs://shop-logs":
                raise subprocess.TimeoutExpired(argv, 300)
            return super().__call__(argv, *args, **kwargs)

    env = run(run_script, "bucket_size", Slow("healthy")).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("timeout", "shop-logs")]


def test_buckets_not_started_before_the_deadline_are_listed_not_hidden(stub_run):
    stub = stub_run("broken")
    resource = {"path": f"gcp/projects/{P}", "type": "gcp/project", "name": P}

    def started():
        return len([a for a in stub.asked() if a[:2] == ["storage", "du"]])

    def clock():  # the deadline is 102: after two buckets the clock reads 100, under the 5s floor
        return 100.0 if started() >= 2 else 0.0

    with make_context(resource=resource, settings=SETTINGS) as tc:
        tc.ctx.run = stub
        st = read_settings(tc.ctx.settings, "usage_threshold_tib", "size_timeout")
        out = sz.run(tc.ctx, st, deadline=102.0, clock=clock)
        errors = list(tc.errors)
    assert out["totals"]["examined"] == 2 and out["totals"]["notMeasured"] == 4
    assert [e["code"] for e in errors] == ["timeout"] and errors[0]["scope"] == "budget"
    assert len(errors[0]["details"]["notMeasured"]) == 4
    assert started() == 2


def test_the_size_budget_matches_the_task_timeout():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "bucket-size")
    assert task["timeout"] == f"{sz.TASK_BUDGET}s"
    assert sz.deadline_from(1000.0) == 1000.0 + sz.TASK_BUDGET - sz.RESERVE


def test_the_size_outputs_fit_their_schemas(run_script, stub_run):
    env = run(run_script, "bucket_size", stub_run("broken")).envelope
    for name, schema_file in (("sizes", "bucket-size.v1.json"), ("totals", "size-totals.v1.json")):
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


READ_VERBS = {
    ("storage", "buckets", "list"): {"storage.buckets.list"},
    ("storage", "buckets", "describe"): {"storage.buckets.get", "storage.buckets.getIamPolicy"},
    ("storage", "buckets", "get-iam-policy"): {"storage.buckets.getIamPolicy"},
    ("storage", "du"): {"storage.objects.list"},
}


def needed(asked):
    out = set()
    for argv in asked:
        key = tuple(a for a in argv[:3] if not a.startswith(("-", "gs://")))
        out |= READ_VERBS[key[:3] if key[:3] in READ_VERBS else key[:2]]
    return out


@pytest.mark.parametrize(
    "task, name, resource",
    [
        ("bucket_config", "bucket-config", None),
        ("public_access", "public-access", None),
        ("bucket_size", "bucket-size", None),
        ("bucket_config", "bucket-config", bucket_target("shop-assets")),
        ("public_access", "public-access", bucket_target("shop-assets")),
        ("bucket_size", "bucket-size", bucket_target("shop-assets")),
    ],
)
def test_every_gcloud_call_is_a_read_and_its_permissions_are_declared(
    run_script, stub_run, task, name, resource
):
    stub = stub_run("broken")
    kwargs = {"resource": resource} if resource else {}
    assert run(run_script, task, stub, **kwargs).envelope["status"] == "ok"
    declared_task = next(
        t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"] if t["name"] == name
    )
    declared = {p["permission"] for p in declared_task["permissions"]}
    assert declared_task["readOnly"] is True
    for argv in stub.asked():
        assert tuple(argv[:3]) in READ_VERBS or tuple(argv[:2]) in READ_VERBS, argv
        assert "--quiet" not in argv and argv[-1].startswith(("--", "gs://", "shop-", P))
    assert needed(stub.asked()) <= declared
    if resource:  # a bucket target never lists the project
        assert "storage.buckets.list" not in needed(stub.asked())
    # nothing extra is declared: the project target needs all of them, a bucket target all but list
    assert declared == needed(stub.asked()) | ({"storage.buckets.list"} if resource else set())


def test_the_declared_permissions_are_valid_iam_permission_names():
    schema = json.loads((PLUGIN / "integration" / "schemas" / "gcp-permission.json").read_text())
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    for t in doc["tasks"]:
        assert t["permissions"], t["name"]
        for permission in t["permissions"]:
            jsonschema.validate(permission, schema)


def test_the_scripts_are_thin():
    for name in ("bucket_config", "public_access", "bucket_size"):
        text = Path(load_script(name, SKILL_NAME).__file__).read_text()
        assert len(text.splitlines()) < 15, name
