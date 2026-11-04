"""Capture the azure-aks-health stub answers from the pinned az CLI (plan Appendix C7).

Runs inside the `azure@1` image, offline:

    docker run --rm --network none -v "$PWD/capture:/capture:ro" -v "$OUT:/out" \
        --entrypoint /opt/az/bin/python rwp-runtime-azure:dev -I /capture/capture_aks.py /out

or through `capture_aks.sh`, which also splices the result into the scenarios.

For every scenario it runs each `az` command the skill (and discover) issues for real, through
`fake_az.py` (sign-in and HTTP replaced, everything else the CLI's own), against the ARM bodies of
`aks_world.py`, and writes into /out/<scenario>/:

    aks-*.json        what the CLI printed on stdout
    calls.aks.jsonl   one stub call per line (argv, stdout file, stderr, rc, source), ready to splice
    served.json       the REST requests the CLI made (path and query, so the api-version is on record)

Nothing is typed by hand into those files: the stdout, the stderr text and the exit code are az's.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python -I does not put the script's directory on the path

import aks_world as w  # noqa: E402

FAKE = HERE / "fake_az.py"
START, END = "2026-11-03T04:00:00Z", "2026-11-04T04:00:00Z"  # stand-ins; the stub matches <ts>
SELECT = [
    "eventTimestamp",
    "level",
    "operationName",
    "properties",
    "resourceId",
    "status",
    "subStatus",
]
SCAN_LIMIT = 500  # the skill's activityScanLimit default; it asks for one more to see a cut

DOC = "https://learn.microsoft.com/rest/api/"
SRC = {
    "show": DOC + "aks/managed-clusters/get",
    "list": DOC + "aks/managed-clusters/list-by-resource-group",
    "pools": DOC + "aks/agent-pools/list",
    "versions": DOC + "aks/managed-clusters/list-kubernetes-versions",
    "upgrades": DOC + "aks/managed-clusters/get-upgrade-profile",
    "diag": DOC + "monitor/diagnostic-settings/list",
    "activity": DOC + "monitor/activity-logs/list",
    "metrics": DOC + "monitor/metrics/list",
    "health": DOC + "resourcehealth/availability-statuses/get-by-resource",
}


def esc(text):
    return re.escape(text)


def rule(path, body=None, *, status=200, query=None, headers=None):
    return {
        "method": "GET",
        "path": esc(path),
        "query": query or {},
        "status": status,
        "headers": headers or {},
        "body": body,
    }


class Call:
    """One az command: its argv (after `az`), the ARM rules that answer it, the file its stdout goes
    to and the reference page its body follows."""

    def __init__(self, argv, rules, stdout, source):
        self.argv, self.rules, self.stdout, self.source = argv, rules, stdout, source


def ok(body):
    return 200, body


def answer(result, path, *, query=None):
    """A rule for a (status, body) pair; a 429 carries Retry-After 0 so the CLI's retries are quick."""
    status, body = result
    headers = {"Retry-After": "0"} if status == 429 else {}
    return rule(path, body, status=status, query=query, headers=headers)


# --- the commands, as the skill and discover issue them ---------------------------------------------------


def show(cluster, result):
    name, group, _ = cluster
    cid = w.cluster_id(name, group)
    return Call(
        ["aks", "show", "--name", name, "-g", group, "--subscription", w.SUB],
        [answer(result, cid)],
        "aks-show.json",
        SRC["show"],
    )


def pools(cluster, result):
    name, group, _ = cluster
    return Call(
        ["aks", "nodepool", "list", "--cluster-name", name, "-g", group, "--subscription", w.SUB],
        [answer(result, w.cluster_id(name, group) + "/agentPools")],
        "aks-nodepools.json",
        SRC["pools"],
    )


def versions(cluster, result):
    _, _, location = cluster
    return Call(
        ["aks", "get-versions", "--location", location, "--subscription", w.SUB],
        [
            answer(
                result,
                f"/subscriptions/{w.SUB}/providers/Microsoft.ContainerService/locations/{location}/kubernetesVersions",
            )
        ],
        "aks-versions.json",
        SRC["versions"],
    )


def upgrades(cluster, result):
    name, group, _ = cluster
    return Call(
        ["aks", "get-upgrades", "--name", name, "-g", group, "--subscription", w.SUB],
        [answer(result, w.cluster_id(name, group) + "/upgradeProfiles/default")],
        "aks-upgrades.json",
        SRC["upgrades"],
    )


def diagnostics(cluster, result):
    name, group, _ = cluster
    cid = w.cluster_id(name, group)
    return Call(
        ["monitor", "diagnostic-settings", "list", "--resource", cid, "--subscription", w.SUB],
        [answer(result, cid + "/providers/Microsoft.Insights/diagnosticSettings")],
        "aks-diagnostic-settings.json",
        SRC["diag"],
    )


def health(cluster, result):
    name, group, _ = cluster
    cid = w.cluster_id(name, group)
    url = f"{cid}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01"
    return Call(
        ["rest", "--method", "get", "--url", url],
        [answer(result, cid + "/providers/Microsoft.ResourceHealth/availabilityStatuses/current")],
        "aks-resource-health.json",
        SRC["health"],
    )


def activity(cluster, result):
    name, group, _ = cluster
    return Call(
        [
            "monitor", "activity-log", "list", "-g", group, "--status", "Failed",
            "--start-time", START, "--end-time", END, "--max-events", str(SCAN_LIMIT + 1),
            "--select", *SELECT, "--subscription", w.SUB,
        ],
        [answer(result, f"/subscriptions/{w.SUB}/providers/Microsoft.Insights/eventtypes/management/values")],
        "aks-activity-log.json",
        SRC["activity"],
    )  # fmt: skip


SHORT = {
    "node_cpu_usage_percentage": "cpu",
    "node_memory_working_set_percentage": "memory",
    "node_disk_usage_percentage": "disk",
}


def metric_calls(cluster, series=None, error=None):
    """One call per metric. `series` maps a metric to (averages, maximums); `error` is a (status, body)."""
    name, group, location = cluster
    cid = w.cluster_id(name, group)
    calls = []
    for metric in w.METRICS:
        result = error or ok(w.metric_body(name, group, location, metric, *series[metric]))
        calls.append(
            Call(
                [
                    "monitor",
                    "metrics",
                    "list",
                    "--resource",
                    cid,
                    "--metrics",
                    metric,
                    "--aggregation",
                    "Average",
                    "Maximum",
                    "--interval",
                    "PT1H",
                    "--start-time",
                    START,
                    "--end-time",
                    END,
                    "--subscription",
                    w.SUB,
                ],
                [
                    answer(
                        result,
                        cid + "/providers/Microsoft.Insights/metrics",
                        query={"metricnames": esc(metric)},
                    )
                ],
                f"aks-metrics-{SHORT[metric]}.json",
                SRC["metrics"],
            )  # fmt: skip
        )
    return calls


def listing(group, clusters, result=None):
    return Call(
        ["aks", "list", "-g", group, "--subscription", w.SUB],
        [
            answer(
                result or ok(w.aks_list_body(*clusters)),
                f"/subscriptions/{w.SUB}/resourceGroups/{group}/providers/Microsoft.ContainerService/managedClusters",
            )
        ],
        f"aks-list-{group.lower()}.json",
        SRC["list"],
    )


def discover_lists(failing=None):
    """The three per-group listings of azure-base/discover; `failing` maps a group to an error."""
    dev, prod = w.dev_cluster(), w.prod_cluster()
    by_group = {"rg-orders-dev": [dev], "rg-orders-prod": [prod], "RG-Platform": []}
    return [listing(g, by_group[g], (failing or {}).get(g)) for g in w.GROUPS]


# --- the scenarios ------------------------------------------------------------------------------------------


def healthy():
    prod = w.PROD
    name, group, location = prod
    pools_ = [(n, p["currentOrchestratorVersion"]) for n, p in w.pools_prod().items()]
    return [
        *discover_lists(),
        show(prod, ok(w.prod_cluster())),
        health(prod, ok(w.resource_health(*prod, degraded=False))),
        diagnostics(prod, ok(w.diagnostic_settings(name, group))),
        pools(prod, ok(w.agent_pools_body(name, group, w.pools_prod()))),
        versions(prod, ok(w.kubernetes_versions())),
        upgrades(
            prod,
            ok(w.upgrade_profile(name, group, "1.33.4", [("1.33.5", False), ("1.34.1", False), ("1.35.0", True)], pools_)),
        ),
        activity(prod, ok(w.events_healthy())),
        *metric_calls(prod, w.metrics_prod()),
    ]  # fmt: skip


def broken():
    dev = w.DEV
    name, group, location = dev
    pools_ = [(n, p["currentOrchestratorVersion"]) for n, p in w.pools_dev().items()]
    return [
        show(dev, ok(w.dev_cluster())),
        health(dev, ok(w.resource_health(*dev, degraded=True))),
        diagnostics(dev, ok({"value": []})),
        pools(dev, ok(w.agent_pools_body(name, group, w.pools_dev()))),
        versions(dev, ok(w.kubernetes_versions())),
        upgrades(dev, ok(w.upgrade_profile(name, group, "1.29.9", [("1.30.14", False)], pools_))),
        activity(dev, ok(w.events_broken())),
        *metric_calls(dev, w.metrics_dev()),
    ]


def stopped():
    dev = w.DEV
    name, group, _ = dev
    return [
        show(dev, ok(w.dev_cluster(stopped=True))),
        diagnostics(dev, ok({"value": []})),
        pools(dev, ok(w.agent_pools_body(name, group, w.pools_dev("Stopped")))),
        *metric_calls(dev, w.metrics_stopped()),
    ]


def monitor_forbidden():
    """Reader on the cluster but no read on Azure Monitor: the cluster reads, the Monitor calls do not."""
    dev = w.DEV
    name, group, _ = dev
    cid = w.cluster_id(name, group)
    sub = f"/subscriptions/{w.SUB}"
    return [
        show(dev, ok(w.dev_cluster())),
        diagnostics(dev, w.forbidden_error("Microsoft.Insights/diagnosticSettings/read", cid)),
        activity(
            dev, w.forbidden_error("Microsoft.Insights/eventtypes/management/values/read", sub)
        ),
        *metric_calls(dev, error=w.forbidden_error("Microsoft.Insights/metrics/read", cid)),
    ]


def forbidden():
    dev = w.DEV
    return [
        show(dev, w.forbidden_error("Microsoft.ContainerService/managedClusters/read", w.cluster_id(dev[0], dev[1])))
    ]  # fmt: skip


def throttled():
    return [show(w.DEV, w.THROTTLED)]


def not_found():
    return [
        show(
            ("nope", "rg-orders-dev", "westeurope"),
            w.not_found_error(f"{w.RP}/managedClusters/nope", "rg-orders-dev"),
        )
    ]


def group_forbidden():
    """azure-base/discover: the identity cannot list rg-orders-dev's key vaults (the other scenario
    file), but can list its clusters: the two partitions of a group are independent."""
    return discover_lists()


SCENARIOS = {
    "healthy": healthy,
    "broken": broken,
    "stopped": stopped,
    "monitor-forbidden": monitor_forbidden,
    "forbidden": forbidden,
    "throttled": throttled,
    "not-found": not_found,
    "group-forbidden": group_forbidden,
}


# --- running az -----------------------------------------------------------------------------------------------


def profile(config_dir):
    """The local state a signed-in az would have: one subscription, as a service principal."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "azureProfile.json").write_text(
        json.dumps(
            {
                "installationId": "00000000-0000-4000-8000-0000000000cc",
                "subscriptions": [
                    {
                        "environmentName": "AzureCloud",
                        "homeTenantId": w.TENANT,
                        "id": w.SUB,
                        "isDefault": True,
                        "managedByTenants": [],
                        "name": "rw-pilot-subscription",
                        "state": "Enabled",
                        "tenantId": w.TENANT,
                        "user": {
                            "name": "00000000-0000-4000-8000-0000000000aa",
                            "type": "servicePrincipal",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def run_az(call, scratch):
    rules = scratch / "rules.json"
    rules.write_text(json.dumps(call.rules))
    served = scratch / "served.json"
    served.unlink(missing_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(scratch),
        "AZURE_CONFIG_DIR": str(scratch / "azure"),
        "AZURE_CORE_COLLECT_TELEMETRY": "false",
        "AZURE_CORE_CHECK_UPGRADE": "false",
        "FAKE_ARM": str(rules),
        "FAKE_ARM_SERVED": str(served),
    }
    done = subprocess.run(
        [sys.executable, "-I", str(FAKE), *call.argv, "--output", "json"],
        env=env, capture_output=True, text=True,
    )  # fmt: skip
    seen = json.loads(served.read_text()) if served.exists() else []
    return done.returncode, done.stdout, done.stderr, seen


def stub_argv(argv):
    """The argv the stub is asked for: the window the capture used is a <ts> token."""
    return ["<ts>" if a in (START, END) else a for a in argv]


def main(out):
    out = Path(out)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        profile(scratch / "azure")
        for scenario, build in SCENARIOS.items():
            directory = out / scenario
            directory.mkdir(parents=True, exist_ok=True)
            lines, served = [], {}
            for call in build():
                rc, stdout, stderr, seen = run_az(call, scratch)
                entry = {"argv": stub_argv(call.argv)}
                if rc == 0 and stdout.strip():
                    (directory / call.stdout).write_text(stdout, encoding="utf-8")
                    entry["stdout"] = call.stdout
                elif rc == 0:
                    raise SystemExit(f"{scenario}: {call.argv[:3]} printed nothing")
                if stderr:
                    entry["stderr"] = stderr
                entry["source"] = call.source
                entry["rc"] = rc
                lines.append("    " + json.dumps(entry, ensure_ascii=False) + ",")
                served[" ".join(call.argv[:3])] = seen
                print(f"{scenario:18} rc={rc} {' '.join(call.argv[:3])}", file=sys.stderr)
            (directory / "calls.aks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (directory / "served.json").write_text(
                json.dumps(served, indent=1, sort_keys=True), encoding="utf-8"
            )


if __name__ == "__main__":
    os.chdir("/")
    main(sys.argv[1])
