"""pg-base/discover pushes its rw.discovery.v1 sync through papi's resource-sync API with the
shared sync client: open, item batches, commit, abort on failure, `held` makes the run partial,
no credential means artifact only. The HTTP side is a stub opener, as in the k8s-base tests;
nothing here touches the network."""

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest
import yaml
from pg_base import discovery as dc
from pg_base import sync
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
K8S_SYNC = PLUGIN.parent / "kubernetes" / "skills" / "k8s-base" / "lib" / "k8s_base" / "sync.py"
SID = "7704057543498637352"
CRED = {
    "apiBaseUrl": "https://papi.example",
    "token": "tok-SECRET",
    "workspace": "ws",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["x"],
}


# --- the shared file: a cross-plugin copy -------------------------------------------------------
# sync.py is copied from kubernetes/skills/k8s-base/lib/k8s_base/sync.py. Plugins do not import each
# other (a task sees only its own skill's lib/), so this is a copy and not an import. The one line
# that names the skill's own package (`from <pkg>.errors import RwError`) is the only difference.


def own_package_import(text, package):
    return text.replace(f"from {package}.errors import RwError", "from <pkg>.errors import RwError")


def test_sync_py_is_the_k8s_base_copy_but_for_its_one_errors_import():
    ours = (SKILLS / "pg-base" / "lib" / "pg_base" / "sync.py").read_text()
    theirs = K8S_SYNC.read_text()
    assert "from pg_base.errors import RwError" in ours
    assert "from k8s_base.errors import RwError" in theirs
    assert own_package_import(ours, "pg_base") == own_package_import(theirs, "k8s_base"), (
        "pg-base's sync.py drifted from k8s-base's: re-copy it, changing only the errors import"
    )
    assert "k8s" not in ours  # the copy imports nothing across plugins


# --- the discover script ------------------------------------------------------------------------


class Http:
    """Stands in for urllib's opener: records (path, headers, body) and answers like papi."""

    def __init__(self, fail=None, rejected=None, commit_body=None, raise_on=None):
        self.requests = []
        self.fail = fail or {}
        self.rejected = rejected or []
        self.commit_body = commit_body or {}
        self.raise_on = raise_on or {}

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/workspaces/ws")[1]
        sent = json.loads(request.data)
        self.requests.append((path, request.headers, sent))
        if path in self.raise_on:
            raise self.raise_on[path]
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


def db(name):
    return {
        "name": name,
        "owner": "postgres",
        "encoding": "UTF8",
        "sizeAllowed": True,
        "sizeBytes": 1000,
    }


def answer(fake, names=("orders", "postgres")):
    fake.on(
        "current_setting('server_version')",
        stdout=json.dumps(
            {"version": "16.15", "versionNum": 160015, "inRecovery": False, "currentUser": "rw"}
        ),
    )
    fake.on("FROM pg_control_system()", stdout=json.dumps({"systemIdentifier": SID}))
    fake.on("FROM pg_database d", stdout=json.dumps([db(n) for n in names]))


@pytest.fixture(autouse=True)
def libpq_env(monkeypatch):
    monkeypatch.setenv("PGHOST", "orders-db.shop.svc")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)
    monkeypatch.delenv("RW_SYNC_ALLOW_HTTP", raising=False)


def credential_file(tmp_path, monkeypatch, value=None):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(value or CRED))
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(path))


def test_discover_declares_the_sync_credential_beside_the_connection():
    doc = yaml.safe_load((SKILLS / "pg-base" / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    assert task["credentials"] == ["postgres/libpq", "runwhen/sync"]
    assert task["readOnly"] is True
    check = next(t for t in doc["tasks"] if t["name"] == "check-permissions")
    assert "credentials" not in check  # only discover pushes


def test_without_the_credential_the_sync_is_only_written_to_the_artifact(
    run_script, fake_psql, monkeypatch
):
    def never(request, timeout=None):
        raise AssertionError("no credential: nothing may be sent")

    monkeypatch.setattr(sync, "default_open", never)
    answer(fake_psql)
    run = run_script("discover")
    assert run.envelope["status"] == "ok" and run.errors == []
    assert json.loads(run.files["sync.json"])["open"]["source"] == "pg-base/discover"


def test_with_the_credential_it_opens_sends_the_batches_and_commits(
    run_script, fake_psql, monkeypatch, tmp_path
):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql)
    run = run_script("discover")
    assert run.envelope["status"] == "ok" and run.errors == []
    assert http.paths() == [
        "/resource-syncs",
        "/resource-syncs/s1/items",
        "/resource-syncs/s1/commit",
    ]
    opened = http.requests[0][2]
    assert opened == {
        "source": "pg-base/discover",
        "scopePath": "postgres/instances/orders-db",  # the task's target path
        "mode": "full",
    }
    assert http.requests[0][1]["Authorization"] == "Bearer tok-SECRET"
    sent_items = http.requests[1][2]["items"]
    assert [i["identity"]["chain"][-1]["type"] for i in sent_items] == [
        "instance",
        "database",
        "database",
    ]
    commit = http.requests[2][2]
    assert commit["partitions"] == [
        {
            "type": "database",
            "parentPath": "postgres/instances/orders-db",
            "status": "complete",
            "count": 2,
        }
    ]
    assert commit["summary"]["counts"] == {"instance": 1, "database": 2}
    # the artifact is the same document, and the token is in neither it nor the result
    artifact = json.loads(run.files["sync.json"])
    assert artifact["commit"]["summary"] == commit["summary"]
    assert "tok-SECRET" not in run.files["sync.json"] + json.dumps(run.envelope)


def test_the_credential_may_be_inline_json(run_script, fake_psql, monkeypatch):
    http = Http()
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", "  " + json.dumps(CRED))
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql)
    assert run_script("discover").envelope["status"] == "ok"
    assert http.paths()[0] == "/resource-syncs"


def test_more_than_500_items_go_in_ordered_batches(run_script, fake_psql, monkeypatch, tmp_path):
    http = Http()
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql, [f"db{i:04d}" for i in range(1000)])
    run = run_script("discover")
    assert run.envelope["status"] == "ok"
    batches = [b for p, _, b in http.requests if p.endswith("/items")]
    assert [b["seq"] for b in batches] == [1, 2, 3] and sum(
        len(b["items"]) for b in batches
    ) == 1001
    assert http.requests[-1][2]["partitions"][0]["count"] == 1000


def test_a_held_commit_makes_the_run_partial_and_the_summary_is_built_after_it(
    run_script, fake_psql, monkeypatch, tmp_path
):
    http = Http(commit_body={"status": "held", "heldReason": "would delete 40% of the scope"})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql)
    run = run_script("discover")
    env = run.envelope
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("internal", "sync")]
    assert "would delete 40%" in env["errors"][0]["message"]
    summary = env["outputs"]["summary"]
    assert [e["scope"] for e in summary["errors"]] == ["sync"]
    assert validate("rw.discovery.summary.v1", summary) == []
    assert "errors" not in json.loads(run.files["sync.json"])["commit"]["summary"]  # sent before


def test_a_rejected_item_fails_its_partition_before_commit_and_shows_in_the_summary(
    run_script, fake_psql, monkeypatch, tmp_path
):
    http = Http(rejected=[{"index": 1, "code": "parent_missing"}])
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql)
    env = run_script("discover").envelope
    sent = http.requests[-1][2]["partitions"]
    assert [(p["type"], p["status"]) for p in sent] == [("database", "failed")]
    assert "count" not in sent[0] and "parent_missing" in sent[0]["reason"]
    assert env["status"] == "partial"
    assert [e["scope"] for e in env["outputs"]["summary"]["errors"]] == ["sync"]
    assert env["outputs"]["summary"]["partitions"] == {
        "complete": 0,
        "failed": 1,
        "forbidden": 0,
        "excluded": 0,
    }


def test_a_failed_push_aborts_the_open_sync_fails_the_task_and_hides_the_token(
    run_script, fake_psql, monkeypatch, tmp_path
):
    http = Http(fail={"/resource-syncs/s1/commit": 403})
    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    answer(fake_psql)
    run = run_script("discover")
    env = run.envelope
    assert env["status"] == "failed" and env["errors"][0]["code"] == "permission_denied"
    assert http.paths()[-1] == "/resource-syncs/s1/abort"
    assert "tok-SECRET" not in json.dumps(env)
    assert "sync.json" in run.files  # the artifact was written before the push


def test_an_unreachable_platform_fails_with_unreachable(
    run_script, fake_psql, monkeypatch, tmp_path
):
    def down(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", down)

    class NoSleep(sync.SyncClient):  # the retries back off 1s, 4s, 10s
        def __init__(self, credential, opener=None, sleep=None):
            super().__init__(credential, opener, lambda seconds: None)

    monkeypatch.setattr(sync, "SyncClient", NoSleep)
    answer(fake_psql)
    env = run_script("discover").envelope
    assert env["status"] == "failed" and env["errors"][0]["code"] == "unreachable"


@pytest.mark.parametrize("value", ["not json", '{"token": "tok-SECRET"}', "/no/such/file"])
def test_a_bad_credential_is_invalid_input_and_never_echoed(
    run_script, fake_psql, monkeypatch, value
):
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", value)
    answer(fake_psql)
    env = run_script("discover").envelope
    assert env["status"] == "failed" and env["errors"][0]["code"] == "invalid_input"
    assert "tok-SECRET" not in json.dumps(env) and value not in json.dumps(env)


def test_the_api_is_https_only_unless_the_test_switch_is_on(monkeypatch):
    http_cred = {**CRED, "apiBaseUrl": "http://papi.example"}
    with pytest.raises(Exception) as exc:
        sync.SyncClient(http_cred)
    assert exc.value.code == "invalid_input"
    monkeypatch.setenv("RW_SYNC_ALLOW_HTTP", "1")
    sync.SyncClient(http_cred)


def test_the_commit_carries_the_summary_the_discover_built():
    # discovery.summarize is what the script calls a second time after the push
    parts = dc.database_partitions("postgres/instances/x", 3)
    summary = dc.summarize({"instance": 1, "database": 3}, parts, [], 12, SID)
    assert validate("rw.discovery.summary.v1", summary) == []
    assert summary["partitions"]["complete"] == 1
    assert re.fullmatch(r"\d+", summary["root"]["canonicalKey"]["value"])
