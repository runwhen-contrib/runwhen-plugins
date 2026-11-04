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
from conftest import SETTINGS, SUB, SUBSCRIPTION, Replies
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


GROUPS = ["rg-orders-dev", "rg-orders-prod", "RG-Platform"]  # as `az group list` spells them
GROUP_PATH = f"azure/subscriptions/{SUB}/resourcegroups"


def group_calls(group):
    """The az calls made for one resource group, in the order of dc.CHILD_TYPES."""
    return [
        ["keyvault", "list", "-g", group, "--resource-type", "vault", "--subscription", SUB],
        ["aks", "list", "-g", group, "--subscription", SUB],
        ["appservice", "plan", "list", "-g", group, "--subscription", SUB],
        ["webapp", "list", "-g", group, "--subscription", SUB],
        ["functionapp", "list", "-g", group, "--subscription", SUB],
    ]


def empty_apps(group):
    """Replies for the plan and web app listings of a group that holds none."""
    return {tuple(call): (0, "[]", "") for call in group_calls(group)[2:]}


def test_it_reports_the_subscription_its_groups_and_its_vaults(run_script):
    run = discover(run_script)
    assert (run.envelope["status"], run.errors) == ("ok", [])
    out = run.envelope["outputs"]
    assert out["subscription"] == {
        "subscriptionId": SUB,
        "displayName": "rw-pilot-subscription",
        "tenantId": "c0ffee00-1234-4abc-8def-0123456789ab",
        "state": "Enabled",
    }
    assert out["resourceGroups"]["total"] == 3
    assert [(g["name"], g["keyvaults"]) for g in out["resourceGroups"]["items"]] == [
        ("rg-orders-dev", 1),
        ("rg-orders-prod", 1),
        ("RG-Platform", 0),
    ]
    assert out["keyvaults"]["total"] == 2 and out["keyvaults"]["omitted"] == 0
    assert [v["name"] for v in out["keyvaults"]["items"]] == ["kv-orders-dev", "kv-orders-prod"]
    assert out["keyvaults"]["items"][1] == {
        "name": "kv-orders-prod",
        "resourceGroup": "rg-orders-prod",
        "location": "eastus",
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.KeyVault/vaults/kv-orders-prod",
    }
    assert out["aksClusters"]["total"] == 2 and out["aksClusters"]["omitted"] == 0
    assert [c["name"] for c in out["aksClusters"]["items"]] == ["aks-orders-dev", "aks-orders-prod"]
    assert out["aksClusters"]["items"][1] == {
        "name": "aks-orders-prod",
        "resourceGroup": "rg-orders-prod",
        "location": "eastus",
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.ContainerService/managedClusters/aks-orders-prod",
    }
    assert [(g["name"], g["aksClusters"]) for g in out["resourceGroups"]["items"]] == [
        ("rg-orders-dev", 1),
        ("rg-orders-prod", 1),
        ("RG-Platform", 0),
    ]
    assert [
        (g["name"], g["appServicePlans"], g["webApps"]) for g in out["resourceGroups"]["items"]
    ] == [
        ("rg-orders-dev", 1, 2),
        ("rg-orders-prod", 1, 2),
        ("RG-Platform", 0, 0),
    ]
    assert out["appServicePlans"]["total"] == 2 and out["webApps"]["total"] == 4
    assert [(p["name"], p["skuName"], p["skuTier"]) for p in out["appServicePlans"]["items"]] == [
        ("plan-orders-dev", "S2", "Standard"),
        ("plan-orders-prod", "P1v3", "PremiumV3"),
    ]
    # a function app is a web app of kind functionapp, listed with the others
    assert [(a["name"], a["kind"], a["state"]) for a in out["webApps"]["items"]] == [
        ("app-orders-api", "app,linux", "Running"),
        ("app-orders-web", "app", "Stopped"),
        ("func-orders-ingest", "functionapp", "Running"),
        ("func-orders-jobs", "functionapp,linux", "Running"),
    ]
    # the account, the groups, then per group the vault, cluster, plan, web app and function app
    # listings, each scoped with -g
    assert run.calls == [
        ["account", "show", "--subscription", SUB],
        ["group", "list", "--subscription", SUB],
        *(call for g in GROUPS for call in group_calls(g)),
    ]


def test_the_summary_reports_the_subscription_id_as_the_canonical_key(run_script):
    summary = discover(run_script).envelope["outputs"]["summary"]
    assert summary["counts"] == {
        "subscription": 1,
        "resourcegroup": 3,
        "keyvault": 2,
        "aks-cluster": 2,
        "app-service-plan": 2,
        "web-app": 4,
    }
    # the groups, then a partition per child type for each of the three groups
    assert summary["partitions"] == {"complete": 13, "failed": 0, "forbidden": 0, "excluded": 0}
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
    (
        root,
        dev,
        dev_vault,
        dev_aks,
        dev_plan,
        dev_web,
        dev_func,
        prod,
        prod_vault,
        prod_aks,
        *rest,
    ) = batch["items"]
    prod_plan, prod_web, prod_func, platform = rest
    assert root["identity"] == {
        "platform": "azure",
        "chain": [{"type": "subscription", "name": SUB}],
    }
    assert root["providerUid"] == SUB and root["document"]["state"] == "Enabled"
    sub = {"type": "subscription", "name": SUB}
    assert prod_vault["identity"]["chain"] == [
        sub,
        {"type": "resourcegroup", "name": "rg-orders-prod"},
        {"type": "keyvault", "name": "kv-orders-prod"},
    ]
    assert prod_vault["labels"] == {"env": "prod", "team": "orders"}
    assert dev_vault["document"]["resourceGroup"] == "rg-orders-dev"
    assert prod_aks["identity"]["chain"] == [
        sub,
        {"type": "resourcegroup", "name": "rg-orders-prod"},
        {"type": "aks-cluster", "name": "aks-orders-prod"},
    ]
    assert prod_aks["labels"] == {"env": "prod", "team": "orders"}
    assert dev_aks["document"] == {
        "name": "aks-orders-dev",
        "resourceGroup": "rg-orders-dev",
        "location": "westeurope",
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-orders-dev/providers/Microsoft.ContainerService/managedClusters/aks-orders-dev",
    }
    assert prod_plan["identity"]["chain"][1:] == [
        {"type": "resourcegroup", "name": "rg-orders-prod"},
        {"type": "app-service-plan", "name": "plan-orders-prod"},
    ]
    assert prod_plan["document"] == {
        "name": "plan-orders-prod",
        "resourceGroup": "rg-orders-prod",
        "location": "East US",
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-orders-prod/providers/Microsoft.Web/serverfarms/plan-orders-prod",
        "kind": "linux",
        "skuName": "P1v3",
        "skuTier": "PremiumV3",
    }
    assert dev_web["identity"]["chain"][2:] == [{"type": "web-app", "name": "app-orders-web"}]
    assert dev_web["document"]["state"] == "Stopped" and dev_web["document"]["kind"] == "app"
    assert dev_func["identity"]["chain"][2:] == [{"type": "web-app", "name": "func-orders-ingest"}]
    assert prod_func["document"]["kind"] == "functionapp,linux"
    for item in (dev_web, dev_func, prod_web, prod_func, dev_plan):
        # nothing but the listed facts: no site object, no settings, no host names
        assert "properties" not in item["document"] and "appSettings" not in str(item)
    # the group's name is lower case in the identity, as ARM spells it in the display name
    assert platform["identity"]["chain"] == [sub, {"type": "resourcegroup", "name": "rg-platform"}]
    assert platform["displayName"] == "RG-Platform"
    assert platform["document"]["name"] == "RG-Platform"
    assert platform["providerUid"] == f"/subscriptions/{SUB}/resourceGroups/RG-Platform"
    assert sync_doc["commit"]["partitions"] == [
        {
            "type": "resourcegroup",
            "parentPath": f"azure/subscriptions/{SUB}",
            "status": "complete",
            "count": 3,
        },
        *(
            {
                "type": type_name,
                "parentPath": f"{GROUP_PATH}/{group}",
                "status": "complete",
                "count": count,
            }
            for group, counts in (
                ("rg-orders-dev", (1, 1, 1, 2)),
                ("rg-orders-prod", (1, 1, 1, 2)),
                ("rg-platform", (0, 0, 0, 0)),
            )
            for type_name, count in zip(
                ("keyvault", "aks-cluster", "app-service-plan", "web-app"), counts
            )
        ),
    ]
    # the identity chain names types the integration declares, each below the right parent
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    for item in batch["items"]:
        chain = item["identity"]["chain"]
        for link in chain:
            assert link["type"] in types
        for parent, child in zip(chain, chain[1:]):
            assert types[child["type"]]["parent"] == parent["type"]


def test_the_group_partitions_use_the_plural_the_types_file_declares():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    assert dc.GROUP_PLURAL == types["resourcegroup"]["plural"]
    assert types["resourcegroup"]["parent"] == "subscription"
    assert types["keyvault"]["parent"] == "resourcegroup"
    assert types["aks-cluster"]["parent"] == "resourcegroup"
    assert [t for t, _ in dc.CHILD_TYPES] == [
        "keyvault",
        "aks-cluster",
        "app-service-plan",
        "web-app",
    ]
    assert all(t in types for t, _ in dc.CHILD_TYPES)
    # every type below the resource group has its discover row
    below = {t for t, spec in types.items() if spec.get("parent") == "resourcegroup"}
    assert below == {t for t, _ in dc.CHILD_TYPES}


def test_max_items_cuts_the_listings_but_never_the_sync(run_script):
    run = discover(run_script, settings={"maxItems": 1})
    keyvaults = run.envelope["outputs"]["keyvaults"]
    assert (len(keyvaults["items"]), keyvaults["total"], keyvaults["omitted"]) == (1, 2, 1)
    groups = run.envelope["outputs"]["resourceGroups"]
    assert (len(groups["items"]), groups["total"], groups["omitted"]) == (1, 3, 2)
    assert len(json.loads(run.files["sync.json"])["batches"][0]["items"]) == 14


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
    assert run.calls[-1][:2] == ["group", "list"]


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
    other = json.dumps({"id": "00000000-0000-4000-8000-000000000001", "name": "x", "tenantId": "t"})
    runner = Replies({("account", "show", "--subscription", SUB): (0, other, "")})
    run = discover(run_script, runner=runner)
    assert run.errors[0]["code"] == "internal"


ACCOUNT = json.dumps({"id": SUB, "name": "s", "tenantId": "t", "state": "Enabled"})


def group_row(name):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{name}",
        "name": name,
        "location": "eastus",
        "properties": {"provisioningState": "Succeeded"},
        "tags": {},
    }


def vault_row(name, group):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.KeyVault/vaults/{name}",
        "name": name,
        "location": "eastus",
        "tags": {},
    }


def list_argv(group):
    return ("keyvault", "list", "-g", group, "--resource-type", "vault", "--subscription", SUB)


def aks_argv(group):
    return ("aks", "list", "-g", group, "--subscription", SUB)


def web_row(name, group, kind="app"):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Web/sites/{name}",
        "name": name,
        "location": "eastus",
        "kind": kind,
        "state": "Running",
        "tags": {},
    }


def aks_row(name, group):
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.ContainerService/managedClusters/{name}",
        "name": name,
        "location": "eastus",
        "tags": {},
    }


def test_a_subscription_without_groups_is_a_complete_empty_partition(run_script):
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, "[]", ""),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "ok"
    assert run.envelope["outputs"]["keyvaults"] == {"items": [], "total": 0, "omitted": 0}
    assert json.loads(run.files["sync.json"])["commit"]["partitions"] == [
        {
            "type": "resourcegroup",
            "parentPath": f"azure/subscriptions/{SUB}",
            "status": "complete",
            "count": 0,
        }
    ]


def test_a_group_without_vaults_is_a_complete_empty_partition(run_script):
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
            list_argv("rg-a"): (0, "[]", ""),
            aks_argv("rg-a"): (0, "[]", ""),
            **empty_apps("rg-a"),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "ok"
    keyvault, aks, plans, webs = json.loads(run.files["sync.json"])["commit"]["partitions"][1:]
    assert (plans["type"], webs["type"]) == ("app-service-plan", "web-app")
    assert keyvault == {
        "type": "keyvault",
        "parentPath": f"{GROUP_PATH}/rg-a",
        "status": "complete",
        "count": 0,
    }
    assert aks == {**keyvault, "type": "aks-cluster"}


def test_a_group_whose_vaults_cannot_be_listed_is_forbidden_and_the_others_stand(run_script):
    run = discover(run_script, "group-forbidden")
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [
        ("permission_denied", "keyvault:rg-orders-dev")
    ]
    sync_doc = json.loads(run.files["sync.json"])
    partitions = {
        p["parentPath"]: p for p in sync_doc["commit"]["partitions"] if p["type"] == "keyvault"
    }
    # the group's cluster partition is its own: the vault refusal does not touch it
    clusters = {
        p["parentPath"]: p for p in sync_doc["commit"]["partitions"] if p["type"] == "aks-cluster"
    }
    assert {p["status"] for p in clusters.values()} == {"complete"} and len(clusters) == 3
    assert [c["name"] for c in run.envelope["outputs"]["aksClusters"]["items"]] == [
        "aks-orders-dev",
        "aks-orders-prod",
    ]
    # nor the plan and web app partitions of the group
    for type_name in ("app-service-plan", "web-app"):
        refused = [p["status"] for p in sync_doc["commit"]["partitions"] if p["type"] == type_name]
        assert refused == ["complete"] * 3
    dev = partitions[f"{GROUP_PATH}/rg-orders-dev"]
    assert (
        dev["status"] == "forbidden"
        and "count" not in dev
        and "Microsoft.KeyVault/vaults/read" in dev["reason"]
    )
    assert partitions[f"{GROUP_PATH}/rg-orders-prod"]["status"] == "complete"
    out = run.envelope["outputs"]
    assert [v["name"] for v in out["keyvaults"]["items"]] == ["kv-orders-prod"]
    groups = {g["name"]: g["keyvaults"] for g in out["resourceGroups"]["items"]}
    assert groups["rg-orders-dev"] is None and groups["rg-orders-prod"] == 1
    clusters_of = {g["name"]: g["aksClusters"] for g in out["resourceGroups"]["items"]}
    assert clusters_of == {"rg-orders-dev": 1, "rg-orders-prod": 1, "RG-Platform": 0}
    assert out["summary"]["partitions"] == {
        "complete": 12,  # the groups and every partition but the one vault listing refused
        "failed": 0,
        "forbidden": 1,
        "excluded": 0,
    }
    assert validate("rw.discovery.summary.v1", out["summary"]) == []
    assert validate("rw.discovery.v1", json.loads(run.files["sync.json"])) == []


def test_a_group_whose_clusters_cannot_be_listed_keeps_its_vaults_and_is_forbidden_for_clusters(
    run_script,
):
    """The two listings of a group are independent: a refused `az aks list -g` is a forbidden
    aks-cluster partition (nothing under it is swept) and a scoped error, and the group's vaults,
    listed first, are complete and in the sync."""
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
            list_argv("rg-a"): (0, json.dumps([vault_row("kv-a-vault", "rg-a")]), ""),
            aks_argv("rg-a"): (
                1,
                "",
                "ERROR: (AuthorizationFailed) The client does not have authorization to perform "
                "action 'Microsoft.ContainerService/managedClusters/read'.\n",
            ),
            **empty_apps("rg-a"),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [
        ("permission_denied", "aks-cluster:rg-a")
    ]
    partitions = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert [(p["type"], p["status"]) for p in partitions] == [
        ("resourcegroup", "complete"),
        ("keyvault", "complete"),
        ("aks-cluster", "forbidden"),
        ("app-service-plan", "complete"),
        ("web-app", "complete"),
    ]
    out = run.envelope["outputs"]
    assert [v["name"] for v in out["keyvaults"]["items"]] == ["kv-a-vault"]
    assert out["aksClusters"] == {"items": [], "total": 0, "omitted": 0}
    assert out["resourceGroups"]["items"][0]["aksClusters"] is None  # not listed, not zero


def test_a_group_whose_plans_or_web_apps_cannot_be_listed_is_forbidden_for_that_type_only(
    run_script,
):
    """The web app type is read by two commands (`az webapp list`, `az functionapp list`) and is
    complete only when both are: one refused makes the whole partition forbidden."""
    denied = "ERROR: (AuthorizationFailed) The client does not have authorization to perform action 'Microsoft.Web/sites/read'.\n"
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
            list_argv("rg-a"): (0, "[]", ""),
            aks_argv("rg-a"): (0, "[]", ""),
            tuple(group_calls("rg-a")[2]): (0, "[]", ""),
            tuple(group_calls("rg-a")[3]): (0, json.dumps([web_row("app-a", "rg-a")]), ""),
            tuple(group_calls("rg-a")[4]): (1, "", denied),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("permission_denied", "web-app:rg-a")]
    partitions = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert [(p["type"], p["status"]) for p in partitions][-2:] == [
        ("app-service-plan", "complete"),
        ("web-app", "forbidden"),
    ]
    out = run.envelope["outputs"]
    # the app the first command found is not listed as a complete listing: the group says unknown
    assert out["resourceGroups"]["items"][0]["webApps"] is None
    assert out["resourceGroups"]["items"][0]["appServicePlans"] == 0
    assert out["webApps"]["total"] == 0


def test_a_function_app_is_listed_once_and_a_web_app_listing_that_is_not_a_list_is_an_error(
    run_script,
):
    both = json.dumps([web_row("app-a", "rg-a"), web_row("func-a", "rg-a", kind="functionapp")])
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
            list_argv("rg-a"): (0, "[]", ""),
            aks_argv("rg-a"): (0, "[]", ""),
            tuple(group_calls("rg-a")[2]): (0, "[]", ""),
            tuple(group_calls("rg-a")[3]): (0, both, ""),
            tuple(group_calls("rg-a")[4]): (
                0,
                json.dumps([web_row("func-a", "rg-a", kind="functionapp")]),
                "",
            ),
        }
    )
    out = discover(run_script, runner=runner).envelope["outputs"]
    assert [(a["name"], a["kind"]) for a in out["webApps"]["items"]] == [
        ("app-a", "app"),
        ("func-a", "functionapp"),
    ]
    runner.answers[tuple(group_calls("rg-a")[3])] = (0, '{"value": []}', "")
    run = discover(run_script, runner=runner)
    assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "web-app:rg-a")]


def test_aks_list_returning_anything_but_a_list_or_a_nameless_cluster_is_an_error(run_script):
    for reply in ('{"value": []}', json.dumps([{"location": "eastus"}])):
        runner = Replies(
            {
                ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
                ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
                list_argv("rg-a"): (0, "[]", ""),
                aks_argv("rg-a"): (0, reply, ""),
                **empty_apps("rg-a"),
            }
        )
        run = discover(run_script, runner=runner)
        assert run.envelope["status"] == "partial"
        assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "aks-cluster:rg-a")]


def test_a_group_listing_that_fails_for_another_reason_is_a_failed_partition(run_script):
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (
                0,
                json.dumps([group_row("rg-a"), group_row("rg-b")]),
                "",
            ),
            list_argv("rg-a"): (
                1,
                "",
                "ERROR: (TooManyRequests) The request is being throttled.\n",
            ),
            list_argv("rg-b"): (0, json.dumps([vault_row("kv-b-vault", "rg-b")]), ""),
            aks_argv("rg-a"): (0, "[]", ""),
            aks_argv("rg-b"): (0, "[]", ""),
            **empty_apps("rg-a"),
            **empty_apps("rg-b"),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "partial"
    states = {
        p["parentPath"].rsplit("/", 1)[-1]: p["status"]
        for p in json.loads(run.files["sync.json"])["commit"]["partitions"]
        if p["type"] == "keyvault"
    }
    assert states == {"rg-a": "failed", "rg-b": "complete"}
    assert run.errors[0]["code"] == "rate_limited"


def test_the_listing_budget_stops_further_groups_and_leaves_them_failed_not_swept():
    from types import SimpleNamespace

    answers = {
        ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
        ("group", "list", "--subscription", SUB): (
            0,
            json.dumps([group_row(n) for n in ("rg-a", "rg-b", "rg-c")]),
            "",
        ),
        list_argv("rg-a"): (0, json.dumps([vault_row("kv-a-vault", "rg-a")]), ""),
        aks_argv("rg-a"): (0, json.dumps([aks_row("aks-a", "rg-a")]), ""),
        **empty_apps("rg-a"),
    }
    errors = []
    ctx = SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s", max_items=200),
        resource={"path": f"azure/subscriptions/{SUB}", "type": "azure/subscription", "name": SUB},
        run=Replies(answers),
        errors=errors,
        error=lambda code, message, scope=None: errors.append(
            {"code": code, "message": message, "scope": scope}
        ),
    )
    ticks = iter(
        [0, 1, 400, 400, 401]
    )  # start, rg-a ok, rg-b and rg-c past the budget, the summary
    outputs, sync_doc = dc.discover(ctx, 200, clock=lambda: next(ticks), budget=300)
    states = {
        p["parentPath"].rsplit("/", 1)[-1]: p
        for p in sync_doc["commit"]["partitions"]
        if p["type"] == "keyvault"
    }
    assert states["rg-a"]["status"] == "complete"
    assert states["rg-b"]["status"] == states["rg-c"]["status"] == "failed"
    assert "budget" in states["rg-b"]["reason"]
    # the budget is spent between groups: a group is listed whole, both types, or not at all
    assert [(e["code"], e["scope"]) for e in errors] == [
        ("timeout", "keyvault"),
        ("timeout", "aks-cluster"),
        ("timeout", "app-service-plan"),
        ("timeout", "web-app"),
    ]
    assert ctx.run.calls[-1] == ["functionapp", "list", "-g", "rg-a", "--subscription", SUB]
    assert outputs["summary"]["partitions"]["failed"] == 8  # rg-b and rg-c, a partition per type
    assert {
        p["status"] for p in sync_doc["commit"]["partitions"] if p["type"] == "aks-cluster"
    } == {"complete", "failed"}


def test_a_rejected_vault_fails_exactly_its_resource_groups_partition():
    """The sync client keys a partition by its parent chain: an item the platform rejects fails the
    partition of ITS group, not the group of the same name elsewhere, not the sibling groups."""
    partitions = [
        {
            "type": "resourcegroup",
            "parentPath": f"azure/subscriptions/{SUB}",
            "status": "complete",
            "count": 2,
        },
        {"type": "keyvault", "parentPath": f"{GROUP_PATH}/rg-a", "status": "complete", "count": 1},
        {"type": "keyvault", "parentPath": f"{GROUP_PATH}/rg-b", "status": "complete", "count": 1},
    ]
    root = {"type": "subscription", "name": SUB}

    def item(*links):
        return {"identity": {"platform": "azure", "chain": [root, *links]}}

    items = [
        item({"type": "resourcegroup", "name": "rg-a"}),
        item({"type": "resourcegroup", "name": "rg-a"}, {"type": "keyvault", "name": "kv-a-vault"}),
        item({"type": "resourcegroup", "name": "rg-b"}),
        item({"type": "resourcegroup", "name": "rg-b"}, {"type": "keyvault", "name": "kv-b-vault"}),
    ]
    rejected = [(*sync._key(items[1]), {"index": 1, "code": "bad_name"})]
    out = sync.fail_rejected(partitions, rejected)
    assert [p["status"] for p in out] == ["complete", "failed", "complete"]
    assert out[1]["reason"] == "items rejected: bad_name" and "count" not in out[1]
    # and a rejected group item fails the groups partition, not any vault partition
    out = sync.fail_rejected(partitions, [(*sync._key(items[0]), {"index": 0, "code": "x"})])
    assert [p["status"] for p in out] == ["failed", "complete", "complete"]


def test_a_rejected_vault_through_the_whole_push_fails_only_its_partition(
    run_script, monkeypatch, tmp_path
):
    with_credential(tmp_path, monkeypatch)
    # the platform refuses the 9th item (index 8 of the batch): the prod vault
    http = Http(rejected=[{"index": 8, "code": "bad_name"}])
    monkeypatch.setattr(sync, "default_open", http)
    run = discover(run_script)
    assert run.envelope["status"] == "partial"
    (_, _, commit) = http.requests[-1]
    states = {
        (p["type"], p["parentPath"].rsplit("/", 1)[-1]): p["status"] for p in commit["partitions"]
    }
    assert states[("keyvault", "rg-orders-prod")] == "failed"
    assert [k for k, v in states.items() if v == "failed"] == [("keyvault", "rg-orders-prod")]
    assert len(states) == 13


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
        "resourcegroup",
        "keyvault",
        "aks-cluster",
        "app-service-plan",
        "web-app",
        "web-app",
        "resourcegroup",
        "keyvault",
        "aks-cluster",
        "app-service-plan",
        "web-app",
        "web-app",
        "resourcegroup",
    ]
    assert http.requests[2][2]["summary"]["counts"] == {
        "subscription": 1,
        "resourcegroup": 3,
        "keyvault": 2,
        "aks-cluster": 2,
        "app-service-plan": 2,
        "web-app": 4,
    }
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
    # az account show reads the local profile; az group list reads the groups of the subscription;
    # az keyvault list -g reads the vaults of one group, az aks list -g its clusters, az appservice
    # plan list -g its plans and az webapp|functionapp list -g its sites. Nothing else: without -g az would search the subscription's resources
    # (Microsoft.Resources/subscriptions/resources/read)
    assert task["permissions"] == [
        {"action": "Microsoft.Resources/subscriptions/resourceGroups/read"},
        {"action": "Microsoft.KeyVault/vaults/read"},
        {"action": "Microsoft.ContainerService/managedClusters/read"},
        {"action": "Microsoft.Web/serverfarms/read"},
        {"action": "Microsoft.Web/sites/read"},
    ]


def test_the_default_settings_in_the_tests_match_the_declared_defaults():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    for name, spec in doc["settings"].items():
        assert SETTINGS[name] == spec["default"]


def test_the_shared_target_in_the_tests_is_the_stub_subscription():
    assert SUBSCRIPTION["name"] == SUB == dc.target_subscription(SUBSCRIPTION)
