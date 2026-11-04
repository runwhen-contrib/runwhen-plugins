"""azure-base/discover against the stub scenarios: facts, the rw.discovery.v1 sync, the push with
the per-run credential, and every failure as a code and not an empty result."""

import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml
from azure_base import budget
from azure_base import discovery as dc
from azure_base import sync
from conftest import SUB, SUBSCRIPTION, SETTINGS
from runwhen_capability.contracts import validate

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
CRED = {
    "apiBaseUrl": "https://papi.example",
    "token": "tok-SECRET",
    "workspace": "ws",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["x"],
}


@pytest.fixture(autouse=True)
def no_sync_credential(monkeypatch):
    monkeypatch.delenv("RW_SYNC_CREDENTIAL", raising=False)
    monkeypatch.delenv("RW_SYNC_ALLOW_HTTP", raising=False)


def discover(run_script, scenario="healthy", **kw):
    return run_script("azure-base", "discover", scenario=scenario, **kw)


def test_it_reports_the_subscription_and_its_vaults(run_script):
    run = discover(run_script)
    assert (run.envelope["status"], run.errors) == ("ok", [])
    out = run.envelope["outputs"]
    assert out["subscription"] == {
        "subscriptionId": SUB,
        "displayName": "rw-pilot-subscription",
        "tenantId": "c0ffee00-1234-4abc-8def-0123456789ab",
        "state": "Enabled",
    }
    assert out["keyvaults"]["total"] == 2 and out["keyvaults"]["omitted"] == 0
    assert [v["name"] for v in out["keyvaults"]["items"]] == ["kv-orders-dev", "kv-orders-prod"]
    assert out["keyvaults"]["items"][1] == {
        "name": "kv-orders-prod",
        "resourceGroup": "rg-orders-prod",
        "location": "eastus",
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.KeyVault/vaults/kv-orders-prod",
    }
    # two az calls, both read-only, both for the target subscription
    assert run.calls == [
        ["account", "show", "--subscription", SUB],
        ["keyvault", "list", "--subscription", SUB],
    ]


def test_the_summary_reports_the_subscription_id_as_the_canonical_key(run_script):
    summary = discover(run_script).envelope["outputs"]["summary"]
    assert summary["counts"] == {"subscription": 1, "keyvault": 2}
    assert summary["partitions"] == {"complete": 1, "failed": 0, "forbidden": 0, "excluded": 0}
    assert summary["root"] == {"canonicalKey": {"name": "subscriptionId", "value": SUB}}
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert types["subscription"]["canonicalKey"] == summary["root"]["canonicalKey"]["name"]
    assert validate("rw.discovery.summary.v1", summary) == []


def test_a_warning_on_stderr_of_a_successful_command_does_not_make_the_run_partial(run_script):
    # the healthy scenario's `az account show` exits 0 with an upgrade warning on stderr
    run = discover(run_script)
    assert run.envelope["status"] == "ok" and run.errors == []


def test_the_sync_artifact_is_a_valid_rw_discovery_v1_document(run_script):
    run = discover(run_script)
    sync_doc = json.loads(run.files["sync.json"])
    assert validate("rw.discovery.v1", sync_doc) == []
    assert sync_doc["open"] == {
        "source": "azure-base/discover",
        "scopePath": f"azure/subscriptions/{SUB}",
        "mode": "full",
    }
    (batch,) = sync_doc["batches"]
    root, dev, prod = batch["items"]
    assert root["identity"] == {
        "platform": "azure",
        "chain": [{"type": "subscription", "name": SUB}],
    }
    assert root["providerUid"] == SUB and root["document"]["state"] == "Enabled"
    assert prod["identity"]["chain"] == [
        {"type": "subscription", "name": SUB},
        {"type": "keyvault", "name": "kv-orders-prod"},
    ]
    assert prod["labels"] == {"env": "prod", "team": "orders"}
    assert dev["document"]["resourceGroup"] == "rg-orders-dev"
    assert sync_doc["commit"]["partitions"] == [
        {
            "type": "keyvault",
            "parentPath": f"azure/subscriptions/{SUB}",
            "status": "complete",
            "count": 2,
        }
    ]
    # the identity chain names types the integration declares
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    for item in batch["items"]:
        for link in item["identity"]["chain"]:
            assert link["type"] in types


def test_max_items_cuts_the_listing_but_never_the_sync(run_script):
    run = discover(run_script, settings={"maxItems": 1})
    keyvaults = run.envelope["outputs"]["keyvaults"]
    assert (len(keyvaults["items"]), keyvaults["total"], keyvaults["omitted"]) == (1, 2, 1)
    assert len(json.loads(run.files["sync.json"])["batches"][0]["items"]) == 3


@pytest.mark.parametrize("bad", [0, 10001, True, "5", 1.5])
def test_a_bad_max_items_is_invalid_input_before_any_az_call(run_script, bad):
    run = discover(run_script, settings={"maxItems": bad})
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


def test_a_target_that_is_not_a_subscription_is_invalid_input(run_script):
    resource = {"path": "azure/subscriptions/x/keyvaults/y", "type": "azure/keyvault", "name": "y"}
    run = discover(run_script, resource=resource)
    assert run.envelope["status"] == "failed" and run.calls == []
    assert run.errors[0]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "scenario,code",
    [("forbidden", "permission_denied"), ("throttled", "rate_limited")],
)
def test_a_refused_listing_fails_with_the_code_and_not_an_empty_list(run_script, scenario, code):
    run = discover(run_script, scenario)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.errors] == [code]
    assert run.envelope["outputs"] == {}
    assert run.files == {}  # no sync artifact for a run that could not look


def test_an_unknown_subscription_is_not_found(run_script):
    resource = {
        "path": "azure/subscriptions/00000000-0000-4000-8000-00000000dead",
        "type": "azure/subscription",
        "name": "00000000-0000-4000-8000-00000000dead",
    }
    run = discover(run_script, "not-found", resource=resource)
    assert run.envelope["status"] == "failed"
    assert [e["code"] for e in run.errors] == ["not_found"]
    assert "not found" in run.errors[0]["message"]


def test_an_account_for_another_subscription_is_internal(run_script):
    from conftest import Replies

    other = json.dumps({"id": "00000000-0000-4000-8000-000000000001", "name": "x", "tenantId": "t"})
    runner = Replies({("account", "show", "--subscription", SUB): (0, other, "")})
    run = discover(run_script, runner=runner)
    assert run.errors[0]["code"] == "internal"


def test_a_subscription_without_vaults_is_a_complete_empty_partition(run_script):
    from conftest import Replies

    account = json.dumps({"id": SUB, "name": "empty", "tenantId": "t", "state": "Enabled"})
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, account, ""),
            ("keyvault", "list", "--subscription", SUB): (0, "[]", ""),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "ok"
    assert run.envelope["outputs"]["keyvaults"] == {"items": [], "total": 0, "omitted": 0}
    assert json.loads(run.files["sync.json"])["commit"]["partitions"][0]["count"] == 0


# --- the push ----------------------------------------------------------------------------------


class Http:
    """Stands in for urllib's opener: records (path, headers, body) and answers like papi."""

    def __init__(self, fail=None, rejected=None, commit_body=None):
        self.requests = []
        self.fail = fail or {}
        self.rejected = rejected or []
        self.commit_body = commit_body or {}

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


def with_credential(tmp_path, monkeypatch):
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(CRED))
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(path))


def test_without_the_credential_nothing_is_sent(run_script, monkeypatch):
    def never(request, timeout=None):
        raise AssertionError("no credential: nothing may be sent")

    monkeypatch.setattr(sync, "default_open", never)
    assert discover(run_script).envelope["status"] == "ok"


def test_with_the_credential_it_opens_sends_the_batches_and_commits(
    run_script, monkeypatch, tmp_path
):
    http = Http()
    with_credential(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "default_open", http)
    run = discover(run_script)
    assert (run.envelope["status"], run.errors) == ("ok", [])
    assert http.paths() == [
        "/resource-syncs",
        "/resource-syncs/s1/items",
        "/resource-syncs/s1/commit",
    ]
    assert http.requests[0][2]["scopePath"] == f"azure/subscriptions/{SUB}"
    assert [i["identity"]["chain"][-1]["type"] for i in http.requests[1][2]["items"]] == [
        "subscription",
        "keyvault",
        "keyvault",
    ]
    assert http.requests[2][2]["summary"]["counts"] == {"subscription": 1, "keyvault": 2}
    assert "tok-SECRET" not in run.files["sync.json"] + json.dumps(run.envelope)


def test_rejected_items_and_a_held_commit_make_the_run_partial(run_script, monkeypatch, tmp_path):
    with_credential(tmp_path, monkeypatch)
    http = Http(
        rejected=[{"index": 1, "code": "bad_name"}],
        commit_body={"status": "held", "heldReason": "mass delete"},
    )
    monkeypatch.setattr(sync, "default_open", http)
    run = discover(run_script)
    assert run.envelope["status"] == "partial"
    assert {e["scope"] for e in run.errors} == {"sync"}
    assert any("mass delete" in e["message"] for e in run.errors)


def test_a_failed_push_fails_the_run_and_aborts_the_sync(run_script, monkeypatch, tmp_path):
    with_credential(tmp_path, monkeypatch)
    http = Http(fail={"/resource-syncs/s1/commit": 500})
    monkeypatch.setattr(sync, "default_open", http)
    monkeypatch.setattr(budget, "budgeted_sleep", lambda deadline: lambda seconds: None)
    run = discover(run_script)
    assert run.envelope["status"] == "failed"
    assert "/resource-syncs/s1/abort" in http.paths()


def test_discovers_timeout_covers_its_az_calls_and_its_push():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    assert task["timeout"] == f"{budget.TASK_BUDGET}s"


# --- the declared task ---------------------------------------------------------------------------


def test_discover_declares_the_connection_and_the_sync_credential_and_reads_only():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    assert doc["runtime"] == "azure@1" and doc["defaults"]["credentials"] == "azure/cli"
    assert task["credentials"] == ["azure/cli", "runwhen/sync"]
    assert task["readOnly"] is True
    assert {k: v["default"] for k, v in doc["settings"].items()} == {
        "maxItems": 200,
        "commandTimeout": "60s",
    }


def test_the_entry_point_names_a_task_that_exists():
    integration = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    skill, _, name = integration["entryPoints"]["discovery"]["task"].partition("/")
    doc = yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())
    assert name in {t["name"] for t in doc["tasks"]}


def test_the_permission_discover_declares_is_the_one_its_az_calls_need():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    task = next(t for t in doc["tasks"] if t["name"] == "discover")
    # az account show reads the local profile; az keyvault list reads vaults at the subscription
    assert task["permissions"] == [{"action": "Microsoft.KeyVault/vaults/read"}]


def test_the_default_settings_in_the_tests_match_the_declared_defaults():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    for name, spec in doc["settings"].items():
        assert SETTINGS[name] == spec["default"]


def test_the_shared_target_in_the_tests_is_the_stub_subscription():
    assert SUBSCRIPTION["name"] == SUB == dc.target_subscription(SUBSCRIPTION)
