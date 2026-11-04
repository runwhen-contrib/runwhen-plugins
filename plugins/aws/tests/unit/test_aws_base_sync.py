"""aws-base/discover pushes its rw.discovery.v1 sync through papi's resource-sync API with the
shared sync client: open, item batches, commit, abort on failure, `held` makes the run partial,
no credential means artifact only. The HTTP side is a stub opener; nothing here touches the
network. (The client itself is tested where it originates, in kubernetes; these tests cover the
wiring in discover.py and the task budget.)"""

import io
import json
import urllib.error

import pytest
import yaml
from aws_base import budget, sync
from conftest import ACCOUNT, PLUGIN
from test_aws_base_discover import answer, listing

CRED = {
    "apiBaseUrl": "https://papi.example",
    "token": "tok-SECRET",
    "workspace": "ws",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["x"],
}


class Http:
    """Stands in for urllib's opener: records (path, headers, body) and answers like papi."""

    def __init__(self, fail=None, rejected=None, commit_body=None):
        self.requests = []
        self.fail = fail or {}
        self.rejected = rejected or []
        self.commit_body = commit_body or {}
        self.timeouts = {}

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/workspaces/ws")[1]
        self.timeouts[path] = timeout
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


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)
    monkeypatch.delenv("RW_SYNC_ALLOW_HTTP", raising=False)


def credential_file(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(CRED))
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(path))


def discover(run_script):
    return run_script("aws-base", "discover", resource=ACCOUNT)


def test_without_the_credential_the_sync_is_only_written_to_the_artifact(
    run_script, fake_aws, monkeypatch
):
    def never(request, timeout=None):
        raise AssertionError("no credential: nothing may be sent")

    monkeypatch.setattr(sync, "default_open", never)
    answer(fake_aws)
    run = discover(run_script)
    assert run.envelope["status"] == "ok" and run.errors == []
    assert json.loads(run.files["sync.json"])["open"]["source"] == "aws-base/discover"


def test_with_the_credential_it_opens_sends_the_batches_and_commits(
    run_script, fake_aws, monkeypatch, tmp_path
):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_aws)
    run = discover(run_script)
    assert run.envelope["status"] == "ok" and run.errors == []
    assert http.paths() == [
        "/resource-syncs",
        "/resource-syncs/s1/items",
        "/resource-syncs/s1/commit",
    ]
    assert http.requests[0][2] == {
        "source": "aws-base/discover",
        "scopePath": "aws/accounts/123456789012",
        "mode": "full",
    }
    assert http.requests[0][1]["Authorization"] == "Bearer tok-SECRET"
    assert [i["identity"]["chain"][-1]["type"] for i in http.requests[1][2]["items"]] == [
        "account", "region", "lambda-function", "lambda-function", "ec2-instance", "region",
    ]  # fmt: skip
    commit = http.requests[2][2]
    assert [(p["type"], p["status"]) for p in commit["partitions"]] == [
        ("region", "complete"),
        ("lambda-function", "complete"),
        ("ec2-instance", "complete"),
        ("lambda-function", "complete"),
        ("ec2-instance", "complete"),
    ]
    assert json.loads(run.files["sync.json"])["commit"]["summary"] == commit["summary"]
    assert "tok-SECRET" not in run.files["sync.json"] + json.dumps(run.envelope)


def test_a_held_commit_makes_the_run_partial_and_the_summary_is_built_after_it(
    run_script, fake_aws, monkeypatch, tmp_path
):
    http = Http(commit_body={"status": "held", "heldReason": "would delete 40% of the scope"})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_aws)
    env_ = discover(run_script).envelope
    assert env_["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env_["errors"]] == [("internal", "sync")]
    assert [e["scope"] for e in env_["outputs"]["summary"]["errors"]] == ["sync"]
    assert env_["outputs"]["summary"]["root"]["canonicalKey"]["value"] == "123456789012"


def test_a_failed_push_fails_the_task_and_hides_the_token(
    run_script, fake_aws, monkeypatch, tmp_path
):
    http = Http(fail={"/resource-syncs/s1/commit": 403})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_aws, {"eu-west-1": listing("a")})
    run = discover(run_script)
    assert run.envelope["status"] == "failed"
    assert http.paths()[-1] == "/resource-syncs/s1/abort"
    assert "tok-SECRET" not in json.dumps(run.envelope)


def test_with_no_budget_left_the_push_fails_as_a_timeout_before_sending_anything(
    run_script, fake_aws, monkeypatch, tmp_path
):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    monkeypatch.setattr(budget, "TASK_BUDGET", budget.MARGIN)
    answer(fake_aws)
    env_ = discover(run_script).envelope
    assert env_["status"] == "failed" and [e["code"] for e in env_["errors"]] == ["timeout"]
    assert http.requests == []


def test_the_commit_timeout_is_clamped_to_what_is_left_of_the_task_budget(
    run_script, fake_aws, monkeypatch, tmp_path
):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    monkeypatch.setattr(budget, "TASK_BUDGET", 100)
    answer(fake_aws)
    assert discover(run_script).envelope["status"] == "ok"
    assert http.timeouts["/resource-syncs/s1/commit"] <= 90


def test_discovers_timeout_covers_its_calls_and_its_push():
    doc = yaml.safe_load((PLUGIN / "skills" / "aws-base" / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    assert task["timeout"] == f"{budget.TASK_BUDGET}s"


def test_a_rejected_function_item_fails_exactly_its_own_regions_partition(
    run_script, fake_aws, monkeypatch, tmp_path
):
    # items: account(0) eu-west-1(1) orders-api(2) report-batch(3) us-east-1(4) thumbnailer(5)
    http = Http(rejected=[{"index": 2, "code": "parent_missing"}])
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_aws, {"us-east-1": listing("thumbnailer", region="us-east-1")})
    env_ = discover(run_script).envelope
    commit = http.requests[-1][2]
    by_region = {
        p["parentPath"].rsplit("/", 1)[-1]: p
        for p in commit["partitions"]
        if p["type"] == "lambda-function"
    }
    # the region that holds the rejected item is failed (nothing under it is swept); the other
    # region and the region partition itself stand
    assert by_region["eu-west-1"]["status"] == "failed"
    assert "parent_missing" in by_region["eu-west-1"]["reason"]
    assert "count" not in by_region["eu-west-1"]
    assert by_region["us-east-1"]["status"] == "complete" and by_region["us-east-1"]["count"] == 1
    assert [p["status"] for p in commit["partitions"] if p["type"] == "region"] == ["complete"]
    assert env_["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env_["errors"]] == [("internal", "sync")]
