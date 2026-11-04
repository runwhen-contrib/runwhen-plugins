"""gcp-base/discover: the project and its buckets as facts and as an rw.discovery.v1 sync, read from
the stub's canned gcloud answers; the push through papi's resource-sync API with the shared sync
client (an HTTP stand-in, nothing touches the network)."""

import io
import json
import urllib.error

import jsonschema
import pytest
import yaml
from conftest import PLUGIN, bucket_target
from gcp_base import discovery as dc
from gcp_base import sync
from runwhen_capability.contracts import validate

SKILL = PLUGIN / "skills" / "gcp-base"
P = "shop-prod-1234"
LIST = ["storage", "buckets", "list", f"--project={P}", "--raw", f"--billing-project={P}"]
DESCRIBE = ["projects", "describe", P, f"--billing-project={P}"]
FLIST = ["functions", "list", f"--project={P}", f"--billing-project={P}"]
CRED = {
    "apiBaseUrl": "https://papi.example",
    "token": "tok-SECRET",
    "workspace": "ws",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["x"],
}


def discover(run_script, stub, **kwargs):
    return run_script("discover", skill="gcp-base", stub=stub, **kwargs)


def test_a_healthy_project_is_listed_with_its_facts_and_canonical_key(run_script, stub_run):
    run = discover(run_script, stub_run("healthy"))
    env = run.envelope
    assert env["status"] == "ok" and run.errors == []
    out = env["outputs"]
    assert out["project"] == {
        "projectId": P,
        "projectNumber": "123456789012",
        "displayName": "Shop Production",
        "lifecycleState": "ACTIVE",
    }  # Resource Manager v1, which `gcloud projects describe` reads today
    assert [b["name"] for b in out["buckets"]["items"]] == [
        "shop-assets",
        "shop-backups",
        "shop-logs",
    ]
    assert (out["buckets"]["total"], out["buckets"]["omitted"]) == (3, 0)
    assert out["buckets"]["items"][1] == {
        "name": "shop-backups",
        "location": "US",
        "locationType": "multi-region",
        "storageClass": "COLDLINE",
        "created": "2025-03-14T09:21:05.123000+00:00",  # gcloud's own rendering of timeCreated
    }
    summary = out["summary"]
    assert summary["counts"] == {"project": 1, "bucket": 3, "cloud-function": 3}
    assert summary["root"] == {"canonicalKey": {"name": "projectId", "value": P}}
    assert validate("rw.discovery.summary.v1", summary) == []
    assert out["functions"]["read"] is True and out["functions"]["total"] == 3
    assert out["functions"]["items"][0] == {
        "name": "orders-api",
        "region": "us-central1",
        "generation": 2,
        "state": "ACTIVE",
        "runtime": "python312",
        "updateTime": "2026-10-30T14:02:11.123456Z",
    }


def test_the_outputs_fit_their_schemas_and_the_sync_fits_the_contract(run_script, stub_run):
    run = discover(run_script, stub_run("broken"))
    out = run.envelope["outputs"]
    for name in ("project", "buckets", "functions"):
        schema = json.loads((SKILL / "schemas" / f"{name}.v1.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(out[name], schema)
    document = json.loads(run.files["sync.json"])
    assert validate("rw.discovery.v1", document) == []
    items = [i for b in document["batches"] for i in b["items"]]
    assert [i["identity"]["chain"][-1] for i in items][:2] == [
        {"type": "project", "name": P},
        {"type": "bucket", "name": "shop-assets"},
    ]
    assert items[0]["providerUid"] == "123456789012" and len(items) == 1 + 6 + 5
    assert document["commit"]["partitions"] == [
        {"type": "bucket", "parentPath": f"gcp/projects/{P}", "status": "complete", "count": 6},
        {
            "type": "cloud-function",
            "parentPath": f"gcp/projects/{P}",
            "status": "complete",
            "count": 5,
        },
    ]
    functions = {
        i["identity"]["chain"][-1]["name"]: i
        for i in items
        if i["identity"]["chain"][-1]["type"] == "cloud-function"
    }
    assert set(functions) == {
        "orders-api",
        "checkout",
        "thumbnailer",
        "legacy-cron",
        "nightly-report",
    }
    assert functions["checkout"]["document"] == {
        "region": "us-central1",
        "generation": 2,
        "state": "FAILED",
        "runtime": "python312",
        "updateTime": "2026-10-30T14:02:11.123456Z",
    }
    assert functions["nightly-report"]["document"]["state"] == "OFFLINE"  # 1st gen: `status`
    assert functions["nightly-report"]["document"]["generation"] == 1
    assert functions["orders-api"]["labels"] == {"deployment-tool": "console", "team": "web"}
    assert functions["checkout"]["identity"]["chain"][0] == {"type": "project", "name": P}
    labelled = next(i for i in items if i["displayName"] == "shop-assets")
    assert labelled["labels"] == {"env": "prod", "team": "web"}


def test_the_calls_are_the_three_read_only_gcloud_commands_with_the_credential_env(
    run_script, stub_run
):
    stub = stub_run("healthy")
    discover(run_script, stub)
    assert stub.asked() == [LIST, DESCRIBE, FLIST]
    assert all(c["timeout"] == 60.0 for c in stub.log)
    # every call is billed to the project and runs with the ambient credentials neutralised
    for call in stub.log:
        assert f"--billing-project={P}" in call["argv"]
        assert (
            call["env"]["CLOUDSDK_AUTH_ACCESS_TOKEN"] == ""
            and call["env"]["CLOUDSDK_CORE_ACCOUNT"] == ""
        )


def test_max_items_cuts_the_listing_but_not_the_sync(run_script, stub_run):
    run = discover(run_script, stub_run("broken"), settings={"maxItems": 2})
    listing = run.envelope["outputs"]["buckets"]
    assert (len(listing["items"]), listing["total"], listing["omitted"]) == (2, 6, 4)
    functions = run.envelope["outputs"]["functions"]
    assert (len(functions["items"]), functions["total"], functions["omitted"]) == (2, 5, 3)
    assert len(json.loads(run.files["sync.json"])["batches"][0]["items"]) == 12


@pytest.mark.parametrize("bad", [0, 10001, True, "5"])
def test_a_bad_max_items_is_invalid_input_before_any_call(run_script, stub_run, bad):
    stub = stub_run("healthy")
    run = discover(run_script, stub, settings={"maxItems": bad})
    assert run.envelope["status"] == "failed" and stub.log == []
    assert run.envelope["errors"][0]["code"] == "invalid_input"


def test_a_bad_command_timeout_is_invalid_input(run_script, stub_run):
    run = discover(run_script, stub_run("healthy"), settings={"commandTimeout": "soon"})
    assert run.envelope["errors"][0]["code"] == "invalid_input"


def test_a_project_the_identity_may_not_read_still_lists_its_buckets_partially(
    run_script, stub_run
):
    run = discover(run_script, stub_run("project-denied"))
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("permission_denied", "project")]
    assert env["outputs"]["project"] == {
        "projectId": P,
        "projectNumber": None,
        "displayName": None,
        "lifecycleState": None,
    }
    assert env["outputs"]["buckets"]["total"] == 6 and env["outputs"]["functions"]["total"] == 5
    assert env["outputs"]["summary"]["root"]["canonicalKey"]["value"] == P
    assert [e["scope"] for e in env["outputs"]["summary"]["errors"]] == ["project"]
    document = json.loads(run.files["sync.json"])
    root = document["batches"][0]["items"][0]
    assert "providerUid" not in root and root["displayName"] == P


@pytest.mark.parametrize(
    "scenario, project, code",
    [
        ("forbidden", P, "permission_denied"),
        ("unauthenticated", P, "permission_denied"),
        ("throttled", P, "rate_limited"),
        ("api-disabled", P, "dependency"),
    ],
)
def test_a_failed_listing_fails_the_task_with_the_classified_code(
    run_script, stub_run, scenario, project, code
):
    resource = {"path": f"gcp/projects/{project}", "type": "gcp/project", "name": project}
    run = discover(run_script, stub_run(scenario), resource=resource)
    env = run.envelope
    assert env["status"] == "failed" and "buckets" not in env["outputs"]
    assert [e["code"] for e in env["errors"]] == [code]
    assert "ERROR: (gcloud." in env["errors"][0]["message"]


def test_discover_refuses_a_bucket_target(run_script, stub_run):
    stub = stub_run("healthy")
    run = discover(run_script, stub, resource=bucket_target("shop-assets"))
    assert run.envelope["errors"][0]["code"] == "invalid_input" and stub.log == []


def test_an_empty_project_has_an_empty_list_and_a_complete_partition(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(LIST, stdout="[]\n")
    run = discover(run_script, stub)
    out = run.envelope["outputs"]
    assert out["buckets"] == {"items": [], "total": 0, "omitted": 0}
    assert json.loads(run.files["sync.json"])["commit"]["partitions"][0]["count"] == 0


@pytest.mark.parametrize("stdout", ["", "{}", '[{"location": "US"}]', "[1]"])
def test_a_listing_that_is_not_a_list_of_named_buckets_is_an_error(run_script, stub_run, stdout):
    stub = stub_run("healthy")
    stub.set(LIST, stdout=stdout)
    run = discover(run_script, stub)
    assert run.envelope["status"] == "failed" and run.envelope["errors"][0]["code"] == "internal"


# ---- the functions: a second type, read after the buckets --------------------------------------


@pytest.mark.parametrize(
    "scenario, code, status",
    [
        ("functions-discover-denied", "permission_denied", "forbidden"),
        ("functions-discover-disabled", "dependency", "failed"),
    ],
)
def test_a_functions_list_that_fails_leaves_the_buckets_and_a_scoped_error(
    run_script, stub_run, scenario, code, status
):
    run = discover(run_script, stub_run(scenario))
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [(code, "functions")]
    out = env["outputs"]
    assert out["buckets"]["total"] == 3
    # not an empty list: unread, with its counts null, and its partition is not complete
    assert out["functions"] == {
        "read": False, "items": [], "total": None, "omitted": None,
        "unreachableRegions": [], "ambiguousIds": [],
    }  # fmt: skip
    assert out["summary"]["counts"] == {"project": 1, "bucket": 3}  # no cloud-function count
    parts = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert parts[1] == {
        "type": "cloud-function",
        "parentPath": f"gcp/projects/{P}",
        "status": status,
        "count": 0,
    }
    assert validate("rw.discovery.v1", json.loads(run.files["sync.json"])) == []


def test_regions_gcloud_could_not_reach_make_the_functions_partition_failed(run_script, stub_run):
    run = discover(run_script, stub_run("functions-discover-unreachable"))
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("unreachable", "functions")]
    assert "asia-east1, southamerica-east1" in env["errors"][0]["message"]
    functions = env["outputs"]["functions"]
    assert functions["read"] is True and functions["total"] == 3
    assert functions["unreachableRegions"] == ["asia-east1", "southamerica-east1"]
    parts = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert (parts[1]["status"], parts[1]["count"]) == ("failed", 3)  # nothing is swept for it


def test_one_id_in_two_regions_is_reported_and_not_synced(run_script, stub_run):
    run = discover(run_script, stub_run("functions-discover-split"))
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("internal", "functions")]
    assert "orders-api" in env["errors"][0]["message"]
    functions = env["outputs"]["functions"]
    assert functions["ambiguousIds"] == ["orders-api"] and functions["total"] == 3
    document = json.loads(run.files["sync.json"])
    names = [
        i["identity"]["chain"][-1]["name"]
        for b in document["batches"]
        for i in b["items"]
        if i["identity"]["chain"][-1]["type"] == "cloud-function"
    ]
    assert names == ["legacy-cron"]  # the two orders-api rows are not resources
    assert document["commit"]["partitions"][1]["status"] == "failed"


def test_an_empty_functions_list_is_a_complete_partition_of_zero(run_script, stub_run):
    stub = stub_run("healthy")
    stub.set(FLIST, stdout="[]\n")
    run = discover(run_script, stub)
    assert run.envelope["status"] == "ok"
    assert run.envelope["outputs"]["functions"]["total"] == 0
    parts = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert parts[1] == {
        "type": "cloud-function",
        "parentPath": f"gcp/projects/{P}",
        "status": "complete",
        "count": 0,
    }


@pytest.mark.parametrize("stdout", ["", "{}", "[1]", '[{"name": "nope"}]'])
def test_a_functions_list_of_an_unknown_shape_is_a_scoped_error_never_an_empty_list(
    run_script, stub_run, stdout
):
    stub = stub_run("healthy")
    stub.set(FLIST, stdout=stdout)
    env = discover(run_script, stub).envelope
    assert env["status"] == "partial" and env["outputs"]["functions"]["read"] is False
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("internal", "functions")]


# ---- the project resource: partial for the reasons that leave the buckets useful ---------------


def project_error(command_text, rc=1):
    return {"rc": rc, "stderr": f"ERROR: (gcloud.projects.describe) {command_text}\n"}


@pytest.mark.parametrize(
    "stderr, code",
    [
        ("HTTPError 403: The caller does not have permission", "permission_denied"),
        ("HTTPError 403: Cloud Resource Manager API has not been used in project 1 before or it is disabled. SERVICE_DISABLED", "dependency"),
        ("RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Read requests'", "rate_limited"),
        ("Project 'shop-prod-1234' not found or deleted.", "not_found"),
    ],
)  # fmt: skip
def test_a_project_describe_that_cannot_be_had_leaves_the_buckets_and_a_scoped_error(
    run_script, stub_run, stderr, code
):
    stub = stub_run("healthy")
    stub.set(DESCRIBE, **project_error(stderr))
    run = discover(run_script, stub)
    env = run.envelope
    assert env["status"] == "partial" and env["outputs"]["buckets"]["total"] == 3
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [(code, "project")]
    assert env["outputs"]["project"]["projectNumber"] is None
    assert [e["scope"] for e in env["outputs"]["summary"]["errors"]] == ["project"]


@pytest.mark.parametrize(
    "stderr",
    [
        "something odd happened",
        "HTTPError 400: Invalid argument",
        "Failed to establish a new connection",
    ],
)
def test_any_other_project_failure_still_fails_the_task(run_script, stub_run, stderr):
    stub = stub_run("healthy")
    stub.set(DESCRIBE, **project_error(stderr))
    assert discover(run_script, stub).envelope["status"] == "failed"


V1 = {
    "projectNumber": "123456789012",
    "projectId": P,
    "name": "Shop Production",
    "lifecycleState": "ACTIVE",
}
V3 = {
    "name": "projects/123456789012",
    "projectId": P,
    "displayName": "Shop Production",
    "state": "ACTIVE",
}
FACTS = {
    "projectId": P,
    "projectNumber": "123456789012",
    "displayName": "Shop Production",
    "lifecycleState": "ACTIVE",
}


@pytest.mark.parametrize("row", [V1, V3], ids=["v1", "v3"])
def test_both_resource_manager_shapes_are_read(run_script, stub_run, row):
    stub = stub_run("healthy")
    stub.set(DESCRIBE, stdout=json.dumps(row))
    env = discover(run_script, stub).envelope
    assert env["status"] == "ok" and env["outputs"]["project"] == FACTS


@pytest.mark.parametrize("row", [{}, {"projectId": P, "labels": {}}, {"name": "projects/1"}, []])
def test_a_project_of_an_unknown_shape_is_a_scoped_error_never_silent_nulls(
    run_script, stub_run, row
):
    stub = stub_run("healthy")
    stub.set(DESCRIBE, stdout=json.dumps(row))
    env = discover(run_script, stub).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("internal", "project")]
    assert "unknown shape" in env["errors"][0]["message"]
    assert (
        env["outputs"]["project"]["projectNumber"] is None
        and env["outputs"]["buckets"]["total"] == 3
    )


def test_the_project_facts_of_the_real_gcloud_output_are_v1(stub_run):
    stub = stub_run("healthy")
    assert dc.project_facts(json.loads((stub.directory / "project.json").read_text())) == (
        "123456789012", "Shop Production", "ACTIVE",
    )  # fmt: skip


# ---- the push ---------------------------------------------------------------------------------


class Http:
    """Stands in for urllib's opener: records (path, headers, body) and answers like papi."""

    def __init__(self, fail=None, rejected=None, commit_body=None):
        self.requests, self.fail = [], fail or {}
        self.rejected, self.commit_body = rejected or [], commit_body or {}

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/workspaces/ws")[1]
        sent = json.loads(request.data)
        self.requests.append((path, request.headers, sent))
        if path in self.fail:
            raise urllib.error.HTTPError(
                request.full_url, self.fail[path], "x", {}, io.BytesIO(b'{"detail":"nope"}')
            )
        body = {}
        if path == "/resource-syncs":
            body = {"syncId": "s1", "leaseExpiresAt": "2030-01-01T00:00:00Z"}
        elif path.endswith("/items"):
            body = {
                "seq": sent["seq"],
                "accepted": len(sent["items"]) - len(self.rejected),
                "rejected": self.rejected,
            }
        elif path.endswith("/commit"):
            body = {"status": "committed", "counts": {}, **self.commit_body}
        return io.BytesIO(json.dumps(body).encode())

    def paths(self):
        return [p for p, _, _ in self.requests]


def credential_file(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(CRED))
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(path))


def test_without_the_credential_nothing_is_sent(run_script, stub_run, monkeypatch):
    def never(request, timeout=None):
        raise AssertionError("no credential: nothing may be sent")

    monkeypatch.setattr(sync, "default_open", never)
    run = discover(run_script, stub_run("healthy"))
    assert run.envelope["status"] == "ok"
    assert json.loads(run.files["sync.json"])["open"]["source"] == "gcp-base/discover"


def test_with_the_credential_it_opens_sends_the_batches_and_commits(
    run_script, stub_run, monkeypatch, tmp_path
):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    run = discover(run_script, stub_run("healthy"))
    assert run.envelope["status"] == "ok" and run.errors == []
    assert http.paths() == [
        "/resource-syncs",
        "/resource-syncs/s1/items",
        "/resource-syncs/s1/commit",
    ]
    assert http.requests[0][2] == {
        "source": "gcp-base/discover",
        "scopePath": f"gcp/projects/{P}",
        "mode": "full",
    }
    assert http.requests[0][1]["Authorization"] == "Bearer tok-SECRET"
    assert [i["identity"]["chain"][-1]["type"] for i in http.requests[1][2]["items"]] == [
        "project", "bucket", "bucket", "bucket",
        "cloud-function", "cloud-function", "cloud-function",
    ]  # fmt: skip
    assert http.requests[2][2]["summary"]["counts"] == {
        "project": 1,
        "bucket": 3,
        "cloud-function": 3,
    }
    assert "tok-SECRET" not in run.files["sync.json"] + json.dumps(run.envelope)


def test_a_held_commit_makes_the_run_partial_and_the_summary_is_built_after_it(
    run_script, stub_run, monkeypatch, tmp_path
):
    http = Http(commit_body={"status": "held", "heldReason": "would delete 40% of the scope"})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    env = discover(run_script, stub_run("healthy")).envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("internal", "sync")]
    summary = env["outputs"]["summary"]
    assert [e["scope"] for e in summary["errors"]] == ["sync"]
    assert summary["root"]["canonicalKey"]["value"] == P
    assert validate("rw.discovery.summary.v1", summary) == []


def test_a_failed_push_fails_the_task_and_hides_the_token(
    run_script, stub_run, monkeypatch, tmp_path
):
    http = Http(fail={"/resource-syncs/s1/commit": 403})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    monkeypatch.setattr(sync, "BACKOFF", ())
    run = discover(run_script, stub_run("healthy"))
    assert run.envelope["status"] == "failed"
    assert "tok-SECRET" not in json.dumps(run.envelope)
    assert http.paths()[-1] == "/resource-syncs/s1/abort"


# ---- the task declares what it needs ----------------------------------------------------------


def test_discover_declares_the_sync_credential_the_permissions_and_a_budget():
    from gcp_base import budget

    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    assert doc["runtime"] == "gcp@1" and doc["defaults"]["credentials"] == "gcp/adc"
    assert task["credentials"] == ["gcp/adc", "runwhen/sync"] and task["readOnly"] is True
    assert task["permissions"] == [
        {"permission": "storage.buckets.list"},
        {"permission": "storage.buckets.getIamPolicy"},  # the list asks for the FULL projection
        {"permission": "resourcemanager.projects.get"},
        {"permission": "cloudfunctions.functions.list"},
        {"permission": "cloudfunctions.locations.list"},  # the 1st gen half of the list
        {"permission": "serviceusage.services.use"},  # --billing-project on every call
    ]
    assert task["timeout"] == f"{budget.TASK_BUDGET}s"


def test_the_helper_blanks_the_variable_for_the_marker_the_auth_script_writes():
    import importlib.util

    from gcp_base import gcloud

    spec = importlib.util.spec_from_file_location(
        "gcp_auth", PLUGIN / "integration" / "auth" / "cloud-identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert gcloud.MARKER_TYPE == module.MARKER_TYPE
    assert json.loads(module.marker("a@b.c"))["type"] == gcloud.MARKER_TYPE


def test_the_summarizer_counts_partitions():
    parts = [{"type": "bucket", "parentPath": "p", "status": "failed"}]
    summary = dc.summarize({"project": 1}, parts, [], 5, P)
    assert summary["partitions"] == {"complete": 0, "failed": 1, "forbidden": 0, "excluded": 0}
