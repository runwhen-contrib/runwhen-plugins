"""Render the azure-vm-health stub scenarios with the real az CLI.

The canned output of `az vm|vmss|monitor|rest ...` must come from the pinned CLI, not from memory
(plan Appendix C7). This script runs the real az 2.92 in the azure@1 image, with its HTTP transport
replaced by `vmworld.answer` (a synthetic Azure Resource Manager that returns the REST bodies Microsoft
documents), and writes what the CLI printed: stdout, stderr and the exit code of every call. So the
formatting is the CLI's (it flattens `properties`, adds `resourceGroup`, sorts keys), the errors are
the CLI's wording for a 403, a 404 and a 429, and every argv below has been parsed by az itself:
a mistyped option fails here.

    docker run --rm --network none -v "$PWD/plugins/azure:/plugin:ro" -v "$OUT:/out" \
        --entrypoint /opt/az/bin/python rwp-runtime-azure:dev -I /plugin/tests/stub/capture/render_vm.py /out

Writes /out/<scenario>/*.json + calls.yaml for the azure-vm-health scenarios (directory names are
`vm-<scenario>`), and /out/discover/ with the vm and vmss listings of each resource group that the
azure-base discover scenarios add. Needs no network and no login. The guest scripts' answers come from
capture/guest/*.txt (capture/guest.sh).
"""

import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vmworld as W  # noqa: E402

SUB = W.SUB
LIB = os.path.join(HERE, "..", "..", "..", "skills", "azure-vm-health", "lib")
START, END = "2026-11-03T22:00:00Z", "2026-11-04T04:00:00Z"
INTERVAL = "PT15M"

CLI = "https://learn.microsoft.com/cli/azure/"
SOURCES = {
    "vm-show": CLI + "vm#az-vm-show",
    "vm-iv": CLI + "vm#az-vm-get-instance-view",
    "vm-list": CLI + "vm#az-vm-list",
    "vmss-show": CLI + "vmss#az-vmss-show",
    "vmss-iv": CLI + "vmss#az-vmss-get-instance-view",
    "vmss-instances": CLI + "vmss#az-vmss-list-instances",
    "vmss-list": CLI + "vmss#az-vmss-list",
    "autoscale": CLI + "monitor/autoscale#az-monitor-autoscale-list",
    "activity": CLI + "monitor/activity-log#az-monitor-activity-log-list",
    "metrics": CLI + "monitor/metrics#az-monitor-metrics-list",
    "health": "https://learn.microsoft.com/rest/api/resourcehealth/availability-statuses/get-by-resource",
    "run": CLI + "vm/run-command#az-vm-run-command-invoke",
}
METRICS = (
    ("cpu", "Percentage CPU", ["Average", "Maximum"]),
    ("memory", "Available Memory Bytes", ["Average", "Minimum"]),
    ("disk-read", "Disk Read Bytes", ["Total"]),
    ("disk-write", "Disk Write Bytes", ["Total"]),
    ("network-in", "Network In Total", ["Total"]),
    ("network-out", "Network Out Total", ["Total"]),
)
CHECKS = ("disk", "memory", "uptime", "patches")


def scripts():
    sys.path.insert(0, LIB)
    from azure_vm_health.guest_scripts import SCRIPTS

    return SCRIPTS


# --- the argv each task issues (a test keeps them equal to the code's) ---------------------------------
def base(name, group):
    return ["--name", name, "-g", group, "--subscription", SUB]


def call(id_, kind, argv):
    return {"id": id_, "source": SOURCES[kind], "argv": argv}


def vm_calls(name, group, runs=False, full=True):
    rid = W.rid(group, "virtualMachines", name)
    out = [
        call("vm-show", "vm-show", ["vm", "show", *base(name, group)]),
        call("vm-instance-view", "vm-iv", ["vm", "get-instance-view", *base(name, group)]),
    ]
    if not full:
        return out
    out.append(
        call(
            "vm-health",
            "health",
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"{rid}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
            ],
        )
    )
    out += metric_calls("vm", rid)
    out.append(activity_call("vm", rid))
    if runs:
        for check, text in scripts().items():
            out.append(
                call(
                    f"vm-run-{check}",
                    "run",
                    [
                        "vm",
                        "run-command",
                        "invoke",
                        "--name",
                        name,
                        "-g",
                        group,
                        "--command-id",
                        "RunShellScript",
                        "--scripts",
                        text,
                        "--subscription",
                        SUB,
                    ],
                )
            )
    return out


def metric_calls(prefix, rid):
    return [
        call(
            f"{prefix}-metrics-{field}",
            "metrics",
            [
                "monitor",
                "metrics",
                "list",
                "--resource",
                rid,
                "--metrics",
                metric,
                "--aggregation",
                *aggs,
                "--interval",
                INTERVAL,
                "--start-time",
                START,
                "--end-time",
                END,
                "--subscription",
                SUB,
            ],
        )
        for field, metric, aggs in METRICS
    ]


def activity_call(prefix, rid):
    return call(
        f"{prefix}-activity",
        "activity",
        [
            "monitor",
            "activity-log",
            "list",
            "--resource-id",
            rid,
            "--start-time",
            START,
            "--end-time",
            END,
            "--max-events",
            "1000",
            "--subscription",
            SUB,
        ],
    )


def vmss_calls(name, group, full=True, only=None):
    rid = W.rid(group, "virtualMachineScaleSets", name)
    out = [call("vmss-show", "vmss-show", ["vmss", "show", *base(name, group)])]
    if not full:
        return out
    out += [
        call("vmss-instance-view", "vmss-iv", ["vmss", "get-instance-view", *base(name, group)]),
        call(
            "vmss-instances",
            "vmss-instances",
            ["vmss", "list-instances", *base(name, group), "--expand", "instanceView"],
        ),
        call(
            "vmss-autoscale",
            "autoscale",
            ["monitor", "autoscale", "list", "-g", group, "--subscription", SUB],
        ),
        call(
            "vmss-health",
            "health",
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"{rid}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2022-10-01",
            ],
        ),
        *metric_calls("vmss", rid),
        activity_call("vmss", rid),
    ]
    return out


def plans():
    return {
        "healthy": vm_calls("vm-orders-web01", "rg-orders-prod", runs=True)
        + vmss_calls("vmss-orders-api", "rg-orders-prod"),
        "broken": vm_calls("vm-orders-batch02", "rg-orders-dev")
        + vmss_calls("vmss-orders-jobs", "rg-orders-dev"),
        # the guest of a VM whose agent is ready, with the findings: batch02's agent is not ready, so no script reaches it
        "guest-broken": [
            c
            for c in vm_calls("vm-orders-web01", "rg-orders-prod", runs=True)
            if c["id"] in ("vm-instance-view",) or "-run-" in c["id"]
        ],
        "forbidden": vm_calls("vm-orders-batch02", "rg-orders-dev", full=False)
        + vmss_calls("vmss-orders-jobs", "rg-orders-dev", full=False),
        "throttled": vm_calls("vm-orders-batch02", "rg-orders-dev", full=False)
        + vmss_calls("vmss-orders-jobs", "rg-orders-dev", full=False),
        "not-found": vm_calls("nope", "rg-orders-dev", full=False)
        + vmss_calls("nope", "rg-orders-dev", full=False),
        "stopped": vm_calls("vm-platform-legacy", "rg-platform", full=False)[1:],
        "no-data": vm_calls("vm-orders-batch02", "rg-orders-dev", full=False)[:1]
        + metric_calls("vm", W.rid("rg-orders-dev", "virtualMachines", "vm-orders-batch02")),
        "instance-view-forbidden": [c for c in vmss_calls("vmss-orders-jobs", "rg-orders-dev")[:3]],
        "instances-forbidden": [c for c in vmss_calls("vmss-orders-jobs", "rg-orders-dev")[:3]],
        "runcommand-forbidden": [
            vm_calls("vm-orders-web01", "rg-orders-prod", full=False)[1],
            vm_calls("vm-orders-web01", "rg-orders-prod", runs=True)[-4],
        ],
    }


def discover_calls():
    out = []
    for group in ("rg-orders-dev", "rg-orders-prod", "RG-Platform"):
        out.append(
            call(
                f"vm-list-{group.lower()}",
                "vm-list",
                ["vm", "list", "-g", group, "--subscription", SUB],
            )
        )
        out.append(
            call(
                f"vmss-list-{group.lower()}",
                "vmss-list",
                ["vmss", "list", "-g", group, "--subscription", SUB],
            )
        )
    return out


# --- one real az call, in its own process ------------------------------------------------------------
def run_one(spec_path):
    spec = json.load(open(spec_path))
    os.environ["AZURE_CONFIG_DIR"] = spec["config"]
    os.makedirs(spec["config"], exist_ok=True)
    tenant = "c0ffee00-1234-4abc-8def-0123456789ab"
    json.dump(
        {
            "installationId": "x",
            "subscriptions": [
                {
                    "id": SUB,
                    "name": "orders",
                    "state": "Enabled",
                    "user": {"name": W.CALLER, "type": "servicePrincipal"},
                    "isDefault": True,
                    "tenantId": tenant,
                    "environmentName": "AzureCloud",
                    "homeTenantId": tenant,
                    "managedByTenants": [],
                }
            ],
        },
        open(os.path.join(spec["config"], "azureProfile.json"), "w"),
    )
    import requests
    from azure.cli.core import get_default_cli
    from azure.cli.core._profile import Profile
    from azure.core.credentials import AccessToken
    from urllib3.response import HTTPResponse

    for check, text in scripts().items():
        W.CHECK_OF_SCRIPT[text] = check

    class Credential:
        def get_token(self, *a, **k):
            return AccessToken("fake-token", 4102444800)

    Profile.get_login_credentials = lambda self, *a, **k: (Credential(), SUB, tenant)
    Profile.get_raw_token = lambda self, *a, **k: (
        ("Bearer", "fake-token", {"expires_on": "2099-01-01"}),
        SUB,
        tenant,
    )

    def send(self, request, **kw):
        if "management.azure.com" not in request.url:
            raise requests.exceptions.ConnectionError("offline")
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        code, headers, doc = W.answer(spec["scenario"], request.method, request.url, body)
        data = json.dumps(doc).encode()
        r = requests.Response()
        r.status_code = code
        r.reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests"}.get(
            code, ""
        )
        r.headers.update({"Content-Type": "application/json; charset=utf-8", **headers})
        r.raw = HTTPResponse(
            body=io.BytesIO(data), status=code, headers=r.headers, preload_content=False
        )
        r.url, r.request = request.url, request
        return r

    requests.adapters.HTTPAdapter.send = send
    out, err = io.StringIO(), io.StringIO()
    sys.stderr, real_stderr = err, sys.stderr
    try:
        rc = get_default_cli().invoke([*spec["argv"], "--output", "json"], out_file=out)
    except SystemExit as exc:  # az's argument parser exits 2 on an option it does not know
        rc = exc.code
    finally:
        sys.stderr = real_stderr
    json.dump({"rc": rc or 0, "stdout": out.getvalue(), "stderr": err.getvalue()}, sys.stdout)


def execute(scenario, item, tmp):
    spec = os.path.join(tmp, f"{scenario}-{item['id']}.json")
    argv = [a.replace("<ts>", "") for a in item["argv"]]
    json.dump(
        {
            "scenario": scenario,
            "argv": argv,
            "config": os.path.join(tmp, f"cfg-{scenario}-{item['id']}"),
        },
        open(spec, "w"),
    )
    done = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--one", spec],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise SystemExit(
            f"render_vm: {scenario}/{item['id']} crashed (rc {done.returncode}):\n{done.stderr[-3000:]}\n{done.stdout[-500:]}"
        )
    return json.loads(done.stdout)


def render(out_dir):
    import tempfile

    tmp = tempfile.mkdtemp()
    jobs = [(s, c) for s, items in plans().items() for c in items] + [
        ("healthy", c) for c in discover_calls()
    ]
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda j: execute(j[0], j[1], tmp), jobs))
    by_scenario = {}
    for (scenario, item), res in zip(jobs, results):
        by_scenario.setdefault(
            (
                "discover"
                if item["id"].endswith(("rg-orders-dev", "rg-orders-prod", "rg-platform"))
                and "list-" in item["id"]
                else scenario
            ),
            [],
        ).append((item, res))
    for scenario, rows in by_scenario.items():
        directory = os.path.join(
            out_dir, "discover" if scenario == "discover" else f"vm-{scenario}"
        )
        os.makedirs(directory, exist_ok=True)
        calls = []
        for item, res in rows:
            argv = [START if a == START else a for a in item["argv"]]
            argv = ["<ts>" if a in (START, END) else a for a in argv]
            entry = {"argv": argv}
            if res["rc"] == 0 and res["stdout"].strip():
                name = f"{item['id']}.json"
                open(os.path.join(directory, name), "w").write(res["stdout"])
                entry["stdout"] = name
            if res["stderr"].strip():
                entry["stderr"] = res["stderr"]
            entry["source"], entry["rc"] = item["source"], res["rc"]
            calls.append(entry)
        header = (
            f"# {scenario}: rendered by capture/render_vm.py with the real az CLI against vmworld.py's synthetic ARM.\n"
            "# Format: JSON written as YAML; `<ts>` in an argv entry matches a timestamp (RFC 3339 UTC, whole seconds). See ../../README.md.\n"
        )
        with open(os.path.join(directory, "calls.yaml"), "w") as f:
            f.write(
                header
                + '{\n  "calls": [\n'
                + ",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in calls)
                + "\n  ]\n}\n"
            )
        print(f"{directory}: {len(calls)} calls, rc={[r['rc'] for _, r in rows]}")


if __name__ == "__main__":
    if sys.argv[1] == "--one":
        run_one(sys.argv[2])
    else:
        render(sys.argv[1])
