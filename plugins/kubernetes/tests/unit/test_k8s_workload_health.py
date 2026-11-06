"""Unit tests for k8s-workload-health's lib/workload.py against JSON captured
from the kind-rw-plugins scenarios (plugins/kubernetes/tests/kind). The cluster
state was captured at one moment; `NOW` is a fixed point just after it."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from k8s_workload_health import workload as w
from k8s_workload_health.k8s import RwError

DATA = Path(__file__).parent / "data" / "k8s_workload_health"
NOW = datetime(2026, 11, 3, 18, 10, tzinfo=UTC)


def load(ns, resource):
    return json.loads((DATA / f"{ns}_{resource}.json").read_text())["items"]


def named(items, name):
    return next(i for i in items if i["metadata"]["name"] == name)


def owned(ns, kind, name):
    """(workload, revisions, pods) for a captured workload."""
    plural = {
        "Deployment": "deployments",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
    }[kind]
    rev = {"Deployment": "replicasets"}.get(kind, "controllerrevisions")
    workload = named(load(ns, plural), name)
    revisions = w.owned_revisions(kind, workload, load(ns, rev))
    return workload, revisions, w.owned_pods(kind, workload, revisions, load(ns, "pods"))


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert w.parse_duration("90s").total_seconds() == 90
    assert w.parse_duration("1h30m").total_seconds() == 5400
    assert w.parse_duration("2d").total_seconds() == 172800
    assert w.parse_duration("500ms").total_seconds() == 0.5
    assert w.parse_duration("1m500ms").total_seconds() == 60.5
    for bad in ("", "soon", "10", "5x", "1h junk", "1w", "-5m", " 5m"):
        with pytest.raises(RwError) as e:
            w.parse_duration(bad)
        assert e.value.code == "invalid_input"


def test_selector_string_covers_labels_and_expressions():
    sel = {
        "matchLabels": {"app": "web"},
        "matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["a", "b"]},
            {"key": "x", "operator": "NotIn", "values": ["y"]},
            {"key": "has", "operator": "Exists"},
            {"key": "lacks", "operator": "DoesNotExist"},
        ],
    }
    assert w.selector_string(sel) == "app=web,tier in (a,b),x notin (y),has,!lacks"


def test_empty_selector_is_refused_rather_than_selecting_every_pod():
    with pytest.raises(RwError) as e:
        w.selector_string({})
    assert e.value.code == "invalid_input"


def test_cap_bounds_a_list():
    assert w.cap(range(10), 3) == [0, 1, 2]
    assert w.cap([1, 2], 200) == [1, 2]


# ---------------------------------------------------------- ownership ----


def test_deployment_owns_its_pods_through_replicasets():
    _, revisions, pods = owned("healthy", "Deployment", "web")
    assert len(revisions) == 1
    assert sorted(p["metadata"]["name"] for p in pods) == sorted(
        p["metadata"]["name"]
        for p in load("healthy", "pods")
        if p["metadata"]["name"].startswith("web-")
    )
    assert len(pods) == 2


def test_owned_pods_excludes_a_pod_with_matching_labels_but_a_foreign_owner():
    deploy, revisions, pods = owned("healthy", "Deployment", "web")
    stranger = copy.deepcopy(pods[0])
    stranger["metadata"]["name"] = "web-impostor"
    stranger["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "other", "uid": "not-ours", "controller": True}
    ]
    ownerless = copy.deepcopy(pods[0])
    ownerless["metadata"]["name"] = "web-ownerless"
    ownerless["metadata"].pop("ownerReferences")
    found = w.owned_pods(
        "Deployment", deploy, revisions, load("healthy", "pods") + [stranger, ownerless]
    )
    names = {p["metadata"]["name"] for p in found}
    assert "web-impostor" not in names and "web-ownerless" not in names
    assert len(names) == 2


def test_owned_pods_excludes_a_pod_that_is_owned_but_does_not_match_the_selector():
    deploy, revisions, pods = owned("healthy", "Deployment", "web")
    relabeled = copy.deepcopy(pods[0])
    relabeled["metadata"]["labels"] = {"app": "something-else"}
    assert w.owned_pods("Deployment", deploy, revisions, [relabeled]) == []


def test_statefulset_and_daemonset_own_their_pods_directly():
    _, _, sts_pods = owned("healthy", "StatefulSet", "db")
    _, _, ds_pods = owned("healthy", "DaemonSet", "agent")
    assert [p["metadata"]["name"] for p in sts_pods] == ["db-0"]
    assert ds_pods[0]["metadata"]["name"].startswith("agent-")


# ------------------------------------------------------ rollout status ----


def test_stuck_rollout_shows_progress_deadline_exceeded_and_updated_below_desired():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    r = w.rollout_status("Deployment", deploy)
    progressing = next(c for c in r["conditions"] if c["type"] == "Progressing")
    assert progressing["status"] == "False" and progressing["reason"] == "ProgressDeadlineExceeded"
    assert r["progressDeadlineExceeded"] is True
    assert r["updated"] < r["desired"] + 1 and r["updated"] == 1 and r["desired"] == 1
    assert r["unavailable"] == 1
    # the deployment has two ReplicaSets, one per revision, and the newest runs the bad image
    history = w.revision_history("Deployment", revisions)
    assert [h["revision"] for h in history] == [2, 1]
    assert history[0]["images"] == {"app": "nginx:does-not-exist-0.0.0"}
    assert history[1]["images"] == {"app": "nginx:alpine"}


def test_healthy_deployment_rollout_is_fully_ready_and_progressing():
    deploy, _, _ = owned("healthy", "Deployment", "web")
    r = w.rollout_status("Deployment", deploy)
    assert r["ready"] == r["desired"] == r["updated"] == r["available"] == 2
    assert r["unavailable"] == 0 and r["generationObserved"] is True
    assert r["progressDeadlineExceeded"] is False and r["paused"] is False


def test_statefulset_and_daemonset_rollout_counts():
    sts = w.rollout_status("StatefulSet", owned("healthy", "StatefulSet", "db")[0])
    assert (sts["desired"], sts["ready"], sts["unavailable"]) == (1, 1, 0)
    assert sts["currentRevision"] and sts["updateRevision"]
    ds = w.rollout_status("DaemonSet", owned("healthy", "DaemonSet", "agent")[0])
    assert ds["desired"] == ds["ready"] == ds["updated"] == 1
    assert ds["misscheduled"] == 0 and ds["updateStrategy"] == "RollingUpdate"


def test_broken_statefulset_is_not_ready():
    r = w.rollout_status("StatefulSet", owned("broken", "StatefulSet", "broken-sts")[0])
    assert r["ready"] == 0 and r["desired"] == 1 and r["unavailable"] == 1


def test_unobserved_generation_is_reported():
    deploy = copy.deepcopy(owned("healthy", "Deployment", "web")[0])
    deploy["metadata"]["generation"] = 9
    assert w.rollout_status("Deployment", deploy)["generationObserved"] is False


# ------------------------------------------------------------ restarts ----


def test_crashloop_pod_shows_restarts_with_last_termination():
    _, _, pods = owned("broken", "Deployment", "crashloop")
    rows = w.pod_restarts(pods, "1h", NOW, 1)
    assert len(rows) == 1
    r = rows[0]
    assert r["restartCount"] > 0 and r["container"] == "app" and r["init"] is False
    assert r["lastTerminated"]["exitCode"] == 1 and r["lastTerminated"]["reason"] == "Error"
    assert r["waitingReason"] == "CrashLoopBackOff" and r["ready"] is False
    assert r["recent"] is True and r["exceedsThreshold"] is True


def test_oom_pod_restart_reason_is_oomkilled():
    _, _, pods = owned("broken", "Deployment", "oom")
    (r,) = w.pod_restarts(pods, "1h", NOW, 1)
    assert r["restartCount"] > 0
    assert r["lastTerminated"]["reason"] == "OOMKilled" and r["lastTerminated"]["exitCode"] == 137


def test_restart_recent_flag_follows_the_window_and_threshold_follows_the_setting():
    _, _, pods = owned("broken", "Deployment", "crashloop")
    (r,) = w.pod_restarts(pods, "1m", NOW, 10_000)
    assert r["recent"] is False and r["exceedsThreshold"] is False
    (r,) = w.pod_restarts(pods, "1m", datetime(2026, 11, 3, 18, 5, 10, tzinfo=UTC), 1)
    assert r["recent"] is True


@pytest.mark.parametrize(
    "ns,kind,name",
    [
        ("healthy", "Deployment", "web"),
        ("healthy", "StatefulSet", "db"),
        ("healthy", "DaemonSet", "agent"),
    ],
)
def test_healthy_workloads_have_no_restarts(ns, kind, name):
    _, _, pods = owned(ns, kind, name)
    assert pods, "the fixture must contain pods, or an empty result would mean nothing"
    assert w.pod_restarts(pods, "1h", NOW, 1) == []


def test_restarts_are_sorted_most_restarted_first():
    pods = owned("broken", "Deployment", "crashloop")[2] + owned("broken", "Deployment", "oom")[2]
    pods[0] = copy.deepcopy(pods[0])
    pods[0]["status"]["containerStatuses"][0]["restartCount"] = 1
    rows = w.pod_restarts(pods, "1h", NOW, 1)
    assert [r["restartCount"] for r in rows] == sorted(
        (r["restartCount"] for r in rows), reverse=True
    )


# -------------------------------------------------------------- status ----


def test_bad_image_pod_status_shows_image_pull_waiting_reason():
    _, _, pods = owned("broken", "Deployment", "bad-image")
    (p,) = w.pod_status(pods)
    assert p["phase"] == "Pending" and p["ready"] is False
    assert p["waiting"][0]["reason"] in ("ImagePullBackOff", "ErrImagePull")
    assert p["containersReady"] == 0 and p["containersTotal"] == 1


def test_crashloop_pod_status_waiting_reason():
    _, _, pods = owned("broken", "Deployment", "crashloop")
    (p,) = w.pod_status(pods)
    assert p["waiting"][0]["reason"] == "CrashLoopBackOff"


def test_pod_status_reports_why_a_pod_is_unscheduled():
    pod = copy.deepcopy(owned("broken", "Deployment", "crashloop")[2][0])
    pod["status"] = {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/1 nodes are available",
            }
        ],
    }
    pod["spec"].pop("nodeName", None)
    (p,) = w.pod_status([pod])
    assert p["scheduled"] is False and p["unschedulable"]["reason"] == "Unschedulable"
    assert p["node"] is None


def test_healthy_pods_are_all_ready_and_counted():
    _, _, pods = owned("healthy", "Deployment", "web")
    rows = w.pod_status(pods)
    assert all(p["ready"] and p["waiting"] == [] and p["unschedulable"] is None for p in rows)
    assert w.pod_counts(pods) == {"total": 2, "ready": 2, "notReady": 0, "byPhase": {"Running": 2}}


def test_not_ready_pods_sort_first():
    pods = owned("healthy", "Deployment", "web")[2] + owned("broken", "Deployment", "bad-probe")[2]
    assert w.pod_status(pods)[0]["ready"] is False


# -------------------------------------------------------------- events ----


def ev(kind, name, reason, last, **extra):
    base = {
        "type": "Warning",
        "reason": reason,
        "message": f"{reason} on {name}",
        "involvedObject": {"kind": kind, "name": name},
        "metadata": {"creationTimestamp": last},
        "lastTimestamp": last,
        "count": 1,
    }
    base.update(extra)
    return base


def test_warning_events_window_filters_on_last_seen_with_a_fixed_now():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    names = {"Pod": {"p1"}}
    events = [
        ev("Pod", "p1", "BackOff", "2026-11-03T17:50:00Z"),
        ev("Pod", "p1", "Failed", "2026-11-03T17:00:00Z"),
    ]
    assert [e["reason"] for e in w.warning_events(events, names, "30m", now, 200)] == ["BackOff"]
    assert {e["reason"] for e in w.warning_events(events, names, "2h", now, 200)} == {
        "BackOff",
        "Failed",
    }
    assert w.warning_events(events, names, "1m", now, 200) == []


def test_warning_events_ignore_normal_events_and_unrelated_objects():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    events = [
        ev("Pod", "p1", "Pulled", "2026-11-03T17:59:00Z", type="Normal"),
        ev("Pod", "other", "BackOff", "2026-11-03T17:59:00Z"),
        ev("Node", "p1", "BackOff", "2026-11-03T17:59:00Z"),
    ]
    assert w.warning_events(events, {"Pod": {"p1"}}, "1h", now, 200) == []


def test_warning_events_group_by_object_and_reason():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    events = [
        ev(
            "Pod",
            "p1",
            "BackOff",
            "2026-11-03T17:50:00Z",
            count=3,
            firstTimestamp="2026-11-03T17:40:00Z",
        ),
        ev(
            "Pod",
            "p1",
            "BackOff",
            "2026-11-03T17:55:00Z",
            count=2,
            firstTimestamp="2026-11-03T17:30:00Z",
            message="newer",
        ),
        ev("Pod", "p2", "BackOff", "2026-11-03T17:51:00Z"),
    ]
    rows = w.warning_events(events, {"Pod": {"p1", "p2"}}, "1h", now, 200)
    assert [r["name"] for r in rows] == ["p1", "p2"]  # newest lastSeen first
    p1 = rows[0]
    assert p1["count"] == 5 and p1["firstSeen"] == "2026-11-03T17:30:00Z"
    assert p1["lastSeen"] == "2026-11-03T17:55:00Z" and p1["message"] == "newer"


def test_warning_events_use_series_and_event_time_when_last_timestamp_is_absent():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    series = ev(
        "Pod",
        "p1",
        "Unhealthy",
        "2026-11-03T10:00:00Z",
        series={"count": 7, "lastObservedTime": "2026-11-03T17:58:00Z"},
    )
    series["lastTimestamp"] = None
    series["eventTime"] = "2026-11-03T10:00:00.000000Z"
    only_event_time = ev(
        "Pod", "p2", "Failed", "2026-11-03T10:00:00Z", eventTime="2026-11-03T17:45:00.000000Z"
    )
    only_event_time["lastTimestamp"] = None
    only_event_time.pop("count")
    rows = {
        r["name"]: r
        for r in w.warning_events([series, only_event_time], {"Pod": {"p1", "p2"}}, "30m", now, 200)
    }
    assert rows["p1"]["count"] == 7 and rows["p1"]["lastSeen"] == "2026-11-03T17:58:00Z"
    assert rows["p2"]["count"] == 1 and rows["p2"]["lastSeen"] == "2026-11-03T17:45:00Z"


def test_warning_events_max_items_caps_and_keeps_the_newest():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    events = [ev("Pod", f"p{i}", "BackOff", f"2026-11-03T17:{10 + i}:00Z") for i in range(10)]
    names = {"Pod": {f"p{i}" for i in range(10)}}
    rows = w.warning_events(events, names, "2h", now, 3)
    assert [r["name"] for r in rows] == ["p9", "p8", "p7"]


def test_crashloop_warning_events_from_the_cluster():
    deploy, revisions, pods = owned("broken", "Deployment", "crashloop")
    names = w.related_names("Deployment", deploy, revisions, pods)
    assert names["ReplicaSet"] and names["Pod"] == {p["metadata"]["name"] for p in pods}
    rows = w.warning_events(load("broken", "events"), names, "1h", NOW, 200)
    assert {(r["kind"], r["reason"]) for r in rows} >= {("Pod", "BackOff")}
    assert all(r["name"] in names[r["kind"]] for r in rows)
    # nothing about the neighbouring bad-image deployment leaks in
    assert not any("bad-image" in r["name"] for r in rows)


def test_healthy_workload_only_has_the_probe_blips_the_cluster_recorded():
    deploy, revisions, pods = owned("healthy", "Deployment", "web")
    names = w.related_names("Deployment", deploy, revisions, pods)
    # the kind node stalled for a moment at 17:58; those probe failures are real recorded events
    rows = w.warning_events(load("healthy", "events"), names, "1h", NOW, 200)
    assert {r["reason"] for r in rows} == {"Unhealthy"} and {r["kind"] for r in rows} == {"Pod"}
    assert w.warning_events(load("healthy", "events"), names, "10m", NOW, 200) == []


# -------------------------------------------------------------- probes ----


def probes(ns, kind, name):
    workload, _, _ = owned(ns, kind, name)
    return w.probe_config(workload["spec"]["template"])


def test_bad_probe_readiness_port_is_not_declared_by_the_container():
    rows = probes("broken", "Deployment", "bad-probe")
    readiness = next(r for r in rows if r["probe"] == "readiness")
    assert readiness["present"] and readiness["handler"] == "httpGet"
    assert (
        readiness["port"] == 9999
        and readiness["portDeclared"] is False
        and readiness["resolvedPort"] == 9999
    )
    assert readiness["periodSeconds"] == 5 and readiness["failureThreshold"] == 1
    assert next(r for r in rows if r["probe"] == "liveness") == {
        "container": "app",
        "init": False,
        "probe": "liveness",
        "present": False,
    }
    # and the pod really is Running but not Ready
    (p,) = w.pod_status(owned("broken", "Deployment", "bad-probe")[2])
    assert p["phase"] == "Running" and p["ready"] is False


def test_probe_ports_are_matched_by_number_and_by_name():
    template = {
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "readinessProbe": {"httpGet": {"path": "/r", "port": "http"}},
                    "livenessProbe": {"httpGet": {"path": "/l", "port": 80}},  # 80 is not 8080
                    "startupProbe": {"tcpSocket": {"port": "metrics"}},
                }
            ]
        }
    }
    rows = {r["probe"]: r for r in w.probe_config(template)}
    assert rows["readiness"]["portDeclared"] is True and rows["readiness"]["resolvedPort"] == 8080
    assert rows["liveness"]["portDeclared"] is False
    assert rows["startup"]["portDeclared"] is False and rows["startup"]["resolvedPort"] is None


def test_exec_probe_has_program_and_arg_count_but_never_the_arguments():
    argv = ["mysql", "-h", "db", "--password=hunter2"]
    template = {
        "spec": {"containers": [{"name": "c", "livenessProbe": {"exec": {"command": argv}}}]}
    }
    r = next(r for r in w.probe_config(template) if r["probe"] == "liveness")
    assert r["handler"] == "exec" and r["command"] == "mysql" and r["argCount"] == 3
    assert "hunter2" not in json.dumps(w.probe_config(template))
    assert r["port"] is None and r["portDeclared"] is None


def test_healthy_web_deployment_probes_point_at_its_declared_port():
    rows = {r["probe"]: r for r in probes("healthy", "Deployment", "web")}
    assert [rows[p]["present"] for p in ("liveness", "readiness", "startup")] == [True, True, False]
    assert rows["readiness"]["portDeclared"] is True and rows["readiness"]["periodSeconds"] == 5


# ------------------------------------------------------ config changes ----


def test_config_changes_on_stuck_rollout_shows_the_image_change():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    rows = w.config_changes("Deployment", deploy, revisions, "1h", NOW)
    assert [r["revision"] for r in rows] == [2, 1]
    newest = rows[0]
    assert newest["previousRevision"] == 1
    assert newest["imageChanges"] == [
        {"container": "app", "from": "nginx:alpine", "to": "nginx:does-not-exist-0.0.0"}
    ]
    assert newest["envAdded"] == [] and newest["resourceChanges"] == []
    assert rows[1]["previousRevision"] is None and rows[1]["imageChanges"] == []


def test_config_changes_respects_the_window():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    assert (
        w.config_changes("Deployment", deploy, revisions, "1h", datetime(2026, 11, 4, tzinfo=UTC))
        == []
    )
    only_new = w.config_changes(
        "Deployment",
        deploy,
        revisions,
        "10s",
        datetime(2026, 11, 3, 17, 33, 30, tzinfo=UTC),
    )
    assert [r["revision"] for r in only_new] == [2]


def test_config_changes_report_env_names_and_resources_but_not_values():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    old = next(
        r
        for r in revisions
        if r["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "1"
    )
    new = copy.deepcopy(old)
    new["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
    new["metadata"]["name"] = "stuck-rollout-next"
    new["metadata"]["creationTimestamp"] = "2026-11-03T18:00:00Z"
    c = new["spec"]["template"]["spec"]["containers"][0]
    c["env"] = [{"name": "FEATURE_X", "value": "super-secret-value"}]
    c["resources"] = {"requests": {"cpu": "100m"}, "limits": {"memory": "64Mi"}}
    rows = w.config_changes("Deployment", deploy, [old, new], "1h", NOW)
    assert rows[0]["revision"] == 3 and rows[0]["previousRevision"] == 1
    assert rows[0]["envAdded"] == [{"container": "app", "name": "FEATURE_X"}]
    assert rows[0]["resourceChanges"][0]["to"]["limits"] == {"memory": "64Mi"}
    assert "super-secret-value" not in json.dumps(rows)
    # and a later revision without it reports the name as removed
    new2 = copy.deepcopy(old)
    new2["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "4"
    new2["metadata"]["name"] = "stuck-rollout-back"
    new2["metadata"]["creationTimestamp"] = "2026-11-03T18:05:00Z"
    rows = w.config_changes("Deployment", deploy, [old, new, new2], "1h", NOW)
    assert rows[0]["revision"] == 4 and rows[0]["envRemoved"] == [
        {"container": "app", "name": "FEATURE_X"}
    ]


def test_config_changes_on_a_controller_revision_workload():
    workload, revisions, _ = owned("healthy", "DaemonSet", "agent")
    (row,) = w.config_changes("DaemonSet", workload, revisions, "1h", NOW)
    assert row["revision"] == 1 and row["images"] == {"pause": "registry.k8s.io/pause:3.9"}


# ----------------------------------------------------------------- HPA ----


def hpa(target_kind="Deployment", target="web", metrics=None, status=None):
    return {
        "metadata": {"name": f"{target}-hpa"},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": target_kind, "name": target},
            "minReplicas": 2,
            "maxReplicas": 5,
            "metrics": metrics
            or [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {"type": "Utilization", "averageUtilization": 70},
                    },
                }
            ],
        },
        "status": status
        or {
            "currentReplicas": 5,
            "desiredReplicas": 5,
            "conditions": [
                {
                    "type": "ScalingLimited",
                    "status": "True",
                    "reason": "TooManyReplicas",
                    "message": "at max",
                }
            ],
            "currentMetrics": [
                {
                    "type": "Resource",
                    "resource": {"name": "cpu", "current": {"averageUtilization": 91}},
                }
            ],
        },
    }


def test_hpa_status_selects_only_hpas_targeting_the_workload():
    template = owned("healthy", "Deployment", "web")[0]["spec"]["template"]
    hpas = [hpa(), hpa(target="other"), hpa(target_kind="StatefulSet", target="web")]
    rows = w.hpa_status(hpas, "Deployment", "web", template)
    assert [r["name"] for r in rows] == ["web-hpa"]
    r = rows[0]
    assert (r["minReplicas"], r["maxReplicas"], r["currentReplicas"], r["desiredReplicas"]) == (
        2,
        5,
        5,
        5,
    )
    assert r["atMax"] is True and r["atMin"] is False
    assert r["conditions"][0]["reason"] == "TooManyReplicas"
    assert r["metrics"][0] | {"containersWithoutRequests": None} == {
        "type": "Resource",
        "name": "cpu",
        "container": None,
        "targetType": "Utilization",
        "target": 70,
        "current": 91,
        "containersWithoutRequests": None,
    }
    assert r["defaultCpuTarget"] is None


def test_hpa_flags_utilization_metric_on_a_container_without_requests():
    no_requests = {
        "spec": {
            "containers": [
                {"name": "app", "resources": {}},
                {"name": "side", "resources": {"requests": {"cpu": "1m"}}},
            ]
        }
    }
    with_requests = {
        "spec": {"containers": [{"name": "app", "resources": {"requests": {"cpu": "1m"}}}]}
    }
    r = w.hpa_status([hpa()], "Deployment", "web", no_requests)[0]
    assert r["resourceMetricWithoutRequests"] is True and r["metrics"][0][
        "containersWithoutRequests"
    ] == ["app"]
    assert (
        w.hpa_status([hpa()], "Deployment", "web", with_requests)[0][
            "resourceMetricWithoutRequests"
        ]
        is False
    )


def test_hpa_average_value_metric_needs_no_requests():
    metrics = [
        {
            "type": "Resource",
            "resource": {
                "name": "memory",
                "target": {"type": "AverageValue", "averageValue": "100Mi"},
            },
        }
    ]
    template = {"spec": {"containers": [{"name": "app"}]}}
    r = w.hpa_status([hpa(metrics=metrics)], "Deployment", "web", template)[0]
    assert r["resourceMetricWithoutRequests"] is False and r["metrics"][0]["target"] == "100Mi"


def test_no_hpa_targeting_the_workload_is_an_empty_list():
    template = owned("healthy", "Deployment", "web")[0]["spec"]["template"]
    assert w.hpa_status([hpa(target="other")], "Deployment", "web", template) == []
    assert w.hpa_status([], "Deployment", "web", template) == []


def test_hpa_without_status_yet_does_not_claim_at_min_or_max():
    template = {"spec": {"containers": [{"name": "app"}]}}
    r = w.hpa_status([hpa(status={"conditions": []})], "Deployment", "web", template)[0]
    assert r["desiredReplicas"] is None and r["atMax"] is None and r["atMin"] is None


# ================================================== review fixes ========


def pod_with(**status):
    pod = copy.deepcopy(owned("healthy", "Deployment", "web")[2][0])
    pod["status"] = {"phase": "Pending", **status}
    return pod


def test_pod_status_includes_init_containers_and_counts_their_readiness_apart():
    pod = pod_with(
        conditions=[{"type": "Ready", "status": "False"}],
        initContainerStatuses=[
            {
                "name": "migrate",
                "ready": True,
                "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
            },
            {
                "name": "wait-db",
                "ready": False,
                "state": {"waiting": {"reason": "ImagePullBackOff", "message": "m"}},
            },
        ],
        containerStatuses=[
            {"name": "app", "ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}
        ],
    )
    (p,) = w.pod_status([pod])
    assert (p["initContainersReady"], p["initContainersTotal"]) == (1, 2)
    assert (p["containersReady"], p["containersTotal"]) == (0, 1)
    assert [(x["container"], x["reason"], x["init"]) for x in p["waiting"]] == [
        ("wait-db", "ImagePullBackOff", True),
        ("app", "PodInitializing", False),
    ]
    assert p["terminated"] == [
        {"container": "migrate", "reason": "Completed", "exitCode": 0, "init": True}
    ]


def test_free_text_is_cut_at_1024_chars_and_flagged():
    long = "x" * 5000
    pod = pod_with(
        message=long,
        containerStatuses=[
            {"name": "app", "ready": False, "state": {"waiting": {"reason": "R", "message": long}}}
        ],
    )
    (p,) = w.pod_status([pod])
    assert len(p["message"]) == 1024 and len(p["waiting"][0]["message"]) == 1024
    assert p["truncated"] is True and p["waiting"][0]["truncated"] is True
    (q,) = w.pod_status(owned("healthy", "Deployment", "web")[2][:1])
    assert "truncated" not in q

    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    e = ev("Pod", "p1", "BackOff", "2026-11-03T17:59:00Z", message=long)
    (row,) = w.warning_events([e], {"Pod": {"p1"}}, "1h", now, 5)
    assert len(row["message"]) == 1024 and row["truncated"] is True

    deploy = copy.deepcopy(owned("healthy", "Deployment", "web")[0])
    deploy["status"]["conditions"][0]["message"] = long
    cond = w.rollout_status("Deployment", deploy)["conditions"][0]
    assert len(cond["message"]) == 1024 and cond["truncated"] is True


def test_revision_history_marks_the_current_and_reused_revisions():
    _, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    history = w.revision_history("Deployment", revisions)
    assert [(h["revision"], h["current"], h["reused"]) for h in history] == [
        (2, True, False),
        (1, False, False),
    ]
    reused = copy.deepcopy(revisions[0])
    reused["metadata"]["annotations"]["deployment.kubernetes.io/revision-history"] = "1,3"
    (h,) = w.revision_history("Deployment", [reused])
    assert h["reused"] is True and h["earlierRevisions"] == [1, 3]


def rs_pair():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    old = next(
        r
        for r in revisions
        if r["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "1"
    )
    return deploy, old


def rev(old, number, name, created, **template_edit):
    new = copy.deepcopy(old)
    new["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = str(number)
    new["metadata"]["name"] = name
    new["metadata"]["creationTimestamp"] = created
    tpl = new["spec"]["template"]
    c = tpl["spec"]["containers"][0]
    for k, v in template_edit.items():
        if k == "annotations":
            tpl["metadata"]["annotations"] = v
        elif k in ("env", "command", "image"):
            c[k] = v
        else:
            tpl["spec"][k] = v
    return new


def test_config_changes_marks_current_revision():
    deploy, revisions, _ = owned("broken", "Deployment", "stuck-rollout")
    rows = w.config_changes("Deployment", deploy, revisions, "1h", NOW)
    assert [(r["revision"], r["current"]) for r in rows] == [(2, True), (1, False)]


def test_a_rolled_back_current_revision_is_included_by_the_progressing_condition_time():
    deploy, old = rs_pair()
    mid = rev(old, 2, "rs-mid", "2026-11-03T17:50:00Z", image="nginx:1.0")
    back = copy.deepcopy(old)  # revision 1 re-used as revision 3: its creationTimestamp stays old
    back["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
    back["metadata"]["annotations"]["deployment.kubernetes.io/revision-history"] = "1"
    deploy = copy.deepcopy(deploy)
    deploy["status"]["conditions"] = [
        {
            "type": "Progressing",
            "status": "True",
            "reason": "NewReplicaSetAvailable",
            "lastUpdateTime": "2026-11-03T18:05:00Z",
        }
    ]
    rows = w.config_changes(
        "Deployment",
        deploy,
        [old, mid, back],
        "10m",
        datetime(2026, 11, 3, 18, 10, tzinfo=UTC),
    )
    assert [r["revision"] for r in rows] == [3]
    r = rows[0]
    assert r["current"] and r["reused"] and r["windowBasis"] == "progressing"
    assert r["previousRevision"] == 2
    assert r["imageChanges"] == [{"container": "app", "from": "nginx:1.0", "to": "nginx:alpine"}]
    # outside the window it is not reported
    assert (
        w.config_changes(
            "Deployment",
            deploy,
            [old, mid, back],
            "1m",
            datetime(2026, 11, 3, 18, 10, tzinfo=UTC),
        )
        == []
    )


def test_config_changes_report_changed_env_values_by_name_only():
    deploy, old = rs_pair()
    a = rev(
        old,
        2,
        "a",
        "2026-11-03T18:00:00Z",
        env=[{"name": "A", "value": "old-secret"}, {"name": "B", "value": "1"}],
    )
    b = rev(
        old,
        3,
        "b",
        "2026-11-03T18:05:00Z",
        env=[
            {"name": "A", "value": "new-secret"},
            {"name": "B", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        ],
    )
    (row,) = w.config_changes("Deployment", deploy, [old, a, b], "6m", NOW)
    assert row["revision"] == 3
    assert row["envChanged"] == [
        {"container": "app", "name": "A"},
        {"container": "app", "name": "B"},
    ]
    assert row["envAdded"] == [] and row["envRemoved"] == []
    assert "secret" not in json.dumps(row).replace('"s"', "")


def test_config_changes_report_other_pod_spec_and_annotation_changes_by_name():
    deploy, old = rs_pair()
    restarted = rev(
        old,
        2,
        "r",
        "2026-11-03T18:00:00Z",
        annotations={"kubectl.kubernetes.io/restartedAt": "2026-11-03T18:00:00Z"},
        nodeSelector={"disk": "ssd"},
        command=["run", "--flag"],
    )
    (row,) = w.config_changes("Deployment", deploy, [old, restarted], "11m", NOW)
    assert row["otherPodSpecChanged"] == [
        "annotations.kubectl.kubernetes.io/restartedAt",
        "containers.app.command",
        "nodeSelector",
    ]
    assert row["imageChanges"] == [] and "run" not in json.dumps(
        {k: v for k, v in row.items() if k != "otherPodSpecChanged"}
    )


def test_template_labels_do_not_count_as_changes():
    deploy, old = rs_pair()
    other = rev(old, 2, "o", "2026-11-03T18:00:00Z")
    other["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] = "different"
    (row,) = w.config_changes("Deployment", deploy, [old, other], "11m", NOW)
    assert row["otherPodSpecChanged"] == []


def test_nested_lists_are_capped_at_50_and_flagged():
    deploy, old = rs_pair()
    many = rev(
        old,
        2,
        "m",
        "2026-11-03T18:00:00Z",
        env=[{"name": f"E{i:03}", "value": "1"} for i in range(80)],
    )
    (row,) = w.config_changes("Deployment", deploy, [old, many], "11m", NOW)
    assert len(row["envAdded"]) == 50 and row["truncated"] is True


def test_probe_config_includes_native_sidecar_init_containers_only():
    template = {
        "spec": {
            "initContainers": [
                {"name": "migrate", "image": "x"},
                {
                    "name": "proxy",
                    "restartPolicy": "Always",
                    "ports": [{"containerPort": 15000}],
                    "readinessProbe": {"tcpSocket": {"port": 15000}},
                },
            ],
            "containers": [{"name": "app"}],
        }
    }
    rows = w.probe_config(template)
    assert {(r["container"], r["init"]) for r in rows} == {("app", False), ("proxy", True)}
    ready = next(r for r in rows if r["container"] == "proxy" and r["probe"] == "readiness")
    assert ready["present"] and ready["portDeclared"] is True


def test_warning_events_ignore_events_for_an_earlier_incarnation_of_a_pod():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    old = ev("Pod", "db-0", "BackOff", "2026-11-03T17:55:00Z")
    old["involvedObject"]["uid"] = "old-uid"
    new = ev("Pod", "db-0", "Unhealthy", "2026-11-03T17:56:00Z")
    new["involvedObject"]["uid"] = "new-uid"
    rows = w.warning_events(
        [old, new], {"Pod": {"db-0"}}, "1h", now, 10, pod_uids={"db-0": "new-uid"}
    )
    assert [r["reason"] for r in rows] == ["Unhealthy"]
    assert w.pod_uids(owned("healthy", "StatefulSet", "db")[2]) == {
        "db-0": owned("healthy", "StatefulSet", "db")[2][0]["metadata"]["uid"]
    }


def test_hpa_current_metrics_are_keyed_on_the_full_identity():
    template = {
        "spec": {
            "containers": [
                {"name": "a", "resources": {"requests": {"cpu": "1m"}}},
                {"name": "b", "resources": {"requests": {"cpu": "1m"}}},
            ]
        }
    }
    metrics = [
        {
            "type": "ContainerResource",
            "containerResource": {
                "name": "cpu",
                "container": c,
                "target": {"type": "Utilization", "averageUtilization": 50},
            },
        }
        for c in ("a", "b")
    ] + [
        {
            "type": "Pods",
            "pods": {
                "metric": {"name": "qps", "selector": {"matchLabels": {"k": s}}},
                "target": {"type": "AverageValue", "averageValue": "10"},
            },
        }
        for s in ("x", "y")
    ]
    current = [
        {
            "type": "ContainerResource",
            "containerResource": {
                "name": "cpu",
                "container": "a",
                "current": {"averageUtilization": 11},
            },
        },
        {
            "type": "ContainerResource",
            "containerResource": {
                "name": "cpu",
                "container": "b",
                "current": {"averageUtilization": 22},
            },
        },
        {
            "type": "Pods",
            "pods": {
                "metric": {"name": "qps", "selector": {"matchLabels": {"k": "y"}}},
                "current": {"averageValue": "7"},
            },
        },
        {
            "type": "Pods",
            "pods": {
                "metric": {"name": "qps", "selector": {"matchLabels": {"k": "x"}}},
                "current": {"averageValue": "3"},
            },
        },
    ]
    status = {"currentReplicas": 1, "desiredReplicas": 1, "currentMetrics": current}
    (r,) = w.hpa_status([hpa(metrics=metrics, status=status)], "Deployment", "web", template)
    assert [(m["container"], m["current"]) for m in r["metrics"][:2]] == [("a", 11), ("b", 22)]
    assert [m["current"] for m in r["metrics"][2:]] == ["3", "7"]


def test_hpa_with_no_metrics_reports_the_controller_default_cpu_target():
    h = hpa()
    h["spec"]["metrics"] = []
    no_req = {"spec": {"containers": [{"name": "app"}]}}
    (r,) = w.hpa_status([h], "Deployment", "web", no_req)
    assert r["metrics"] == [] and r["defaultCpuTarget"] == 80
    assert r["resourceMetricWithoutRequests"] is True


def test_hpa_metrics_and_messages_are_bounded():
    h = hpa()
    h["spec"]["metrics"] = [
        {
            "type": "Pods",
            "pods": {
                "metric": {"name": f"m{i}"},
                "target": {"type": "AverageValue", "averageValue": "1"},
            },
        }
        for i in range(80)
    ]
    h["status"]["conditions"] = [{"type": "X", "status": "True", "message": "y" * 3000}]
    (r,) = w.hpa_status([h], "Deployment", "web", {"spec": {"containers": []}})
    assert len(r["metrics"]) == 50 and r["truncated"] is True
    assert len(r["conditions"][0]["message"]) == 1024 and r["conditions"][0]["truncated"] is True
