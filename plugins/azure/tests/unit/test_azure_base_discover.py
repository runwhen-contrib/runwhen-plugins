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
    assert out["virtualMachines"]["total"] == 3 and out["vmScaleSets"]["total"] == 2
    assert [(v["name"], v["osType"]) for v in out["virtualMachines"]["items"]] == [
        ("vm-orders-batch02", "Linux"),
        ("vm-orders-web01", "Linux"),
        ("vm-platform-legacy", "Linux"),
    ]
    assert [v["name"] for v in out["vmScaleSets"]["items"]] == [
        "vmss-orders-api",
        "vmss-orders-jobs",
    ]
    assert [
        (g["name"], g["virtualMachines"], g["vmScaleSets"]) for g in out["resourceGroups"]["items"]
    ] == [("rg-orders-dev", 1, 1), ("rg-orders-prod", 1, 1), ("RG-Platform", 1, 0)]
    # the account, the groups, then in each group one listing per child type, each scoped with -g
    assert run.calls == [
        ["account", "show", "--subscription", SUB],
        ["group", "list", "--subscription", SUB],
        *(call for g in GROUPS for call in list_calls(g)),
    ]


def test_the_summary_reports_the_subscription_id_as_the_canonical_key(run_script):
    summary = discover(run_script).envelope["outputs"]["summary"]
    assert summary["counts"] == {
        "subscription": 1,
        "resourcegroup": 3,
        "keyvault": 2,
        "aks-cluster": 2,
        "virtual-machine": 3,
        "vm-scale-set": 2,
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
    (root, dev, dev_vault, dev_aks, dev_vm, dev_vmss, prod, prod_vault, prod_aks, prod_vm,
     prod_vmss, platform, platform_vm) = batch["items"]  # fmt: skip
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
    assert prod_vm["identity"]["chain"][1:] == [
        {"type": "resourcegroup", "name": "rg-orders-prod"},
        {"type": "virtual-machine", "name": "vm-orders-web01"},
    ]
    assert prod_vm["document"]["osType"] == "Linux" and "osType" not in prod_vault["document"]
    assert prod_vmss["identity"]["chain"][-1] == {"type": "vm-scale-set", "name": "vmss-orders-api"}
    assert dev_vm["labels"] == {"env": "dev", "team": "orders"}
    assert dev_vmss["displayName"] == "vmss-orders-jobs"
    assert platform_vm["identity"]["chain"][1:] == [
        {"type": "resourcegroup", "name": "rg-platform"},
        {"type": "virtual-machine", "name": "vm-platform-legacy"},
    ]
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
                ("rg-orders-dev", (1, 1, 1, 1)),
                ("rg-orders-prod", (1, 1, 1, 1)),
                ("rg-platform", (0, 0, 1, 0)),
            )
            for type_name, count in zip(
                ("keyvault", "aks-cluster", "virtual-machine", "vm-scale-set"), counts
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
        "virtual-machine",
        "vm-scale-set",
    ]
    assert types["virtual-machine"]["plural"] == "virtual-machines"
    assert types["vm-scale-set"]["plural"] == "vm-scale-sets"
    assert all(t in types for t, _ in dc.CHILD_TYPES)


def test_max_items_cuts_the_listings_but_never_the_sync(run_script):
    run = discover(run_script, settings={"maxItems": 1})
    keyvaults = run.envelope["outputs"]["keyvaults"]
    assert (len(keyvaults["items"]), keyvaults["total"], keyvaults["omitted"]) == (1, 2, 1)
    groups = run.envelope["outputs"]["resourceGroups"]
    assert (len(groups["items"]), groups["total"], groups["omitted"]) == (1, 3, 2)
    assert len(json.loads(run.files["sync.json"])["batches"][0]["items"]) == 13
    assert run.envelope["outputs"]["virtualMachines"]["omitted"] == 2


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


def no_compute(*groups):
    """Answers for the virtual machine and scale set listings of `groups`: none there."""
    return {
        (*cmd, "-g", g, "--subscription", SUB): (0, "[]", "")
        for g in groups
        for cmd in (("vm", "list"), ("vmss", "list"))
    }


def list_calls(group):
    """The four listings made in one resource group, in order."""
    return [
        ["keyvault", "list", "-g", group, "--resource-type", "vault", "--subscription", SUB],
        ["aks", "list", "-g", group, "--subscription", SUB],
        ["vm", "list", "-g", group, "--subscription", SUB],
        ["vmss", "list", "-g", group, "--subscription", SUB],
    ]


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
            **no_compute("rg-a"),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "ok"
    keyvault, aks, vm, vmss = json.loads(run.files["sync.json"])["commit"]["partitions"][1:]
    assert keyvault == {
        "type": "keyvault",
        "parentPath": f"{GROUP_PATH}/rg-a",
        "status": "complete",
        "count": 0,
    }
    assert aks == {**keyvault, "type": "aks-cluster"}
    assert vm == {**keyvault, "type": "virtual-machine"}
    assert vmss == {**keyvault, "type": "vm-scale-set"}


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
        "complete": 12,  # the groups and a partition per type in each group, but one
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
            **no_compute("rg-a"),
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
        ("virtual-machine", "complete"),
        ("vm-scale-set", "complete"),
    ]
    out = run.envelope["outputs"]
    assert [v["name"] for v in out["keyvaults"]["items"]] == ["kv-a-vault"]
    assert out["aksClusters"] == {"items": [], "total": 0, "omitted": 0}
    assert out["resourceGroups"]["items"][0]["aksClusters"] is None  # not listed, not zero


def test_aks_list_returning_anything_but_a_list_or_a_nameless_cluster_is_an_error(run_script):
    for reply in ('{"value": []}', json.dumps([{"location": "eastus"}])):
        runner = Replies(
            {
                ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
                ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
                list_argv("rg-a"): (0, "[]", ""),
                aks_argv("rg-a"): (0, reply, ""),
                **no_compute("rg-a"),
            }
        )
        run = discover(run_script, runner=runner)
        assert run.envelope["status"] == "partial"
        assert [(e["code"], e["scope"]) for e in run.errors] == [("internal", "aks-cluster:rg-a")]


def test_a_group_whose_virtual_machines_cannot_be_listed_does_not_hide_its_other_types(
    run_script,
):
    denied = "ERROR: (AuthorizationFailed) no 'Microsoft.Compute/virtualMachines/read'\n"
    runner = Replies(
        {
            ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
            ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
            list_argv("rg-a"): (0, json.dumps([vault_row("kv-a-vault", "rg-a")]), ""),
            aks_argv("rg-a"): (0, "[]", ""),
            ("vm", "list", "-g", "rg-a", "--subscription", SUB): (1, "", denied),
            ("vmss", "list", "-g", "rg-a", "--subscription", SUB): (0, "[]", ""),
        }
    )
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [
        ("permission_denied", "virtual-machine:rg-a")
    ]
    states = {
        p["type"]: p["status"]
        for p in json.loads(run.files["sync.json"])["commit"]["partitions"]
        if p["parentPath"].endswith("/rg-a")
    }
    assert states == {
        "keyvault": "complete",
        "aks-cluster": "complete",
        "virtual-machine": "forbidden",
        "vm-scale-set": "complete",
    }
    out = run.envelope["outputs"]
    assert out["virtualMachines"] == {"items": [], "total": 0, "omitted": 0}  # not "none"
    (row,) = out["resourceGroups"]["items"]
    assert (row["keyvaults"], row["virtualMachines"], row["vmScaleSets"]) == (1, None, 0)


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
            **no_compute("rg-a", "rg-b"),
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


def budget_ctx(answers):
    from types import SimpleNamespace

    errors = []
    return SimpleNamespace(
        settings=SimpleNamespace(command_timeout="60s", max_items=200),
        resource={"path": f"azure/subscriptions/{SUB}", "type": "azure/subscription", "name": SUB},
        run=Replies(answers),
        errors=errors,
        error=lambda code, message, scope=None: errors.append(
            {"code": code, "message": message, "scope": scope}
        ),
    )


def test_the_listing_budget_is_checked_before_each_reader_and_leaves_the_rest_failed_not_swept():
    """The budget is spent between two readers of the SAME group: rg-a's vaults are listed, its
    clusters are not (a failed partition with a timeout), and rg-b and rg-c are not listed at all.
    Every type a fan-out skill adds is one more row of CHILD_TYPES and gets this for free."""
    answers = {
        ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
        ("group", "list", "--subscription", SUB): (
            0,
            json.dumps([group_row(n) for n in ("rg-a", "rg-b", "rg-c")]),
            "",
        ),
        list_argv("rg-a"): (0, json.dumps([vault_row("kv-a-vault", "rg-a")]), ""),
    }
    ctx = budget_ctx(answers)
    # start; rg-a keyvault (ok); then rg-a's other three types and rg-b's and rg-c's four (past the budget); the summary
    ticks = iter([0, 1, *([400] * 11), 401])
    outputs, sync_doc = dc.discover(ctx, 200, clock=lambda: next(ticks), budget=300)
    by_type = {
        (p["type"], p["parentPath"].rsplit("/", 1)[-1]): p
        for p in sync_doc["commit"]["partitions"]
        if p["type"] != "resourcegroup"
    }
    assert by_type[("keyvault", "rg-a")]["status"] == "complete"
    assert by_type[("aks-cluster", "rg-a")]["status"] == "failed"
    assert "budget" in by_type[("aks-cluster", "rg-a")]["reason"]
    assert "count" not in by_type[("aks-cluster", "rg-a")]  # a failed partition sweeps nothing
    for group in ("rg-b", "rg-c"):
        assert by_type[("keyvault", group)]["status"] == "failed"
        assert by_type[("aks-cluster", group)]["status"] == "failed"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("timeout", "keyvault"),
        ("timeout", "aks-cluster"),
        ("timeout", "virtual-machine"),
        ("timeout", "vm-scale-set"),
    ]
    assert "2 resource groups" in ctx.errors[0]["message"]  # rg-b and rg-c
    for error in ctx.errors[1:]:
        assert "3 resource groups" in error["message"]  # rg-a, rg-b and rg-c
    assert ctx.run.calls[-1] == list(list_argv("rg-a"))  # nothing was asked after the budget
    assert outputs["summary"]["partitions"]["failed"] == 11  # 3 in rg-a, 4 in each of rg-b, rg-c
    for group in ("rg-a", "rg-b", "rg-c"):
        assert by_type[("virtual-machine", group)]["status"] == "failed"
        assert by_type[("vm-scale-set", group)]["status"] == "failed"
    assert outputs["virtualMachines"]["total"] == 0
    assert [v["name"] for v in outputs["keyvaults"]["items"]] == ["kv-a-vault"]
    assert outputs["aksClusters"]["total"] == 0
    assert validate("rw.discovery.v1", sync_doc) == []


def unregistered_answers(*types):
    not_registered = (
        1,
        "",
        "ERROR: (MissingSubscriptionRegistration) The subscription is not registered to use "
        "namespace 'Microsoft.ContainerService'. See https://aka.ms/rps-not-found for how to "
        "register subscriptions.\nCode: MissingSubscriptionRegistration\n",
    )
    answers = {
        ("account", "show", "--subscription", SUB): (0, ACCOUNT, ""),
        ("group", "list", "--subscription", SUB): (0, json.dumps([group_row("rg-a")]), ""),
        list_argv("rg-a"): (0, json.dumps([vault_row("kv-a-vault", "rg-a")]), ""),
        aks_argv("rg-a"): (0, "[]", ""),
        **no_compute("rg-a"),
    }
    if "aks-cluster" in types:
        answers[aks_argv("rg-a")] = not_registered
    if "keyvault" in types:
        answers[list_argv("rg-a")] = not_registered
    return Replies(answers)


def test_an_unregistered_provider_is_a_complete_empty_partition_and_a_fact_not_a_failure(
    run_script,
):
    """Azure refuses `az aks list` when the subscription never registered Microsoft.ContainerService.
    No cluster can exist then, so the listing is complete with count 0 (it may be swept) and the
    group says which providers are missing; the run is ok, with no error."""
    run = discover(run_script, runner=unregistered_answers("aks-cluster"))
    assert (run.envelope["status"], run.errors) == ("ok", [])
    partitions = json.loads(run.files["sync.json"])["commit"]["partitions"]
    assert {"type": "aks-cluster", "status": "complete", "count": 0}.items() <= partitions[
        2
    ].items()
    out = run.envelope["outputs"]
    (group,) = out["resourceGroups"]["items"]
    assert group["providerNotRegistered"] == ["aks-cluster"] and group["aksClusters"] == 0
    assert group["keyvaults"] == 1
    assert out["aksClusters"]["total"] == 0


def test_each_type_reports_its_own_unregistered_provider(run_script):
    run = discover(run_script, runner=unregistered_answers("aks-cluster", "keyvault"))
    (group,) = run.envelope["outputs"]["resourceGroups"]["items"]
    assert group["providerNotRegistered"] == ["keyvault", "aks-cluster"]
    assert run.envelope["status"] == "ok"


def test_a_group_whose_providers_are_registered_reports_none(run_script):
    (group, *_) = discover(run_script).envelope["outputs"]["resourceGroups"]["items"]
    assert group["providerNotRegistered"] == []


def test_a_missing_az_is_still_a_failed_partition_not_an_unregistered_provider(run_script):
    runner = unregistered_answers()
    runner.answers[aks_argv("rg-a")] = (127, "", "az: command not found")
    run = discover(run_script, runner=runner)
    assert run.envelope["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in run.errors] == [("dependency", "aks-cluster:rg-a")]
    (group,) = run.envelope["outputs"]["resourceGroups"]["items"]
    assert group["providerNotRegistered"] == [] and group["aksClusters"] is None


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
    # the platform refuses the 8th item (index 7 of the batch): the prod vault
    http = Http(rejected=[{"index": 7, "code": "bad_name"}])
    monkeypatch.setattr(sync, "default_open", http)
    run = discover(run_script)
    assert run.envelope["status"] == "partial"
    (_, _, commit) = http.requests[-1]
    states = {
        (p["type"], p["parentPath"].rsplit("/", 1)[-1]): p["status"] for p in commit["partitions"]
    }
    assert states[("keyvault", "rg-orders-prod")] == "failed"
    assert [k for k, v in states.items() if v == "failed"] == [("keyvault", "rg-orders-prod")]
    assert states[("virtual-machine", "rg-orders-prod")] == "complete"  # same group, other type
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
        "virtual-machine",
        "vm-scale-set",
        "resourcegroup",
        "keyvault",
        "aks-cluster",
        "virtual-machine",
        "vm-scale-set",
        "resourcegroup",
        "virtual-machine",
    ]
    assert http.requests[2][2]["summary"]["counts"] == {
        "subscription": 1,
        "resourcegroup": 3,
        "keyvault": 2,
        "aks-cluster": 2,
        "virtual-machine": 3,
        "vm-scale-set": 2,
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
    # az keyvault list -g reads the vaults of one group and az aks list -g its clusters. Nothing
    # else: without -g az would search the subscription's resources
    # (Microsoft.Resources/subscriptions/resources/read)
    assert task["permissions"] == [
        {"action": "Microsoft.Resources/subscriptions/resourceGroups/read"},
        {"action": "Microsoft.KeyVault/vaults/read"},
        {"action": "Microsoft.ContainerService/managedClusters/read"},
        {"action": "Microsoft.Compute/virtualMachines/read"},
        {"action": "Microsoft.Compute/virtualMachineScaleSets/read"},
    ]


def test_the_default_settings_in_the_tests_match_the_declared_defaults():
    doc = yaml.safe_load((SKILLS / "azure-base" / "tasks.yaml").read_text())
    for name, spec in doc["settings"].items():
        assert SETTINGS[name] == spec["default"]


def test_the_shared_target_in_the_tests_is_the_stub_subscription():
    assert SUBSCRIPTION["name"] == SUB == dc.target_subscription(SUBSCRIPTION)
