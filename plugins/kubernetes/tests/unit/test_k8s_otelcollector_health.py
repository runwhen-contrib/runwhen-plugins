"""Unit tests for k8s-otelcollector-health: the metrics parser, the queue and counter views and the
thin script, against pages captured from the kind-rw-plugins scenario rw-otel (otel-ok exports to a live
sink, otel-broken to a Service with no pods; collector 0.164.0, with the histogram buckets and HELP lines
cut from the captured pages) and synthetic pages in the older and newer naming. Scripts run through
runwhen_capability.testing; no test reaches a cluster."""

import copy
import re
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_otelcollector_health import collector, exporter_queue, k8s, prom
from k8s_otelcollector_health.errors import RwError
from k8s_otelcollector_health.k8s import proxy_path
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-otelcollector-health"
DATA = Path(__file__).parent / "data" / "k8s_otelcollector_health"
NOW = datetime(2026, 11, 3, 23, 30, 0, tzinfo=timezone.utc)
NS = "rw-otel"
TASK = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"][0]
SETTINGS = {
    "maxItems": 200,
    "maxPods": 20,
    "metricsPort": 8888,
    "scheme": "http",
    "queueUtilizationPercent": 80,
}


def load(name):
    return json.loads((DATA / name).read_text())


def text(name):
    return (DATA / name).read_text()


DEPLOY = {r: load(f"deployment_{r}.json") for r in ("ok", "broken")}
PODS = {r: load(f"pods_{r}.json") for r in ("ok", "broken")}
POD = {r: PODS[r]["items"][0]["metadata"]["name"] for r in PODS}
PAGE = {r: text(f"metrics_{r}.txt") for r in ("ok", "broken")}


def script():
    spec = importlib.util.spec_from_file_location(
        "otel_exporter_queue", SKILL / "scripts" / "exporter_queue.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def resource(name="otel-ok", kind="deployments", type_="kubernetes/deployment"):
    res = {
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/{kind}/{name}",
        "cluster": "kind",
        "namespace": NS,
        "name": name,
    }
    if type_:
        res["type"] = type_
    return res


def get_workload(rel, plural="deployments"):
    return ("kubectl", "-n", NS, "get", plural, f"otel-{rel}", "-o", "json")


def get_pods(rel):
    return ("kubectl", "-n", NS, "get", "pods", "-l", f"app=otel-{rel}", "-o", "json")


def metrics(pod, port=8888):
    return ("kubectl", "get", "--raw", proxy_path(NS, pod, port, "metrics"))


def ok(doc):
    return 0, doc if isinstance(doc, str) else json.dumps(doc), ""


RBAC = 'Error from server (Forbidden): pods "%s" is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot get resource "pods/proxy" in API group "" in the namespace "rw-otel"'
REFUSED = (
    1,
    "",
    "Error from server (ServiceUnavailable): error trying to reach service: dial tcp 10.244.0.9:8888: connect: connection refused",
)
NOT_FOUND = (
    1,
    "",
    "Error from server (NotFound): the server could not find the requested resource",
)


def stubs_for(rel, extra=None):
    out = {
        get_workload(rel): ok(DEPLOY[rel]),
        get_pods(rel): ok(PODS[rel]),
        metrics(POD[rel]): ok(PAGE[rel]),
    }
    out.update(extra or {})
    return out


def run(stubs, res=None, settings=None):
    with make_context(
        resource=res or resource(), settings={**SETTINGS, **(settings or {})}, now=NOW
    ) as tc:
        calls = stub_commands(tc, stubs)
        env = run_main(script(), tc)
        return env, tc.errors, list(calls)


def validate(schema_file, value):
    schema = json.loads((SKILL / "schemas" / schema_file).read_text())
    Draft202012Validator.check_schema(schema)
    return [e.message for e in Draft202012Validator(schema).iter_errors(value)]


def check_outputs(env):
    assert set(env["outputs"]) == set(TASK["outputs"])
    for out, spec in TASK["outputs"].items():
        assert validate(Path(spec["schema"]).name, env["outputs"][out]) == [], out


def view(page, pod="p", threshold=80):
    return collector.view(prom.parse(page), pod, threshold)


# ---------------------------------------------------------------- the parser ----


def test_captured_pages_parse():
    for rel in ("ok", "broken"):
        samples = prom.parse(PAGE[rel])
        assert len(samples) > 50
        assert any(s.name == "otelcol_exporter_queue_size" for s in samples)


def test_labels_values_exponents_timestamps_and_exemplars():
    page = "\n".join(
        [
            "# HELP x help",
            "# TYPE x counter",
            'm1{a="b",c="d e, f",} 1.5e+06 1700000000',
            'm2{path="C:\\\\tmp",q="say \\"hi\\"",n="l1\\nl2"} 3',
            'm3 12 # {trace_id="abc"} 1 1700000000',
            "m4{} +Inf",
            "m5 NaN",
            "m6 -2.5E-3",
            "# EOF",
        ]
    )
    s = {x.name: x for x in prom.parse(page)}
    assert s["m1"].labels == {"a": "b", "c": "d e, f"} and s["m1"].value == 1.5e6
    assert s["m2"].labels == {"path": "C:\\tmp", "q": 'say "hi"', "n": "l1\nl2"}
    assert s["m3"].value == 12 and s["m3"].labels == {}
    assert s["m4"].value is None and s["m5"].value is None  # not finite: no number to report
    assert s["m6"].value == -0.0025


@pytest.mark.parametrize(
    "line",
    [
        '{a="b"} 1',
        'm{a="b} 1',
        "m{a=b} 1",
        'm{a="b" 1',
        "m",
        "m x",
        "m 1 2 3",
        'm{a="\\q"} 1',
        "m 1 abc",
        "<html>",
        'm{1a="b"} 1',
    ],
)
def test_lines_that_are_not_samples_raise_naming_the_line(line):
    with pytest.raises(RwError) as exc:
        prom.parse(f"ok 1\n{line}\n")
    assert exc.value.code == "internal" and "line 2" in exc.value.message


def test_a_page_over_the_size_limit_is_refused():
    with pytest.raises(RwError):
        prom.parse("x" * (prom.MAX_BYTES + 1))


def test_empty_pages_have_no_samples():
    assert prom.parse("") == [] and prom.parse("# EOF\n") == []


# ----------------------------------------------------------------- the views ----


def test_healthy_collector_has_empty_queues_and_zero_counters():
    queues, counters, facts = view(PAGE["ok"], "ok-0")
    assert [
        (q["exporter"], q["dataType"], q["size"], q["capacity"], q["full"]) for q in queues
    ] == [
        ("otlp/sink", "logs", 0, 10, False),
        ("otlp/sink", "metrics", 0, 10, False),
        ("otlp/sink", "traces", 0, 10, False),
    ]
    assert all(q["atOrAboveThreshold"] is False and q["utilizationPercent"] == 0 for q in queues)
    assert {c["counter"] for c in counters} == {"refused"} and all(
        c["value"] == 0 for c in counters
    )
    assert facts["uptimeSeconds"] > 0 and facts["seriesCount"] > 50


def test_broken_collector_shows_full_queues_and_failure_counters():
    queues, counters, _ = view(PAGE["broken"], "br-0")
    dead = [q for q in queues if q["exporter"] == "otlp/dead"]
    assert [
        (q["dataType"], q["size"], q["capacity"], q["utilizationPercent"], q["full"]) for q in dead
    ] == [
        ("logs", 10, 10, 100, True),
        ("metrics", 10, 10, 100, True),
        ("traces", 10, 10, 100, True),
    ]
    assert queues[0]["exporter"] == "otlp/dead"  # worst first
    by = {(c["component"], c["name"], c["counter"], c["dataType"]): c for c in counters}
    assert by[("exporter", "otlp/dead", "enqueue_failed", "traces")]["value"] > 0
    give_up = by[("exporter", "otlp/giveup", "send_failed", "traces")]
    assert give_up["value"] > 0 and give_up["errorTypes"] == ["Unavailable"]
    assert by[("receiver", "otlp", "refused", "logs")]["value"] > 0
    assert counters[0]["value"] >= counters[-1]["value"]


OLD = """
otelcol_exporter_queue_size{exporter="otlp",service_instance_id="x"} 450
otelcol_exporter_queue_capacity{exporter="otlp",service_instance_id="x"} 500
otelcol_exporter_send_failed_spans{exporter="otlp"} 7
otelcol_exporter_send_failed_metric_points{exporter="otlp"} 0
otelcol_exporter_enqueue_failed_log_records{exporter="otlp"} 3
otelcol_receiver_refused_spans{receiver="otlp",transport="grpc"} 11
otelcol_receiver_refused_spans{receiver="otlp",transport="http"} 4
otelcol_processor_dropped_spans{processor="memory_limiter"} 2
otelcol_processor_refused_metric_points{processor="memory_limiter"} 5
otelcol_process_uptime 3600.5
"""


def normalised(page):
    queues, counters, facts = view(page)
    return queues, counters, facts["uptimeSeconds"]


def test_older_names_without_data_type_label():
    queues, counters, up = normalised(OLD)
    assert queues == [
        {"pod": "p", "exporter": "otlp", "dataType": None, "size": 450, "capacity": 500, "utilizationPercent": 90,
         "atOrAboveThreshold": True, "full": False}
    ]  # fmt: skip
    by = {(c["component"], c["name"], c["counter"], c["dataType"]): c["value"] for c in counters}
    assert by == {
        ("exporter", "otlp", "send_failed", "traces"): 7,
        ("exporter", "otlp", "send_failed", "metrics"): 0,
        ("exporter", "otlp", "enqueue_failed", "logs"): 3,
        ("receiver", "otlp", "refused", "traces"): 15,  # grpc and http transports add up
        ("processor", "memory_limiter", "dropped", "traces"): 2,
        ("processor", "memory_limiter", "refused", "metrics"): 5,
    }
    assert up == 3600.5


def test_the_total_suffix_and_a_missing_otelcol_prefix_read_the_same():
    def rename(page, prefix, suffix):
        return re.sub(r"^otelcol_(\S+?)(\{| )", rf"{prefix}\1{suffix}\2", page.strip(), flags=re.M)

    newest = rename(OLD, "otelcol_", "_total")  # otelcol_exporter_send_failed_spans_total
    bare = rename(OLD, "", "")  # exporter_send_failed_spans
    bare_total = rename(OLD, "", "_total")
    assert (
        "otelcol_exporter_send_failed_spans_total{" in newest
        and "\nexporter_queue_size{" in "\n" + bare
    )
    assert "process_uptime_total 3600.5" in bare_total
    for page in (newest, bare, bare_total):
        assert view(page) == view(OLD)


def test_uptime_in_seconds_unit_name_is_read():
    page = 'otelcol_process_uptime_seconds_total 12\notelcol_exporter_queue_size{exporter="e"} 1\n'
    assert view(page)[2]["uptimeSeconds"] == 12


def test_queue_size_without_capacity_or_the_reverse_has_no_utilisation():
    for page in (
        'otelcol_exporter_queue_size{exporter="e",data_type="traces"} 5',
        'otelcol_exporter_queue_capacity{exporter="e",data_type="traces"} 5',
    ):
        (q,) = view(page + "\notelcol_process_uptime 1")[0]
        assert (
            q["utilizationPercent"] is None
            and q["atOrAboveThreshold"] is None
            and q["full"] is None
        )


def test_a_full_queue_is_full_even_below_the_threshold_setting_of_100_and_the_threshold_is_a_setting():
    page = 'otelcol_exporter_queue_size{exporter="e",data_type="traces"} 5\notelcol_exporter_queue_capacity{exporter="e",data_type="traces"} 10\n'
    (q,) = view(page, threshold=50)[0]
    assert (q["utilizationPercent"], q["atOrAboveThreshold"], q["full"]) == (50, True, False)
    (q,) = view(page, threshold=51)[0]
    assert q["atOrAboveThreshold"] is False
    full = page.replace("} 5", "} 10")
    (q,) = view(full, threshold=100)[0]
    assert q["atOrAboveThreshold"] is True and q["full"] is True


def test_non_finite_values_are_null_not_a_number():
    page = 'otelcol_exporter_queue_size{exporter="e",data_type="logs"} NaN\notelcol_exporter_queue_capacity{exporter="e",data_type="logs"} 10\notelcol_exporter_send_failed_spans{exporter="e"} +Inf\n'
    queues, counters, _ = view(page)
    assert queues[0]["size"] is None and queues[0]["utilizationPercent"] is None
    assert counters[0]["value"] is None


def test_float_counter_values_are_read_not_compared_as_integers():
    # the classic compared the raw text with -gt and broke on 5e+02 or 500.0
    page = 'otelcol_exporter_queue_size{exporter="e",data_type="traces"} 5e+02\notelcol_exporter_queue_capacity{exporter="e",data_type="traces"} 1000.0\n'
    (q,) = view(page + "otelcol_process_uptime 1")[0]
    assert (q["size"], q["capacity"], q["utilizationPercent"]) == (500, 1000, 50)


def test_a_page_that_is_not_a_collectors_is_an_error_not_a_clean_collector():
    for page in ("", "# EOF\n", 'http_requests_total{code="200"} 5\ngo_goroutines 12\n', "up 1"):
        with pytest.raises(RwError) as exc:
            view(page)
        assert exc.value.code == "internal"


def test_series_without_their_exporter_or_receiver_label_are_an_error():
    for line in (
        "otelcol_exporter_queue_size 5",
        "otelcol_exporter_send_failed_spans 1",
        'otelcol_receiver_refused_spans{transport="x"} 1',
    ):
        with pytest.raises(RwError):
            view(line + "\notelcol_process_uptime 1")


def test_unknown_data_kind_suffixes_are_kept_and_unrelated_series_ignored():
    page = 'otelcol_exporter_send_failed_profile_samples{exporter="e"} 2\notelcol_exporter_sent_spans{exporter="e"} 9\notelcol_process_uptime 1\n'
    (c,) = view(page)[1]
    assert (c["dataType"], c["value"]) == ("profile_samples", 2)


# ------------------------------------------------------------ the task ----


@pytest.mark.parametrize("rel", ["ok", "broken"])
def test_exporter_queue_of_each_collector(rel):
    env, errors, _ = run(stubs_for(rel), resource(f"otel-{rel}"))
    assert env["status"] == "ok" and errors == []
    check_outputs(env)
    o = env["outputs"]
    assert o["pods"]["queriedCount"] == 1 and o["pods"]["items"][0]["queried"] is True
    if rel == "ok":
        assert o["counters"]["nonZeroCount"] == 0 and not any(
            q["full"] for q in o["queues"]["items"]
        )
    else:
        assert any(q["full"] and q["exporter"] == "otlp/dead" for q in o["queues"]["items"])
        assert o["counters"]["nonZeroCount"] >= 9


def test_daemonset_and_statefulset_targets_and_the_path_fallback():
    for plural, rtype in (
        ("daemonsets", "kubernetes/daemonset"),
        ("statefulsets", "kubernetes/statefulset"),
    ):
        stubs = stubs_for("ok") | {
            ("kubectl", "-n", NS, "get", plural, "otel-ok", "-o", "json"): ok(DEPLOY["ok"])
        }
        env, errors, _ = run(stubs, resource("otel-ok", plural, rtype))
        assert env["status"] == "ok", errors
        env, errors, _ = run(stubs, resource("otel-ok", plural, None))
        assert env["status"] == "ok", errors


def test_the_metrics_port_and_scheme_are_settings():
    page = metrics(POD["ok"], 9464)
    stubs = {
        get_workload("ok"): ok(DEPLOY["ok"]),
        get_pods("ok"): ok(PODS["ok"]),
        page: ok(PAGE["ok"]),
    }
    env, _, calls = run(stubs, resource("otel-ok"), {"metricsPort": 9464})
    assert env["status"] == "ok" and page in calls
    https = ("kubectl", "get", "--raw", proxy_path(NS, POD["ok"], 8888, "metrics", "https"))
    stubs = {
        get_workload("ok"): ok(DEPLOY["ok"]),
        get_pods("ok"): ok(PODS["ok"]),
        https: ok(PAGE["ok"]),
    }
    env, _, _ = run(stubs, resource("otel-ok"), {"scheme": "https"})
    assert env["status"] == "ok"


def test_queue_threshold_setting_changes_the_flag_not_the_facts():
    low = {"queueUtilizationPercent": 0}
    env, _, _ = run(stubs_for("ok"), resource("otel-ok"), low)
    assert all(q["atOrAboveThreshold"] is True for q in env["outputs"]["queues"]["items"])
    assert env["outputs"]["queues"]["thresholdPercent"] == 0


def two_pods(rel, second_ip="10.244.9.9"):
    pods = copy.deepcopy(PODS[rel])
    other = copy.deepcopy(pods["items"][0])
    other["metadata"]["name"] = f"otel-{rel}-second"
    other["status"]["podIP"] = second_ip
    pods["items"].append(other)
    return pods, other["metadata"]["name"]


def test_each_pod_has_its_own_counters_and_a_failing_pod_makes_the_result_partial():
    pods, second = two_pods("broken")
    stubs = stubs_for("broken", {get_pods("broken"): ok(pods), metrics(second): REFUSED})
    env, errors, _ = run(stubs, resource("otel-broken"))
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("unreachable", f"pod/{second}")
    ]
    assert "localhost" in errors[0]["hint"]
    o = env["outputs"]["pods"]
    assert (o["count"], o["queriedCount"]) == (2, 1)
    entry = {p["pod"]: p for p in o["items"]}[second]
    assert entry["queried"] is False and entry["notQueriedReason"].startswith("unreachable")
    assert {q["pod"] for q in env["outputs"]["queues"]["items"]} == {POD["broken"]}


def test_two_pods_read_are_listed_separately():
    pods, second = two_pods("broken")
    stubs = stubs_for("broken", {get_pods("broken"): ok(pods), metrics(second): ok(PAGE["ok"])})
    env, errors, _ = run(stubs, resource("otel-broken"))
    assert env["status"] == "ok" and errors == []
    assert {q["pod"] for q in env["outputs"]["queues"]["items"]} == {POD["broken"], second}
    assert env["outputs"]["queues"]["items"][0]["pod"] == POD["broken"]  # the full ones come first


@pytest.mark.parametrize(
    "answer,code",
    [
        (REFUSED, "unreachable"),
        (NOT_FOUND, "dependency"),
        ((1, "", RBAC % "x"), "permission_denied"),
        ((1, "", "Error from server (Forbidden): unknown"), "permission_denied"),
        (
            (
                1,
                "",
                'Error from server (InternalError): an error on the server ("unknown") has prevented the request from succeeding',
            ),
            "internal",
        ),
        ((1, "", "Error from server (Timeout): request did not complete"), "timeout"),
        (ok("<html>login</html>"), "internal"),
        (ok(""), "internal"),
        (ok("up 1\n"), "internal"),
    ],
)
def test_when_no_pod_can_be_read_the_task_fails_with_the_proxy_error_never_empty(answer, code):
    env, errors, _ = run(stubs_for("ok", {metrics(POD["ok"]): answer}), resource("otel-ok"))
    assert env["status"] == "failed" and errors[0]["code"] == code
    assert env["outputs"] == {}


def test_when_every_pod_failed_the_error_carries_the_same_hint_as_the_partial_path():
    from k8s_otelcollector_health.exporter_queue import METRICS_HINT

    # all pods failed: the task fails with RwError(code, message, hint=...), scope naming the pod
    env, errors, _ = run(stubs_for("ok", {metrics(POD["ok"]): REFUSED}), resource("otel-ok"))
    assert env["status"] == "failed" and env["outputs"] == {}
    assert [(e["code"], e["scope"], e["hint"]) for e in errors] == [
        ("unreachable", f"pod/{POD['ok']}", METRICS_HINT)
    ]
    assert "metricsPort" in errors[0]["hint"] and POD["ok"] in errors[0]["message"]
    # ... and the partial path gives the very same hint
    pods, second = two_pods("ok")
    env, errors, _ = run(stubs_for("ok", {get_pods("ok"): ok(pods), metrics(second): REFUSED}))
    assert env["status"] == "partial" and errors[0]["hint"] == METRICS_HINT


def test_a_collector_with_no_exporter_queue_reports_the_fact_not_an_empty_list_alone():
    page = 'otelcol_process_uptime{service_instance_id="x"} 12\notelcol_receiver_refused_spans{receiver="otlp"} 0\n'
    env, errors, _ = run(stubs_for("ok", {metrics(POD["ok"]): ok(page)}), resource("otel-ok"))
    assert env["status"] == "ok" and errors == []
    check_outputs(env)
    q = env["outputs"]["queues"]
    assert q["items"] == [] and q["count"] == 0 and q["noQueuedExporters"] is True
    env, _, _ = run(stubs_for("ok"), resource("otel-ok"))
    assert env["outputs"]["queues"]["noQueuedExporters"] is False


def test_pods_that_are_not_running_are_listed_and_not_asked_and_all_pending_is_unreachable():
    pods = copy.deepcopy(PODS["ok"])
    pods["items"][0]["status"]["phase"] = "Pending"
    pods["items"][0]["status"].pop("podIP")
    env, errors, calls = run(stubs_for("ok", {get_pods("ok"): ok(pods)}), resource("otel-ok"))
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "unreachable"
        and "Pending" in errors[0]["message"]
    )
    assert metrics(POD["ok"]) not in calls
    # one Running and one Pending: the Pending one is listed with its reason, the result stays ok
    more, second = two_pods("ok")
    more["items"][1]["status"]["phase"] = "Pending"
    env, errors, calls = run(stubs_for("ok", {get_pods("ok"): ok(more)}), resource("otel-ok"))
    assert env["status"] == "ok" and errors == []
    entry = {p["pod"]: p for p in env["outputs"]["pods"]["items"]}[second]
    assert entry["queried"] is False and "Pending" in entry["notQueriedReason"]


def test_a_workload_without_pods_is_unreachable_not_an_empty_result():
    env, errors, _ = run(stubs_for("ok", {get_pods("ok"): ok({"items": []})}), resource("otel-ok"))
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "unreachable"
        and "no pods" in errors[0]["message"]
    )


def test_missing_workload_and_forbidden_reads_and_unreachable_api():
    err = 'Error from server (NotFound): deployments.apps "otel-ok" not found'
    env, errors, calls = run({get_workload("ok"): (1, "", err)}, resource("otel-ok"))
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "not_found"
        and calls == [get_workload("ok")]
    )
    forbidden = 'Error from server (Forbidden): deployments.apps "otel-ok" is forbidden: User "u" cannot get resource "deployments" in API group "apps" in the namespace "rw-otel"'
    env, errors, _ = run({get_workload("ok"): (1, "", forbidden)}, resource("otel-ok"))
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"
    stubs = {
        get_workload("ok"): ok(DEPLOY["ok"]),
        get_pods("ok"): (
            1,
            "",
            'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods" in API group "" in the namespace "rw-otel"',
        ),
    }
    env, errors, _ = run(stubs, resource("otel-ok"))
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"
    down = (
        1,
        "",
        "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
    )
    env, errors, _ = run({get_workload("ok"): down}, resource("otel-ok"))
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


def test_wrong_target_type_is_refused_before_any_kubectl_call():
    res = {
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/cronjobs/x",
        "cluster": "kind",
        "namespace": NS,
        "name": "x",
        "type": "kubernetes/cronjob",
    }
    env, errors, calls = run({}, res)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_a_workload_with_an_empty_selector_is_refused_not_every_pod_of_the_namespace():
    doc = copy.deepcopy(DEPLOY["ok"])
    doc["spec"]["selector"] = {}
    env, errors, calls = run({get_workload("ok"): ok(doc)}, resource("otel-ok"))
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "invalid_input"
        and calls == [get_workload("ok")]
    )


def test_selector_expressions_become_a_label_selector():
    sel = {"matchLabels": {"a": "b"}, "matchExpressions": [
        {"key": "t", "operator": "In", "values": ["x", "y"]}, {"key": "u", "operator": "Exists"}, {"key": "v", "operator": "DoesNotExist"},
        {"key": "w", "operator": "NotIn", "values": ["z"]}]}  # fmt: skip
    assert exporter_queue.selector_string(sel) == "a=b,t in (x,y),u,!v,w notin (z)"
    with pytest.raises(RwError):
        exporter_queue.selector_string(
            {"matchExpressions": [{"key": "k", "operator": "Gt", "values": ["1"]}]}
        )


def test_pods_beyond_max_pods_are_counted_but_not_asked_and_lists_are_capped():
    pods = copy.deepcopy(PODS["broken"])
    extras = []
    for i in range(1, 4):
        extra = copy.deepcopy(pods["items"][0])
        extra["metadata"]["name"] = f"otel-broken-x{i}"
        extra["status"]["podIP"] = f"10.244.8.{i}"
        pods["items"].append(extra)
        extras.append(extra["metadata"]["name"])
    stubs = stubs_for(
        "broken", {get_pods("broken"): ok(pods), **{metrics(n): ok(PAGE["broken"]) for n in extras}}
    )
    env, _, calls = run(stubs, resource("otel-broken"), {"maxPods": 2, "maxItems": 5})
    p = env["outputs"]["pods"]
    assert (p["count"], p["queriedCount"], p["truncated"]) == (4, 2, True)
    assert metrics(extras[-1]) not in calls
    q, c = env["outputs"]["queues"], env["outputs"]["counters"]
    assert (q["count"], len(q["items"]), q["truncated"]) == (12, 5, True)
    assert (c["count"], len(c["items"]), c["truncated"]) == (18, 5, True)


def test_proxy_error_classification_reaches_the_kubectl_json_path_too():
    class Done:
        returncode, stdout, stderr = (
            1,
            "",
            "Error from server (Forbidden): x is forbidden: cannot get resource",
        )

    class Ctx:
        def run(self, argv, **kw):
            return Done()

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "permission_denied"


# ------------------------------------------------------------- RBAC / argv ----

RESOURCES = {
    "pods": ("", "pods"),
    "deployments": ("apps", "deployments"),
    "daemonsets": ("apps", "daemonsets"),
    "statefulsets": ("apps", "statefulsets"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-l"}


def rbac_needs(argv):
    argv = list(argv)
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    if "--raw" in argv:
        assert positional[0] == "get" and "/pods/" in positional[1] and "/proxy/" in positional[1]
        return {("", "pods/proxy", "get")}
    assert positional[0] == "get", argv
    group, plural = RESOURCES[positional[1]]
    return {(group, plural, "get" if len(positional) > 2 and "-l" not in flags else "list")}


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


@pytest.mark.parametrize(
    "plural,rtype",
    [
        ("deployments", "kubernetes/deployment"),
        ("daemonsets", "kubernetes/daemonset"),
        ("statefulsets", "kubernetes/statefulset"),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_for_each_workload_kind(plural, rtype):
    stubs = stubs_for(
        "ok", {("kubectl", "-n", NS, "get", plural, "otel-ok", "-o", "json"): ok(DEPLOY["ok"])}
    )
    env, errors, calls = run(stubs, resource("otel-ok", plural, rtype))
    assert env["status"] == "ok", errors
    needed = set()
    for argv in calls:
        needed |= rbac_needs(argv)
    assert needed <= declared(TASK), sorted(needed - declared(TASK))
    assert {r for _, r, _ in needed} == {plural, "pods", "pods/proxy"}


def test_the_task_is_read_only_and_declares_only_get_and_list():
    assert TASK["readOnly"] is True
    assert {v for _, _, v in declared(TASK)} == {"get", "list"}
    assert {r for _, r, _ in declared(TASK)} == {
        "deployments",
        "daemonsets",
        "statefulsets",
        "pods",
        "pods/proxy",
    }


def test_every_call_is_a_read():
    _, _, calls = run(stubs_for("broken"), resource("otel-broken"))
    assert not [
        c for c in calls if {"replace", "delete", "create", "patch", "apply", "exec"} & set(c)
    ]


def test_no_output_carries_env_or_secret_content():
    env, _, _ = run(stubs_for("broken"), resource("otel-broken"))
    dump = json.dumps(env["outputs"]).lower()
    for needle in ("secret", "password", "token", '"env"', "authorization"):
        assert needle not in dump
