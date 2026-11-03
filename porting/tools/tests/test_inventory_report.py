import sys
from pathlib import Path

import yaml

PORTING = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PORTING / "tools"))

import inventory_report as ir  # noqa: E402


def task(title, *, read_only=True, false_pass="none", scope="ns:target", why=""):
    return {
        "title": title,
        "readOnly": read_only,
        "falsePass": false_pass,
        "falsePassWhy": why,
        "scope": scope,
    }


FIXTURE = [
    {
        "bundle": "gke-cluster-health",
        "classicResourceTypes": ["gcp_container_clusters"],
        "sli": True,
        "tasks": [
            task("List clusters", false_pass="confirmed", why="x.sh:1 2>/dev/null | jq length -> 0"),
            task("Patch cluster", read_only=False, scope="project"),
        ],
    },
    {
        "bundle": "k8s-x-healthcheck",
        "classicResourceTypes": ["deployment"],
        "sli": False,
        "tasks": [
            task("Check replicas", false_pass="likely", scope="all-namespaces"),
            task("Check events"),
        ],
    },
]
PARITY = {"kubernetes": [{"bundle": "k8s-x-healthcheck", "skills": ["k8s-workload-health"]}]}


def test_plugin_mapping():
    assert ir.plugin_of("gke-cluster-health") == "misc"
    assert ir.plugin_of("k8s-x") == "kubernetes"
    assert ir.plugin_of("azure-vm") == "azure"
    assert ir.plugin_of("aws-eks") == "aws"
    assert ir.plugin_of("gcp-bucket-health") == "gcp"
    assert ir.plugin_of("gcloud-log-inspection") == "misc"
    assert ir.plugin_of("gh-repo-health") == "misc"


def test_totals_line():
    md = ir.render(FIXTURE, PARITY)
    assert (
        "Totals: 2 bundles, 4 tasks, 1 write tasks, falsePass confirmed 1 / likely 1 / none 2." in md
    )


def test_kubernetes_table_has_skills_and_others_dash():
    md = ir.render(FIXTURE, PARITY)
    assert "| k8s-x-healthcheck | deployment | 2 | 0 | 0/1/1 | no | k8s-workload-health |" in md
    assert "| gke-cluster-health | gcp_container_clusters | 2 | 1 | 1/0/1 | yes | — |" in md


def test_beyond_baseline_rule():
    assert ir.is_beyond_baseline("gcp-apigee-health")
    assert not ir.is_beyond_baseline("gcp-bucket-health")
    assert ir.is_beyond_baseline("gh-repo-health")
    assert not ir.is_beyond_baseline("k8s-x")
    md = ir.render([*FIXTURE, {"bundle": "gcp-apigee-health", "classicResourceTypes": [], "tasks": []}], PARITY)
    assert "Beyond baseline (Samir decides scope), 1:" in md
    assert "- `gcp-apigee-health` - beyond baseline (Samir decides scope)" in md


def test_hidden_writes_rule():
    inv = [
        *FIXTURE,
        {"bundle": "k8s-deployment-ops", "tasks": [task("Restart", read_only=False)]},
        {"bundle": "k8s-chaos-flux", "tasks": [task("Suspend", read_only=False)]},
        {"bundle": "k8s-restart-resource", "tasks": [task("Restart", read_only=False)]},
        {"bundle": "k8s-gitops-gh-remediate", "tasks": [task("Fix", read_only=False)]},
        {"bundle": "azure-acr-image-sync", "tasks": [task("Sync", read_only=False)]},
    ]
    assert ir.hidden_writes(inv) == [("gke-cluster-health", "Patch cluster")]


def test_wider_than_target_lists_kubernetes_only():
    md = ir.render(FIXTURE, PARITY)
    section = md.split("## Wider than the target")[1].split("## Writes hidden")[0]
    assert "| k8s-x-healthcheck | Check replicas | all-namespaces |" in section
    assert "gke-cluster-health" not in section


def test_hot_spot_truncates_why():
    inv = [{**FIXTURE[0], "tasks": [task("t", false_pass="confirmed", why="y" * 400)]}]
    md = ir.render(inv, PARITY)
    row = next(line for line in md.splitlines() if line.startswith("| gke-cluster-health | 1 |"))
    assert "y" * 157 + "..." in row and "y" * 158 not in row


def test_parity_covers_every_k8s_bundle():
    parity = yaml.safe_load((PORTING / "parity.yaml").read_text())
    listed = [e["bundle"] for e in parity["kubernetes"]]
    inventory = ir.load_inventory(PORTING / "inventory")
    expected = sorted(b["bundle"] for b in inventory if b["bundle"].startswith("k8s-"))
    assert len(expected) == 46
    assert sorted(listed) == expected
    for entry in parity["kubernetes"]:
        assert entry["status"] in {"planned", "ported", "dropped"}
