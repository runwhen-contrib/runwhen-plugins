"""Unit tests for k8s-node-health's lib/ and scripts.

Nodes, pods and node events in data/k8s_node_health come from the
kind-rw-plugins cluster (one node, `rw-plugins-control-plane`, healthy;
`NOW` is a fixed point just after the capture). kind has no metrics-server, so
`node_metrics.json` is synthetic, shaped like
`kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes`. Broken cases (NotReady,
pressure, cordoned, tainted, over-committed) are built by mutating the
captured JSON.
"""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from k8s_node_health import common, fetch, k8s
from k8s_node_health import nodes as n
from k8s_node_health import resources as r
from k8s_node_health.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-node-health"
DATA = Path(__file__).parent / "data" / "k8s_node_health"
NOW = datetime(2026, 11, 3, 18, 40, tzinfo=timezone.utc)
NODE = "rw-plugins-control-plane"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def node(**changes):
    """A copy of the captured node; `name` renames it, other keys replace top-level fields."""
    doc = copy.deepcopy(load("nodes")["items"][0])
    name = changes.pop("name", None)
    if name:
        doc["metadata"]["name"] = name
    doc.update(changes)
    return doc


def set_condition(doc, ctype, status, reason=None, message=None, transition=None):
    for c in doc["status"]["conditions"]:
        if c["type"] == ctype:
            break
    else:
        c = {"type": ctype}
        doc["status"]["conditions"].append(c)
    c["status"] = status
    if reason is not None:
        c["reason"] = reason
    if message is not None:
        c["message"] = message
    if transition is not None:
        c["lastTransitionTime"] = transition
    return doc


def pod(name, node_name=NODE, phase="Running", containers=None, init=None, **spec):
    """A minimal Pod. Containers are (requests, limits) dicts of strings."""

    def ctr(i, rl, extra=None):
        requests, limits = rl
        res = {}
        if requests:
            res["requests"] = requests
        if limits:
            res["limits"] = limits
        return {"name": f"c{i}", "resources": res, **(extra or {})}

    spec_doc = {"containers": [ctr(i, rl) for i, rl in enumerate(containers or [])], **spec}
    if node_name:
        spec_doc["nodeName"] = node_name
    if init:
        spec_doc["initContainers"] = [
            ctr(f"i{i}", rl, {"restartPolicy": "Always"} if side else None)
            for i, (rl, side) in enumerate(init)
        ]
    return {
        "metadata": {"name": name, "namespace": "default"},
        "spec": spec_doc,
        "status": {"phase": phase},
    }


def event(reason, name=NODE, last="2026-11-03T18:30:00Z", component="node-controller", **extra):
    doc = {
        "metadata": {"name": f"{name}.{reason}", "creationTimestamp": last},
        "involvedObject": {"kind": "Node", "name": name, "uid": "uid"},
        "reason": reason,
        "type": "Normal",
        "message": f"Node {name} event: {reason}",
        "count": 1,
        "firstTimestamp": last,
        "lastTimestamp": last,
        "source": {"component": component},
    }
    doc.update(extra)
    return doc


# ---------------------------------------------------------- fake kubectl ----


class FakeCtx:
    """A stand-in for the SDK context: `routes` maps argv fragments to (rc, stdout, stderr)."""

    def __init__(self, routes, rtype="kubernetes/cluster", name="rw-plugins", **settings):
        self.routes = routes
        self.calls = []
        self.errors = []
        self.resource = SimpleNamespace(type=rtype, name=name, namespace=None)
        self.settings = SimpleNamespace(
            max_items=200, utilization_percent=90, pod_limit_percent=90, **settings
        )

    def run(self, argv):
        self.calls.append(argv)
        for fragment, (rc, out, err) in self.routes:
            if all(tok in argv for tok in fragment):
                return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        raise AssertionError(f"no route for {argv}")

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append((code, scope))


def ok(doc):
    return (0, json.dumps(doc), "")


FORBIDDEN = (
    1,
    "",
    'Error from server (Forbidden): nodes is forbidden: User "u" cannot list resource "nodes" '
    'in API group "" at the cluster scope',
)
NOT_FOUND = (1, "", 'Error from server (NotFound): nodes "nope" not found')
UNREACHABLE = (1, "", "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout")
NO_METRICS = (1, "", 'error: the server doesn\'t have a resource type "nodes"')


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def script(name):
    """The script module with its clock frozen at NOW (when it reads one)."""
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "datetime"):
        mod.datetime = Frozen
    return mod


def cluster_routes():
    return [
        (("get", "nodes.metrics.k8s.io"), ok(load("node_metrics"))),
        (("get", "nodes"), ok(load("nodes"))),
        (("get", "pods"), ok(load("pods"))),
        (("get", "events"), ok(load("node_events"))),
    ]


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert common.parse_duration("90s").total_seconds() == 90
    assert common.parse_duration("1h30m").total_seconds() == 5400
    assert common.parse_duration("500ms").total_seconds() == 0.5
    assert common.parse_duration("2d").total_seconds() == 172800
    for bad in ("", "soon", "10", "5x", "1w"):
        with pytest.raises(RwError) as exc:
            common.parse_duration(bad)
        assert exc.value.code == "invalid_input"


def test_whole_cores_are_not_millicores():
    # The classic check stripped an "m" and read allocatable "8" as 8 millicores.
    assert common.cpu_millicores("8") == 8000
    assert common.cpu_millicores("4") == 4000
    assert common.cpu_millicores("500m") == 500
    assert common.cpu_millicores("1.5") == 1500
    assert common.cpu_millicores("250000000n") == 250
    assert common.cpu_millicores("214858307n") == pytest.approx(214.858, abs=0.001)
    assert common.cpu_millicores("100u") == 0.1
    assert common.cpu_millicores(None) is None


def test_memory_suffixes():
    assert common.memory_bytes("4012892Ki") == 4012892 * 1024
    assert common.memory_bytes("128Mi") == 128 * 2**20
    assert common.memory_bytes("2Gi") == 2 * 2**30
    assert common.memory_bytes("512K") == 512_000
    assert common.memory_bytes("256M") == 256_000_000
    assert common.memory_bytes("2G") == 2_000_000_000
    assert common.memory_bytes("129e6") == 129_000_000
    assert common.memory_bytes("1.5Gi") == 1610612736
    assert common.memory_bytes(None) is None
    with pytest.raises(ValueError):
        common.parse_quantity("lots")


# ------------------------------------------------------ node-conditions ----


def test_healthy_node_has_no_conditions_taints_or_cordon():
    views, counts = n.node_conditions(load("nodes")["items"], NOW, 200)
    (v,) = views
    assert v["name"] == NODE
    assert v["ready"]["status"] == "True" and v["ready"]["reason"] == "KubeletReady"
    assert v["ready"]["lastTransition"] == "2026-11-03T17:32:56Z"
    assert v["conditions"] == []
    assert v["unschedulable"] is False and v["taints"] == []
    assert v["kubeletVersion"] == "v1.37.0"
    assert v["ageSeconds"] > 0
    assert counts == {
        "total": 1,
        "ready": 1,
        "notReady": 0,
        "unschedulable": 0,
        "withConditions": 0,
    }


def test_not_ready_pressure_cordon_and_taints_are_reported_and_sorted_first():
    sick = node(name="a-sick")
    set_condition(sick, "Ready", "False", "KubeletNotReady", "container runtime is down")
    set_condition(sick, "MemoryPressure", "True", "KubeletHasInsufficientMemory", "low memory")
    set_condition(sick, "DiskPressure", "Unknown", "NodeStatusUnknown", "Kubelet stopped posting")
    sick["spec"]["unschedulable"] = True
    sick["spec"]["taints"] = [
        {"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule", "timeAdded": "x"},
        {"key": "dedicated", "value": "gpu", "effect": "NoExecute"},
    ]
    healthy = node(name="0-healthy")
    views, counts = n.node_conditions([healthy, sick], NOW, 200)
    assert [v["name"] for v in views] == ["a-sick", "0-healthy"]
    v = views[0]
    assert v["ready"]["status"] == "False" and v["ready"]["reason"] == "KubeletNotReady"
    assert {c["type"]: c["status"] for c in v["conditions"]} == {
        "MemoryPressure": "True",
        "DiskPressure": "Unknown",
    }
    assert v["unschedulable"] is True
    assert v["taints"] == [
        {"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule"},
        {"key": "dedicated", "effect": "NoExecute"},
    ]
    assert (
        counts["notReady"] == 1 and counts["unschedulable"] == 1 and counts["withConditions"] == 1
    )


def test_network_unavailable_and_pid_pressure_when_not_false():
    doc = node()
    set_condition(doc, "NetworkUnavailable", "True", "NoRouteCreated")
    set_condition(doc, "PIDPressure", "True", "KubeletHasInsufficientPID")
    view = n.node_view(doc, NOW)
    assert {c["type"] for c in view["conditions"]} == {"NetworkUnavailable", "PIDPressure"}
    # An absent NetworkUnavailable (most clusters) is not reported at all.
    assert n.node_view(node(), NOW)["conditions"] == []


def test_node_with_no_ready_condition_counts_as_not_ready():
    doc = node()
    doc["status"]["conditions"] = [c for c in doc["status"]["conditions"] if c["type"] != "Ready"]
    view = n.node_view(doc, NOW)
    assert view["ready"]["status"] is None
    _views, counts = n.node_conditions([doc], NOW, 200)
    assert counts["notReady"] == 1


def test_conditions_cap_truncate_and_nest():
    docs = [node(name=f"n{i:02}") for i in range(5)]
    views, counts = n.node_conditions(docs, NOW, 3)
    assert len(views) == 3 and counts["total"] == 5
    doc = node()
    set_condition(doc, "Ready", "False", "KubeletNotReady", "x" * 5000)
    ready = n.node_view(doc, NOW)["ready"]
    assert len(ready["message"]) == 1024 and ready["truncated"] is True
    doc["spec"]["taints"] = [{"key": f"k{i}", "effect": "NoSchedule"} for i in range(80)]
    assert len(n.node_view(doc, NOW)["taints"]) == 50


# -------------------------------------------------------- node-restarts ----


def test_registered_node_events_grouped_per_node_with_counts_and_last_time():
    rows = n.node_events(load("node_events")["items"], NOW, "2h", 200)
    (row,) = rows
    assert row["node"] == NODE and row["total"] == 3
    (reason,) = row["reasons"]
    assert reason["reason"] == "RegisteredNode" and reason["count"] == 3
    assert reason["firstSeen"] == "2026-11-03T17:58:51Z"
    assert reason["lastSeen"] == row["lastSeen"] == "2026-11-03T18:29:41Z"
    assert reason["reportingComponent"] == "node-controller"
    # Inside the window only: a 5 minute window at 18:40 sees none of them.
    assert n.node_events(load("node_events")["items"], NOW, "5m", 200) == []


def test_restart_reasons_kubelet_starting_and_other_events():
    events = [
        event("NodeNotReady", name="a", last="2026-11-03T18:35:00Z"),
        event("NodeReady", name="a", last="2026-11-03T18:38:00Z", component="kubelet"),
        event("Rebooted", name="a", last="2026-11-03T18:36:00Z", component="kubelet"),
        event("Starting", name="a", last="2026-11-03T18:37:00Z", component="kubelet"),
        event("Starting", name="b", last="2026-11-03T18:39:00Z", component="kube-proxy"),
        event("NodeHasSufficientMemory", name="b", last="2026-11-03T18:39:00Z"),
        event("NodeNotReady", name="c", last="2026-11-03T16:00:00Z"),  # outside the window
    ]
    events.append(copy.deepcopy(events[0]))
    events[-1]["involvedObject"]["kind"] = "Pod"
    rows = n.node_events(events, NOW, "1h", 200)
    assert [r["node"] for r in rows] == ["a"]  # b only has kube-proxy Starting; c is too old
    assert {x["reason"]: x["count"] for x in rows[0]["reasons"]} == {
        "NodeReady": 1,
        "Starting": 1,
        "Rebooted": 1,
        "NodeNotReady": 1,
    }
    assert rows[0]["total"] == 4 and rows[0]["lastSeen"] == "2026-11-03T18:38:00Z"


def test_event_counter_and_events_k8s_io_shape():
    # events.k8s.io events have eventTime/series and no lastTimestamp; the classic
    # awk date comparison on lastTimestamp dropped them.
    e = event("NodeNotReady", name="a")
    e["lastTimestamp"] = None
    e["firstTimestamp"] = None
    e["eventTime"] = "2026-11-03T18:20:00.000000Z"
    e["series"] = {"count": 7, "lastObservedTime": "2026-11-03T18:33:00.000000Z"}
    (row,) = n.node_events([e], NOW, "1h", 200)
    assert row["reasons"][0]["count"] == 7 and row["lastSeen"] == "2026-11-03T18:33:00Z"
    assert row["reasons"][0]["firstSeen"] == "2026-11-03T18:20:00Z"


def test_events_order_cap_and_truncation():
    events = [
        event("NodeNotReady", name=f"n{i}", last=f"2026-11-03T18:{10 + i}:00Z") for i in range(5)
    ]
    events[4]["message"] = "m" * 3000
    rows = n.node_events(events, NOW, "1h", 3)
    assert [r["node"] for r in rows] == ["n4", "n3", "n2"]
    assert len(rows[0]["reasons"][0]["message"]) == 1024 and rows[0]["reasons"][0]["truncated"]


def test_ready_transitions_within_window():
    flapped = node(name="flapped")
    set_condition(flapped, "Ready", "True", "KubeletReady", transition="2026-11-03T18:20:00Z")
    stable = node(name="stable")
    set_condition(stable, "Ready", "True", transition="2026-11-03T10:00:00Z")
    down = node(name="down")
    set_condition(down, "Ready", "Unknown", "NodeStatusUnknown", transition="2026-11-03T18:35:00Z")
    rows = n.ready_transitions([flapped, stable, down], NOW, "1h")
    assert [(x["node"], x["status"], x["secondsAgo"]) for x in rows] == [
        ("down", "Unknown", 300),
        ("flapped", "True", 1200),
    ]
    assert n.ready_transitions([load("nodes")["items"][0]], NOW, "2h")[0]["node"] == NODE
    assert n.ready_transitions([load("nodes")["items"][0]], NOW, "1h") == []


# ------------------------------------------------------ node-utilization ----


def test_utilization_from_synthetic_node_metrics():
    rows, inspected = r.utilization(load("nodes")["items"], load("node_metrics")["items"], 90, 200)
    (row,) = rows
    assert row["cpu"]["allocatableMillicores"] == 4000  # allocatable "4" cores
    assert row["cpu"]["usageMillicores"] == pytest.approx(214.858, abs=0.001)
    assert row["cpu"]["percent"] == 5.4 and row["cpu"]["atOrAbove"] is False
    assert row["memory"]["usageBytes"] == 1394572 * 1024
    assert row["memory"]["allocatableBytes"] == 4012892 * 1024
    assert row["memory"]["percent"] == 34.8 and row["atOrAbove"] is False
    assert row["window"] == "20.021s" and inspected == {
        "nodes": 1,
        "withMetrics": 1,
        "utilizationPercent": 90,
    }


def metric(name, cpu, memory):
    return {
        "metadata": {"name": name},
        "timestamp": "2026-11-03T18:39:32Z",
        "window": "20s",
        "usage": {"cpu": cpu, "memory": memory},
    }


def test_utilization_at_or_above_threshold_whole_core_allocatable():
    hot = node(name="hot")  # allocatable cpu "4", memory 4012892Ki
    idle = node(name="idle")
    # The classic read allocatable "8" as 8 millicores, so any usage looked >= 90%.
    big = node(name="big")
    big["status"]["allocatable"]["cpu"] = "8"
    metrics = [
        metric("hot", "3700m", "3800000Ki"),  # 92.5% cpu, 94.7% memory
        metric("idle", "40m", "100000Ki"),
        metric("big", "100m", "100000Ki"),  # 1.25% of 8 cores
    ]
    rows, _ = r.utilization([idle, hot, big], metrics, 90, 200)
    by_name = {x["name"]: x for x in rows}
    assert [x["name"] for x in rows][0] == "hot"  # busiest first
    assert by_name["hot"]["cpu"]["percent"] == 92.5 and by_name["hot"]["cpu"]["atOrAbove"] is True
    assert by_name["hot"]["memory"]["atOrAbove"] is True and by_name["hot"]["atOrAbove"] is True
    assert by_name["big"]["cpu"]["allocatableMillicores"] == 8000
    assert by_name["big"]["cpu"]["percent"] == 1.2 and by_name["big"]["atOrAbove"] is False
    assert by_name["idle"]["atOrAbove"] is False
    # The setting moves the line.
    rows, _ = r.utilization([hot], metrics, 95, 200)
    assert rows[0]["cpu"]["atOrAbove"] is False and rows[0]["memory"]["atOrAbove"] is False
    # Exactly at the threshold counts.
    exact = r.utilization([hot], [metric("hot", "3600m", "1Ki")], 90, 200)[0][0]
    assert exact["cpu"]["percent"] == 90.0 and exact["cpu"]["atOrAbove"] is True


def test_node_without_metrics_is_unknown_not_idle():
    # The classic awk turned "<unknown>" usage into 0, so a node without metrics read as idle.
    rows, inspected = r.utilization([node(name="new")], [], 90, 200)
    assert rows[0]["cpu"]["usageMillicores"] is None and rows[0]["cpu"]["percent"] is None
    assert rows[0]["atOrAbove"] is None and inspected["withMetrics"] == 0


def test_script_node_utilization_without_metrics_api_raises_dependency():
    # The classic `kubectl top nodes | awk | jq` printed [] when top failed.
    ctx = FakeCtx(
        [(("get", "nodes.metrics.k8s.io"), NO_METRICS), (("get", "nodes"), ok(load("nodes")))]
    )
    with pytest.raises(RwError) as exc:
        script("node_utilization").main(ctx)
    assert exc.value.code == "dependency" and exc.value.message == "metrics.k8s.io not available"


def test_script_node_utilization_ok_and_forbidden_metrics():
    ctx = FakeCtx(cluster_routes())
    out = script("node_utilization").main(ctx)
    assert out["nodes"][0]["name"] == NODE and out["inspected"]["withMetrics"] == 1
    ctx = FakeCtx(
        [(("get", "nodes.metrics.k8s.io"), FORBIDDEN), (("get", "nodes"), ok(load("nodes")))]
    )
    with pytest.raises(RwError) as exc:
        script("node_utilization").main(ctx)
    assert exc.value.code == "permission_denied"


# ------------------------------------------------------------ overcommit ----


def mc(text):
    return int(text[:-1]) if text.endswith("m") else int(text) * 1000


def mi(text):
    return int(text[:-2]) * 2**20


def test_captured_pods_sum_requests_and_limits_for_active_pods_only():
    pods = load("pods")["items"]
    rows, inspected = r.overcommit(load("nodes")["items"], pods, 200)
    (row,) = rows
    active = [p for p in pods if p["status"]["phase"] not in ("Succeeded", "Failed")]
    assert len(pods) - len(active) == 2  # the two Failed job pods are not counted
    assert row["pods"] == len(active) and inspected["pods"] == len(active)
    want_cpu = want_mem = want_lim_mem = 0
    for p in active:
        for c in p["spec"]["containers"]:
            res = c.get("resources", {})
            want_cpu += mc(res.get("requests", {}).get("cpu", "0m"))
            want_mem += mi(res.get("requests", {}).get("memory", "0Mi"))
            want_lim_mem += mi(res.get("limits", {}).get("memory", "0Mi"))
    assert row["cpu"]["requestsMillicores"] == want_cpu
    assert row["memory"]["requestsBytes"] == want_mem
    assert row["memory"]["limitsBytes"] == want_lim_mem
    assert row["cpu"]["limitsMillicores"] == 0 and row["cpu"]["containersWithoutLimits"] > 0
    assert row["cpu"]["requestsPercent"] == round(want_cpu / 4000 * 100, 1)
    assert row["limitsAboveAllocatable"] is False and row["requestsAboveAllocatable"] is False
    assert row["podCapacity"] == 110


def test_overcommitted_node_limits_above_allocatable():
    # Allocatable is 4 cores / ~3.8Gi; three pods with 2-core, 1.5Gi limits each.
    pods = [
        pod(f"p{i}", containers=[({"cpu": "100m"}, {"cpu": "2", "memory": "1536Mi"})])
        for i in range(3)
    ]
    (row,), _ = r.overcommit([node()], pods, 200)
    assert row["cpu"]["limitsMillicores"] == 6000 and row["cpu"]["limitsAboveAllocatable"] is True
    assert row["cpu"]["limitsPercent"] == 150.0
    assert row["memory"]["limitsBytes"] == 3 * 1536 * 2**20
    assert row["memory"]["limitsAboveAllocatable"] is True
    assert row["limitsAboveAllocatable"] is True
    assert (
        row["cpu"]["requestsAboveAllocatable"] is False and row["requestsAboveAllocatable"] is False
    )


def test_only_memory_limits_above_allocatable_sets_the_node_flag():
    pods = [pod("p", containers=[({}, {"memory": "8Gi"})])]
    (row,), _ = r.overcommit([node()], pods, 200)
    assert row["cpu"]["limitsAboveAllocatable"] is False
    assert row["memory"]["limitsAboveAllocatable"] is True and row["limitsAboveAllocatable"] is True


def test_quantity_suffixes_in_sums():
    pods = [
        pod("a", containers=[({"cpu": "2", "memory": "1G"}, {"cpu": "2500m", "memory": "512M"})]),
        pod("b", containers=[({"cpu": "0.5", "memory": "1Gi"}, {"memory": "256Ki"})]),
        pod("c", containers=[({"memory": "10K"}, {})]),
    ]
    (row,), _ = r.overcommit([node()], pods, 200)
    assert row["cpu"]["requestsMillicores"] == 2500 and row["cpu"]["limitsMillicores"] == 2500
    assert row["memory"]["requestsBytes"] == 10**9 + 2**30 + 10_000
    assert row["memory"]["limitsBytes"] == 512_000_000 + 256 * 1024


def test_init_containers_use_the_effective_request_rule():
    # app 100m+100m; a regular init container asks 500m (peak 500m > 200m); a native
    # sidecar adds 50m to the steady state and to the init peak.
    p = pod(
        "p",
        containers=[({"cpu": "100m"}, {}), ({"cpu": "100m"}, {})],
        init=[(({"cpu": "50m"}, {}), True), (({"cpu": "500m"}, {}), False)],
    )
    assert r.effective(p, "requests", "cpu")[0] * 1000 == 550  # init peak 50m sidecar + 500m
    p2 = pod(
        "p2",
        containers=[({"cpu": "300m"}, {})],
        init=[(({"cpu": "50m"}, {}), True), (({"cpu": "100m"}, {}), False)],
    )
    assert r.effective(p2, "requests", "cpu")[0] * 1000 == 350  # app + sidecar beats init peak 150m
    p3 = pod("p3", containers=[({"cpu": "100m"}, {})], init=[(({"cpu": "1"}, {}), False)])
    assert r.effective(p3, "requests", "cpu")[0] * 1000 == 1000
    (row,), _ = r.overcommit([node()], [p], 200)
    assert row["cpu"]["requestsMillicores"] == 550


def test_pod_overhead_is_added_and_missing_values_are_counted():
    p = pod(
        "p",
        containers=[({"cpu": "100m"}, {}), ({}, {})],
        overhead={"cpu": "250m", "memory": "64Mi"},
    )
    (row,), _ = r.overcommit([node()], [p], 200)
    assert row["cpu"]["requestsMillicores"] == 350
    assert row["memory"]["requestsBytes"] == 64 * 2**20
    assert (
        row["cpu"]["containersWithoutRequests"] == 1 and row["cpu"]["containersWithoutLimits"] == 2
    )


def test_unscheduled_terminated_and_other_node_pods_are_not_counted():
    pods = [
        pod("running", containers=[({"cpu": "1"}, {})]),
        pod(
            "pending-bound", phase="Pending", containers=[({"cpu": "1"}, {})]
        ),  # reserved on the node
        pod("pending-free", node_name=None, phase="Pending", containers=[({"cpu": "1"}, {})]),
        pod("done", phase="Succeeded", containers=[({"cpu": "1"}, {})]),
        pod("failed", phase="Failed", containers=[({"cpu": "1"}, {})]),
        pod("elsewhere", node_name="gone", containers=[({"cpu": "1"}, {})]),
    ]
    (row,), inspected = r.overcommit([node()], pods, 200)
    assert row["pods"] == 2 and row["cpu"]["requestsMillicores"] == 2000
    assert inspected == {
        "nodes": 1,
        "pods": 2,
        "podsUnscheduled": 1,
        "podsOnOtherNodes": 1,
    }


def test_overcommit_orders_over_limit_nodes_first_and_caps():
    over = node(name="z-over")
    ok_node = node(name="a-ok")
    pods = [pod("big", node_name="z-over", containers=[({}, {"cpu": "16"})])]
    rows, _ = r.overcommit([ok_node, over], pods, 200)
    assert [x["name"] for x in rows] == ["z-over", "a-ok"]
    assert len(r.overcommit([ok_node, over], pods, 1)[0]) == 1


# --------------------------------------------------------------- scripts ----


def test_script_node_conditions_cluster_and_node_targets():
    ctx = FakeCtx(cluster_routes())
    out = script("node_conditions").main(ctx)
    assert [v["name"] for v in out["nodes"]] == [NODE] and out["counts"]["ready"] == 1
    assert ctx.calls == [["kubectl", "get", "nodes", "-o", "json"]]
    one = load("nodes")["items"][0]
    ctx = FakeCtx([(("get", "nodes", NODE), ok(one))], rtype="kubernetes/node", name=NODE)
    out = script("node_conditions").main(ctx)
    assert out["nodes"][0]["name"] == NODE
    assert ctx.calls == [["kubectl", "get", "nodes", NODE, "-o", "json"]]


def test_target_type_falls_back_to_path_and_rejects_other_types():
    path_ctx = SimpleNamespace(
        resource=SimpleNamespace(type=None, path="kubernetes/clusters/c/nodes/n1", name="n1")
    )
    assert fetch.target_type(path_ctx) == "kubernetes/node"
    path_ctx.resource.path = "kubernetes/clusters/c"
    assert fetch.target_type(path_ctx) == "kubernetes/cluster"
    ctx = FakeCtx([], rtype="kubernetes/namespace")
    for name in ("node_conditions", "node_restarts", "node_utilization", "overcommit"):
        with pytest.raises(RwError) as exc:
            script(name).main(ctx)
        assert exc.value.code == "invalid_input"
    assert ctx.calls == []


@pytest.mark.parametrize(
    "name, failure, code",
    [
        ("node_conditions", FORBIDDEN, "permission_denied"),
        ("node_conditions", UNREACHABLE, "unreachable"),
        ("overcommit", FORBIDDEN, "permission_denied"),
        ("node_restarts", FORBIDDEN, "permission_denied"),
        ("node_utilization", UNREACHABLE, "unreachable"),
    ],
)
def test_nodes_failure_raises_instead_of_returning_empty(name, failure, code):
    ctx = FakeCtx([(("get", "nodes"), failure)])
    with pytest.raises(RwError) as exc:
        script(name).main(ctx)
    assert exc.value.code == code


def test_node_not_found_propagates():
    ctx = FakeCtx([(("get", "nodes", "nope"), NOT_FOUND)], rtype="kubernetes/node", name="nope")
    for name in ("node_conditions", "node_restarts", "node_utilization", "overcommit"):
        with pytest.raises(RwError) as exc:
            script(name).main(ctx)
        assert exc.value.code == "not_found"


def test_script_overcommit_node_target_filters_pods_server_side():
    one = load("nodes")["items"][0]
    ctx = FakeCtx(
        [(("get", "nodes", NODE), ok(one)), (("get", "pods"), ok(load("pods")))],
        rtype="kubernetes/node",
        name=NODE,
    )
    out = script("overcommit").main(ctx)
    pods_call = [c for c in ctx.calls if "pods" in c][0]
    assert any(f"spec.nodeName={NODE}" in a for a in pods_call) and "-A" in pods_call
    assert out["nodes"][0]["pods"] == 25


def test_script_node_restarts_events_forbidden_still_returns_transitions():
    # The classic piped `kubectl get events | awk | grep > file` with no pipefail, so a failed
    # or forbidden read printed "Total start/stop events: 0".
    flapped = node(name="flapped")
    set_condition(flapped, "Ready", "False", "KubeletNotReady", transition="2026-11-03T18:30:00Z")
    ctx = FakeCtx([(("get", "events"), FORBIDDEN), (("get", "nodes"), ok({"items": [flapped]}))])
    out = script("node_restarts").main(ctx, since="2h")
    assert out["events"] is None
    assert [t["node"] for t in out["transitions"]] == ["flapped"]
    assert ctx.errors == [("permission_denied", "events")]
    events_call = [c for c in ctx.calls if "events" in c][0]
    assert "default" in events_call and "involvedObject.kind=Node" in events_call


def test_script_node_restarts_events_unreachable_raises():
    ctx = FakeCtx([(("get", "events"), UNREACHABLE), (("get", "nodes"), ok(load("nodes")))])
    with pytest.raises(RwError) as exc:
        script("node_restarts").main(ctx)
    assert exc.value.code == "unreachable"


def test_script_node_restarts_bad_since_fails_before_kubectl():
    ctx = FakeCtx([])
    with pytest.raises(RwError) as exc:
        script("node_restarts").main(ctx, since="yesterday")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_script_node_restarts_node_target_selects_the_node_events():
    one = load("nodes")["items"][0]
    ctx = FakeCtx(
        [(("get", "events"), ok(load("node_events"))), (("get", "nodes", NODE), ok(one))],
        rtype="kubernetes/node",
        name=NODE,
    )
    out = script("node_restarts").main(ctx, since="2h")
    assert out["events"]["nodes"][0]["node"] == NODE and out["events"]["nodes"][0]["total"] == 3
    assert [t["node"] for t in out["transitions"]] == [NODE]
    events_call = [c for c in ctx.calls if "events" in c][0]
    assert f"involvedObject.kind=Node,involvedObject.name={NODE}" in events_call


# ------------------------------------------------------------ classifier ----


@pytest.mark.parametrize(
    "rc, stderr, code",
    [
        (
            1,
            'Error from server (Forbidden): nodes is forbidden: User "u" cannot list',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): nodes "x" not found', "not_found"),
        (1, "Unable to connect to the server: dial tcp: connection refused", "unreachable"),
        (1, "dial tcp: lookup api: no such host", "unreachable"),
        (1, "x509: certificate signed by unknown authority", "unreachable"),
        (1, "context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "nodes"', "dependency"),
        (
            1,
            "Error from server (ServiceUnavailable): the server is currently unable to handle the request",
            "dependency",
        ),
        (2, "something odd", "internal"),
    ],
)
def test_classify(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


def test_non_json_output_is_internal():
    ctx = FakeCtx([(("get", "nodes"), (0, "not json", ""))])
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ctx, "get", "nodes")
    assert exc.value.code == "internal"


# ---------------------------------------------------------- bundle shape ----


def test_declared_files_exist_and_schemas_are_valid():
    import yaml
    from jsonschema import Draft202012Validator

    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    assert [t["name"] for t in doc["tasks"]] == [
        "node-conditions",
        "node-restarts",
        "node-utilization",
        "overcommit",
    ]
    for t in doc["tasks"]:
        assert (SKILL / t["script"]).is_file() and t["readOnly"] is True
        for spec in t["outputs"].values():
            schema = json.loads((SKILL / spec["schema"]).read_text())
            Draft202012Validator.check_schema(schema)


def test_outputs_validate_against_schemas():
    import yaml
    from jsonschema import Draft202012Validator

    tasks = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}

    def check(task, out):
        assert set(out) == set(tasks[task]["outputs"])
        for key, value in out.items():
            schema = json.loads((SKILL / tasks[task]["outputs"][key]["schema"]).read_text())
            Draft202012Validator(schema).validate(value)

    sick = node(name="sick")
    set_condition(sick, "Ready", "False", "KubeletNotReady", transition="2026-11-03T18:30:00Z")
    set_condition(sick, "MemoryPressure", "True")
    sick["spec"]["taints"] = [{"key": "k", "effect": "NoSchedule"}]
    both = {"items": [sick, load("nodes")["items"][0]]}
    routes = [
        (("get", "nodes.metrics.k8s.io"), ok(load("node_metrics"))),
        (("get", "nodes"), ok(both)),
        (("get", "pods"), ok(load("pods"))),
        (
            ("get", "events"),
            ok({"items": load("node_events")["items"] + [event("NodeNotReady", name="sick")]}),
        ),
    ]
    check("node-conditions", script("node_conditions").main(FakeCtx(routes)))
    check("node-restarts", script("node_restarts").main(FakeCtx(routes), since="2h"))
    check("node-utilization", script("node_utilization").main(FakeCtx(routes)))
    check("overcommit", script("overcommit").main(FakeCtx(routes)))


# ================================================== review fixes ==========


def labelled(name, **labels):
    doc = node(name=name)
    doc["metadata"]["labels"] = labels
    return doc


# --- 1. removal reasons, capacity, eventsExamined / oldestEventSeen ---------


def test_removal_and_stop_reasons_show_a_node_that_came_and_went():
    reasons = [
        "RemovingNode",
        "DeletingNode",
        "NodeShutdown",
        "TerminatingNode",
        "Shutdown",
        "Removed",
        "Preempting",
        "PreemptScheduled",
        "TerminationEvent",
        "TerminationGracePeriod",
    ]
    events = [event(x, name="gone", last="2026-11-03T18:30:00Z") for x in reasons]
    events += [
        event("TerminatingEvictedPod", name="gone"),
        event("NodeHasSufficientPID", name="gone"),
    ]
    (row,) = n.node_events(events, NOW, "1h", 200)
    assert {x["reason"] for x in row["reasons"]} == set(reasons)
    assert n.is_restart_reason("NodeNotReady") and n.is_restart_reason("Preemption")
    assert not n.is_restart_reason("TerminatingEvictedPod")


@pytest.mark.parametrize(
    "labels, want",
    [
        ({"karpenter.sh/capacity-type": "spot"}, "spot"),
        ({"karpenter.sh/capacity-type": "on-demand"}, "on-demand"),
        ({"eks.amazonaws.com/capacityType": "SPOT"}, "spot"),
        ({"eks.amazonaws.com/capacityType": "ON_DEMAND"}, "on-demand"),
        ({"cloud.google.com/gke-preemptible": "true"}, "preemptible"),
        ({"cloud.google.com/gke-preemptible": "false"}, None),
        ({"cloud.google.com/gke-spot": "true"}, "spot"),
        ({"kubernetes.azure.com/scalesetpriority": "spot"}, "spot"),
        ({"kubernetes.azure.com/scalesetpriority": "regular"}, "on-demand"),
        ({"kubernetes.io/os": "linux"}, None),
    ],
)
def test_capacity_type_from_provider_labels(labels, want):
    assert n.node_view(labelled("x", **labels), NOW)["capacity"] == want


def test_events_output_reports_examined_and_oldest_event_seen():
    events = [
        event("NodeNotReady", name="a", last="2026-11-03T18:35:00Z"),
        event(
            "NodeHasSufficientMemory", name="a", last="2026-11-03T17:50:00Z"
        ),  # not a restart reason
        event("RegisteredNode", name="b", last="2026-11-03T16:00:00Z"),  # outside the window
    ]
    out = n.events_output(events, NOW, "1h", 200)
    assert out["eventsExamined"] == 3 and out["oldestEventSeen"] == "2026-11-03T16:00:00Z"
    assert [r["node"] for r in out["nodes"]] == ["a"]
    empty = n.events_output([], NOW, "1h", 200)
    assert empty == {"eventsExamined": 0, "oldestEventSeen": None, "nodes": []}


# --- 7/8. other conditions and sort order ----------------------------------


def test_other_condition_types_not_false_are_reported():
    doc = node()
    set_condition(doc, "KernelDeadlock", "True", "AskedToReboot", "kernel is stuck")
    set_condition(doc, "FrequentKubeletRestart", "False", "NoFrequentKubeletRestart")
    set_condition(doc, "CorruptDockerOverlay2", "Unknown", "Unknown", "m" * 3000)
    view = n.node_view(doc, NOW)
    other = {c["type"]: c for c in view["otherConditions"]}
    assert set(other) == {"KernelDeadlock", "CorruptDockerOverlay2"}
    assert other["KernelDeadlock"]["reason"] == "AskedToReboot"
    assert len(other["CorruptDockerOverlay2"]["message"]) == 1024
    assert other["CorruptDockerOverlay2"]["truncated"] is True
    assert view["conditions"] == []  # the four standard ones stay in `conditions`
    for i in range(80):
        set_condition(doc, f"Custom{i}", "True")
    assert len(n.node_view(doc, NOW)["otherConditions"]) == 50
    assert n.node_view(node(), NOW)["otherConditions"] == []


def test_node_conditions_sort_puts_interesting_nodes_first_so_the_cap_keeps_them():
    healthy = node(name="a-healthy")
    cordoned = node(name="b-cordoned")
    cordoned["spec"]["unschedulable"] = True
    pressure = node(name="c-pressure")
    set_condition(pressure, "DiskPressure", "True")
    custom = node(name="d-custom")
    set_condition(custom, "KernelDeadlock", "True")
    down = node(name="e-down")
    set_condition(down, "Ready", "False")
    views, counts = n.node_conditions([healthy, cordoned, custom, pressure, down], NOW, 200)
    assert [v["name"] for v in views] == [
        "e-down",
        "c-pressure",
        "d-custom",
        "b-cordoned",
        "a-healthy",
    ]
    assert counts["withConditions"] == 2
    cut, _ = n.node_conditions([healthy, cordoned, custom, pressure, down], NOW, 2)
    assert [v["name"] for v in cut] == ["e-down", "c-pressure"]


# --- 2. per-pod top lists ---------------------------------------------------


def test_top_pods_by_limits_and_requests_with_percent_and_largest_pod():
    pods = [
        pod("small", containers=[({"cpu": "100m", "memory": "64Mi"}, {"cpu": "200m"})]),
        pod("big", containers=[({"cpu": "1"}, {"cpu": "3800m", "memory": "1Gi"})]),
        pod("mem", containers=[({"memory": "2Gi"}, {})]),
        pod("none", containers=[({}, {})]),
    ]
    (row,), _ = r.overcommit([node()], pods, 200, pod_limit_percent=90)
    by_limits = row["topPodsByLimits"]
    assert [p["name"] for p in by_limits] == ["big", "small"]  # pods with no limit are left out
    big = by_limits[0]
    assert big["namespace"] == "default"
    assert big["limits"]["cpuMillicores"] == 3800 and big["limits"]["cpuPercent"] == 95.0
    assert big["limits"]["memoryBytes"] == 2**30
    assert big["podAboveLimitPercent"] is True
    assert by_limits[1]["podAboveLimitPercent"] is False
    assert row["largestPodLimitPercent"] == 95.0 and row["podAboveLimitPercent"] is True
    by_requests = row["topPodsByRequests"]
    assert [p["name"] for p in by_requests][:2] == [
        "mem",
        "big",
    ]  # 2Gi is 52% of memory; 1 core 25%
    assert by_requests[0]["requests"]["memoryPercent"] == pytest.approx(52.3, abs=0.1)
    assert "none" not in [p["name"] for p in by_requests]


def test_top_pods_are_capped_at_five_and_the_threshold_is_a_setting():
    pods = [pod(f"p{i}", containers=[({}, {"cpu": f"{(i + 1) * 100}m"})]) for i in range(8)]
    (row,), _ = r.overcommit([node()], pods, 200, pod_limit_percent=90)
    assert [p["name"] for p in row["topPodsByLimits"]] == ["p7", "p6", "p5", "p4", "p3"]
    assert row["podAboveLimitPercent"] is False and row["largestPodLimitPercent"] == 20.0
    (low,), _ = r.overcommit([node()], pods, 200, pod_limit_percent=15)
    assert low["podAboveLimitPercent"] is True
    assert [p["podAboveLimitPercent"] for p in low["topPodsByLimits"]][:2] == [True, True]
    assert [p["podAboveLimitPercent"] for p in low["topPodsByLimits"]][-1] is False


def test_node_with_no_limits_has_null_largest_pod_percent():
    (row,), _ = r.overcommit([node()], [pod("p", containers=[({"cpu": "1"}, {})])], 200)
    assert row["largestPodLimitPercent"] is None and row["podAboveLimitPercent"] is None
    assert row["topPodsByLimits"] == []


def test_overcommit_sort_keys_keep_interesting_nodes_inside_the_cap():
    a_ok = node(name="a-ok")
    b_pod = node(name="b-pod")  # no sum above allocatable, but one pod is above 90%
    c_req = node(name="c-req")
    d_lim = node(name="d-lim")
    pods = [
        pod("one", node_name="b-pod", containers=[({}, {"cpu": "3800m"})]),
        pod("req", node_name="c-req", containers=[({"cpu": "5"}, {})]),
        pod("lim", node_name="d-lim", containers=[({}, {"cpu": "2"}), ({}, {"cpu": "3"})]),
    ]
    rows, _ = r.overcommit([a_ok, b_pod, c_req, d_lim], pods, 200)
    assert [x["name"] for x in rows] == ["d-lim", "c-req", "b-pod", "a-ok"]
    assert [x["name"] for x in r.overcommit([a_ok, b_pod, c_req, d_lim], pods, 2)[0]] == [
        "d-lim",
        "c-req",
    ]


# --- 3. overhead and limits -------------------------------------------------


def test_overhead_is_added_to_limits_only_when_the_limit_total_is_positive():
    with_limit = pod(
        "a", containers=[({}, {"cpu": "1"})], overhead={"cpu": "250m", "memory": "64Mi"}
    )
    without = pod("b", containers=[({"cpu": "1"}, {})], overhead={"cpu": "250m", "memory": "64Mi"})
    assert r.effective(with_limit, "limits", "cpu")[0] * 1000 == 1250
    assert r.effective(with_limit, "limits", "memory")[0] == 0  # no memory limit: stays 0
    assert r.effective(without, "limits", "cpu")[0] == 0
    assert r.effective(without, "requests", "cpu")[0] * 1000 == 1250  # requests always take it
    (row,), _ = r.overcommit([node()], [without], 200)
    assert row["cpu"]["limitsMillicores"] == 0 and row["memory"]["limitsBytes"] == 0


# --- 4. pod-level resources (1.34+) ----------------------------------------


def test_pod_level_resources_replace_the_container_sum():
    p = pod("p", containers=[({"cpu": "100m"}, {"cpu": "200m"}), ({}, {})])
    p["spec"]["resources"] = {"requests": {"cpu": "2"}, "limits": {"cpu": "4", "memory": "1Gi"}}
    total, missing = r.effective(p, "requests", "cpu")
    assert total * 1000 == 2000 and missing == 0  # not 100m, and no container counted missing
    total, missing = r.effective(p, "limits", "cpu")
    assert total * 1000 == 4000 and missing == 0
    # memory requests are not set at pod level: containers set none either, both counted.
    total, missing = r.effective(p, "requests", "memory")
    assert total == 0 and missing == 2
    # memory limit is set at pod level only: containers set none, not counted missing.
    total, missing = r.effective(p, "limits", "memory")
    assert total == 2**30 and missing == 0


def test_pod_level_request_still_takes_overhead():
    p = pod("p", containers=[({}, {})], overhead={"cpu": "100m"})
    p["spec"]["resources"] = {"requests": {"cpu": "1"}}
    assert r.effective(p, "requests", "cpu")[0] * 1000 == 1100


# --- 5. load_pods server-side selector -------------------------------------


def test_load_pods_filters_terminated_pods_server_side():
    ctx = FakeCtx([(("get", "pods"), ok(load("pods")))])
    fetch.load_pods(ctx)
    (call,) = ctx.calls
    selector = call[call.index("--field-selector") + 1]
    assert selector == "status.phase!=Succeeded,status.phase!=Failed"
    one = FakeCtx([(("get", "pods"), ok(load("pods")))], rtype="kubernetes/node", name=NODE)
    fetch.load_pods(one)
    selector = one.calls[0][one.calls[0].index("--field-selector") + 1]
    assert selector == f"status.phase!=Succeeded,status.phase!=Failed,spec.nodeName={NODE}"


def test_overcommit_task_has_a_longer_timeout_and_no_terminated_count():
    import yaml

    tasks = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
    assert tasks["overcommit"]["timeout"] == "120s"
    assert (
        "podsTerminated"
        not in json.loads((SKILL / "schemas" / "overcommit-inspected.v1.json").read_text())[
            "properties"
        ]
    )
    settings = yaml.safe_load((SKILL / "tasks.yaml").read_text())["settings"]
    assert settings["podLimitPercent"]["default"] == 90


# --- 6. RBAC subset and forbidden pods -------------------------------------

VALUE_FLAGS = {"-n", "--namespace", "-o", "--field-selector", "-l", "--selector", "--context"}
RESOURCES = {
    "nodes": ("", "nodes"),
    "pods": ("", "pods"),
    "events": ("", "events"),
    "nodes.metrics.k8s.io": ("metrics.k8s.io", "nodes"),
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triple one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, with or without --field-selector / -l) -> list.
    Raises on anything else, so a skill that issues a new verb has to extend this mapping.
    """
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    if positional[0] != "get":
        raise AssertionError(f"no RBAC mapping for kubectl {positional[0]}: {argv}")
    group, plural = RESOURCES[positional[1]]
    named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
    return {(group, plural, "get" if named_object else "list")}


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def tasks_by_name():
    import yaml

    return {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


@pytest.mark.parametrize(
    "task", ["node-conditions", "node-restarts", "node-utilization", "overcommit"]
)
def test_declared_permissions_are_exactly_what_the_script_calls(task):
    needed = set()
    for rtype, name in (("kubernetes/cluster", "rw-plugins"), ("kubernetes/node", NODE)):
        one = {"items": [load("nodes")["items"][0]]}
        routes = [
            (("get", "nodes.metrics.k8s.io"), ok(load("node_metrics"))),
            (("get", "nodes", NODE), ok(one["items"][0])),
            (("get", "nodes"), ok(one)),
            (("get", "pods"), ok(load("pods"))),
            (("get", "events"), ok(load("node_events"))),
        ]
        ctx = FakeCtx(routes, rtype=rtype, name=name)
        script(task.replace("-", "_")).main(ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    assert needed == declared(tasks_by_name()[task]), task


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "get", "nodes", "-o", "json"]) == {("", "nodes", "list")}
    assert rbac_needs(["kubectl", "get", "nodes", "n1", "-o", "json"]) == {("", "nodes", "get")}
    assert rbac_needs(
        ["kubectl", "get", "pods", "-A", "--field-selector", "spec.nodeName=n1", "-o", "json"]
    ) == {("", "pods", "list")}
    assert rbac_needs(["kubectl", "get", "nodes.metrics.k8s.io", "-o", "json"]) == {
        ("metrics.k8s.io", "nodes", "list")
    }
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "exec", "p"])


def test_overcommit_forbidden_pods_raises_permission_denied():
    # Nodes are readable but pods are not: nothing useful remains, so the task raises.
    ctx = FakeCtx([(("get", "pods"), FORBIDDEN), (("get", "nodes"), ok(load("nodes")))])
    with pytest.raises(RwError) as exc:
        script("overcommit").main(ctx)
    assert exc.value.code == "permission_denied"


# --- 10. metrics relabel ----------------------------------------------------


def test_metrics_dependency_keeps_its_own_message_when_it_is_not_about_the_resource_type():
    missing_kubectl = (127, "", "kubectl: executable file not found in $PATH")
    ctx = FakeCtx([(("get", "nodes.metrics.k8s.io"), missing_kubectl)])
    with pytest.raises(RwError) as exc:
        fetch.load_node_metrics(ctx)
    assert exc.value.code == "dependency"
    assert exc.value.message != "metrics.k8s.io not available"
    assert "executable file not found" in exc.value.message
    unavailable = (
        1,
        "",
        "Error from server (ServiceUnavailable): the server is currently unable to handle the request",
    )
    ctx = FakeCtx([(("get", "nodes.metrics.k8s.io"), unavailable)])
    with pytest.raises(RwError) as exc:
        fetch.load_node_metrics(ctx)
    assert exc.value.message == "metrics.k8s.io not available"
