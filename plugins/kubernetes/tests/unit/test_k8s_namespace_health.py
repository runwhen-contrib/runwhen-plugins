"""Unit tests for k8s-namespace-health's lib/ against JSON captured from the
kind-rw-plugins scenarios (plugins/kubernetes/tests/kind). The cluster state was
captured at one moment; `NOW` is a fixed point just after it."""

import copy
import importlib.util
import json
import types
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest
import yaml
from k8s_namespace_health import common, events, fetch, jobs, owners, pdb, pods, quotas
from k8s_namespace_health import k8s
from k8s_namespace_health.k8s import RwError

DATA = Path(__file__).parent / "data" / "k8s_namespace_health"
SKILL = Path(__file__).parents[2] / "skills" / "k8s-namespace-health"
NOW = datetime(2026, 11, 3, 18, 29, tzinfo=timezone.utc)


def load(ns, resource):
    return json.loads((DATA / f"{ns}_{resource}.json").read_text())["items"]


def named(items, name):
    return next(i for i in items if i["metadata"]["name"] == name)


def by_name(rows):
    return {r["name"]: r for r in rows}


def ev(kind, name, reason, last, **extra):
    out = {
        "type": "Warning",
        "reason": reason,
        "message": f"{reason} message",
        "involvedObject": {"kind": kind, "name": name},
        "lastTimestamp": last,
        "firstTimestamp": last,
        "count": 1,
    }
    out.update(extra)
    return out


def warnings(ns="broken", since="1h", **replace):
    args = {k: load(ns, k) for k in ("events", "pods", "replicasets", "jobs")}
    args.update(replace)
    return events.warning_events(
        args["events"], args["pods"], args["replicasets"], args["jobs"], since, NOW, 200
    )


def unhealthy(ns="broken", threshold=3, **replace):
    args = {k: load(ns, k) for k in ("pods", "replicasets", "jobs")}
    args.update(replace)
    return pods.unhealthy_pods(args["pods"], args["replicasets"], args["jobs"], threshold)


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert common.parse_duration("90s").total_seconds() == 90
    assert common.parse_duration("1h30m").total_seconds() == 5400
    assert common.parse_duration("500ms").total_seconds() == 0.5
    assert common.parse_duration("1m30s250ms").total_seconds() == 90.25
    for bad in ("", "soon", "10", "5x", "1h junk"):
        with pytest.raises(ValueError):
            common.parse_duration(bad)


def test_parse_quantity_handles_suffixes_and_exponents():
    q = common.parse_quantity
    assert q("500m") == Fraction(1, 2) and q("2") == 2 and q("1Gi") == 1024**3
    assert (
        q("1G") == 10**9 and q("129e6") == 129_000_000 and q("1E") == 10**18 and q("1.5Ki") == 1536
    )
    assert q("100m") < q("1") < q("1k")


def test_unknown_quantity_suffix_is_refused_not_read_as_a_number():
    # classic resource_quota_check.sh treated unknown unit suffixes as bytes
    for bad in ("5X", "1Gb", "", "abc"):
        with pytest.raises(ValueError):
            common.parse_quantity(bad)


def test_selector_matching():
    sel = {
        "matchLabels": {"app": "web", "tier": "fe"},
        "matchExpressions": [{"key": "env", "operator": "In", "values": ["a", "b"]}],
    }
    assert common.matches_selector({"app": "web", "tier": "fe", "env": "a"}, sel)
    assert not common.matches_selector({"app": "web", "env": "a"}, sel)
    assert not common.matches_selector({"app": "web", "tier": "fe", "env": "c"}, sel)
    assert common.matches_selector({"anything": "x"}, {})
    with pytest.raises(ValueError):
        common.matches_selector({}, {"matchExpressions": [{"key": "k", "operator": "Weird"}]})


# ------------------------------------------------------------- owners ----


def test_owner_chain_pod_replicaset_deployment_and_direct_owners():
    pod_items = load("broken", "pods")
    index = owners.index_objects(
        ReplicaSet=load("broken", "replicasets"), Job=load("broken", "jobs")
    )
    assert owners.resolve_owner(named(pod_items, "crashloop-77d67d4789-f6rbl"), index) == {
        "kind": "Deployment",
        "name": "crashloop",
        "resolved": True,
    }
    assert owners.resolve_owner(named(pod_items, "broken-sts-0"), index)["kind"] == "StatefulSet"
    assert owners.resolve_owner(named(pod_items, "fails-24pcc"), index) == {
        "kind": "Job",
        "name": "fails",
        "resolved": True,
    }
    assert owners.resolve_owner({"metadata": {"name": "bare"}}, index) is None


def test_job_owned_by_a_cronjob_resolves_to_the_cronjob():
    job = copy.deepcopy(named(load("broken", "jobs"), "fails"))
    job["metadata"]["ownerReferences"] = [
        {"kind": "CronJob", "name": "nightly", "uid": "u", "controller": True}
    ]
    pod = named(load("broken", "pods"), "fails-24pcc")
    assert owners.resolve_owner(pod, owners.index_objects(Job=[job])) == {
        "kind": "CronJob",
        "name": "nightly",
        "resolved": True,
    }


def test_owner_chain_stops_unresolved_when_the_replicaset_is_gone():
    pod = named(load("broken", "pods"), "crashloop-77d67d4789-f6rbl")
    out = owners.resolve_owner(pod, owners.index_objects(ReplicaSet=[]))
    assert out["kind"] == "ReplicaSet" and out["resolved"] is False


# ------------------------------------------------------ warning-events ----


def test_broken_namespace_warning_events_are_grouped_with_owners():
    rows = by_name(warnings())
    crash = rows["crashloop-77d67d4789-f6rbl"]
    assert crash["kind"] == "Pod" and crash["reason"] == "BackOff" and crash["count"] >= 1
    assert crash["owner"] == {"kind": "Deployment", "name": "crashloop", "resolved": True}
    assert rows["broken-sts-0"]["owner"]["kind"] == "StatefulSet"
    assert rows["bad-probe-776cdc668b-l46vv"]["reason"] == "Unhealthy"
    assert rows["bad-probe-776cdc668b-l46vv"]["owner"]["name"] == "bad-probe"
    # the Job's own event names no owner (no CronJob); a PVC is not an owned kind
    assert rows["fails"]["kind"] == "Job" and rows["fails"]["reason"] == "BackoffLimitExceeded"
    assert rows["fails"]["owner"] is None and rows["fails"]["objectExists"] is True
    assert rows["pending"]["kind"] == "PersistentVolumeClaim" and rows["pending"]["owner"] is None
    assert rows["pending"]["objectExists"] is None


def test_events_are_ordered_newest_first_and_capped():
    rows = warnings()
    assert [r["lastSeen"] for r in rows] == sorted((r["lastSeen"] for r in rows), reverse=True)
    assert len(events.warning_events(load("broken", "events"), [], [], [], "1h", NOW, 2)) == 2


def test_healthy_namespace_only_has_startup_probe_warnings_on_web():
    rows = warnings("healthy", "2h")
    assert {(r["kind"], r["reason"], r["owner"]["name"]) for r in rows} == {
        ("Pod", "Unhealthy", "web")
    }


def test_event_window_filters_on_last_seen():
    # the Job event was last seen nearly an hour before NOW
    assert "fails" in by_name(warnings(since="2h"))
    assert "fails" not in by_name(warnings(since="30m"))
    assert warnings(since="1s") == []


def test_event_about_a_deleted_pod_is_kept_with_no_owner():
    # classic find_resource_owners.sh turned a deleted pod into 'Unknown' and dropped the event
    remaining = [
        p for p in load("broken", "pods") if not p["metadata"]["name"].startswith("crashloop")
    ]
    row = by_name(warnings(pods=remaining))["crashloop-77d67d4789-f6rbl"]
    assert row["reason"] == "BackOff" and row["objectExists"] is False and row["owner"] is None


def test_event_owner_stays_the_replicaset_when_the_replicaset_is_gone():
    row = by_name(warnings(replicasets=[]))["crashloop-77d67d4789-f6rbl"]
    assert row["owner"] == {"kind": "ReplicaSet", "name": "crashloop-77d67d4789", "resolved": False}


def test_events_group_and_sum_counts_and_ignore_normal_events():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
    items = [
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
        ev("Pod", "p1", "Pulled", "2026-11-03T17:59:00Z", type="Normal"),
    ]
    (row,) = events.warning_events(items, [], [], [], "1h", now, 200)
    assert row["count"] == 5 and row["firstSeen"] == "2026-11-03T17:30:00Z"
    assert row["lastSeen"] == "2026-11-03T17:55:00Z" and row["message"] == "newer"


def test_event_times_fall_back_to_series_and_event_time():
    now = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
    series = ev(
        "Pod",
        "p1",
        "Unhealthy",
        "2026-11-03T10:00:00Z",
        series={"count": 7, "lastObservedTime": "2026-11-03T17:58:00.000000Z"},
    )
    series["lastTimestamp"] = None
    only_event_time = ev(
        "Pod", "p2", "Failed", "2026-11-03T10:00:00Z", eventTime="2026-11-03T17:45:00.000000Z"
    )
    only_event_time["lastTimestamp"] = None
    rows = by_name(events.warning_events([series, only_event_time], [], [], [], "30m", now, 200))
    assert rows["p1"]["count"] == 7 and rows["p1"]["lastSeen"] == "2026-11-03T17:58:00Z"
    assert rows["p2"]["lastSeen"] == "2026-11-03T17:45:00Z"


# ------------------------------------------------------ unhealthy-pods ----


def test_broken_namespace_unhealthy_pods_with_owners():
    rows = by_name(unhealthy())
    crash = rows["crashloop-77d67d4789-f6rbl"]
    assert crash["states"] == ["notReady", "waiting", "restarts"]
    assert (
        crash["waiting"][0]["reason"] == "CrashLoopBackOff"
        and crash["restarts"][0]["restartCount"] >= 3
    )
    assert crash["owner"] == {"kind": "Deployment", "name": "crashloop", "resolved": True}
    assert rows["oom-d84c6594b-fwg7x"]["restarts"][0]["lastTerminated"]["reason"] == "OOMKilled"
    assert rows["broken-sts-0"]["owner"]["kind"] == "StatefulSet"
    # ImagePullBackOff: Pending phase, scheduled, container waiting
    img = rows["bad-image-866577b69d-llmzv"]
    assert img["states"] == ["pending", "waiting"] and img["pending"] == {
        "scheduled": True,
        "reason": None,
        "message": None,
    }
    assert img["waiting"][0]["reason"] == "ImagePullBackOff"
    # Running but never Ready: a probe on a port nothing listens on
    assert rows["bad-probe-776cdc668b-l46vv"]["states"] == ["notReady"]
    # Failed Job pods
    failed = rows["fails-24pcc"]
    assert failed["states"] == ["failed"] and failed["owner"] == {
        "kind": "Job",
        "name": "fails",
        "resolved": True,
    }
    assert failed["failed"]["terminated"][0]["exitCode"] == 1
    # healthy pods of the broken namespace are not listed
    assert "still-running-l2nr9" not in rows
    assert not any(n.startswith("no-pdb-") for n in rows)


def test_most_states_first():
    rows = unhealthy()
    assert [len(r["states"]) for r in rows] == sorted(
        (len(r["states"]) for r in rows), reverse=True
    )


def test_healthy_namespace_has_no_unhealthy_pods():
    assert unhealthy("healthy") == []


def test_restart_threshold_controls_the_restarts_state():
    assert any("restarts" in r["states"] for r in unhealthy(threshold=1))
    assert all("restarts" not in r["states"] for r in unhealthy(threshold=10_000))


def test_unschedulable_pending_pod_carries_the_scheduler_reason_however_old():
    pod = copy.deepcopy(named(load("broken", "pods"), "bad-image-866577b69d-llmzv"))
    pod["status"]["conditions"] = [
        {
            "type": "PodScheduled",
            "status": "False",
            "reason": "Unschedulable",
            "message": "0/1 nodes are available: Insufficient cpu.",
            "lastTransitionTime": "2020-01-01T00:00:00Z",
        }
    ]
    pod["status"].pop("containerStatuses", None)
    pod["metadata"]["creationTimestamp"] = "2020-01-01T00:00:00Z"
    # classic filtered on lastTransitionTime inside the look-back, so a pod pending for years aged out
    (row,) = pods.unhealthy_pods([pod], [], [], 3)
    assert row["states"] == ["pending"]
    assert row["pending"] == {
        "scheduled": False,
        "reason": "Unschedulable",
        "message": "0/1 nodes are available: Insufficient cpu.",
    }


def test_failed_pod_keeps_its_reason_and_succeeded_pod_is_not_listed():
    failed = copy.deepcopy(named(load("broken", "pods"), "fails-24pcc"))
    failed["status"].update(
        {"reason": "Evicted", "message": "The node was low on resource: memory."}
    )
    done = copy.deepcopy(failed)
    done["metadata"]["name"] = "done"
    done["status"] = {
        "phase": "Succeeded",
        "conditions": [{"type": "Ready", "status": "False", "reason": "PodCompleted"}],
    }
    rows = by_name(pods.unhealthy_pods([failed, done], [], [], 3))
    assert rows["fails-24pcc"]["failed"]["reason"] == "Evicted"
    assert "done" not in rows


def test_pod_counts_report_totals_before_the_cap():
    all_pods = load("broken", "pods")
    rows = unhealthy()
    counts = pods.pod_counts(all_pods, len(rows))
    assert counts["total"] == len(all_pods) and counts["unhealthy"] == len(rows)
    assert counts["byPhase"]["Failed"] == 2
    assert pods.pod_counts([], 0) == {"total": 0, "unhealthy": 0, "byPhase": {}}


# --------------------------------------------------------- failed-jobs ----


def test_failed_job_is_reported_with_its_reason():
    fails = by_name(jobs.failed_jobs(load("broken", "jobs"), NOW, 60))["fails"]
    assert fails["failed"] is True and fails["failureReason"] == "BackoffLimitExceeded"
    assert fails["failedPods"] == 2 and fails["backoffLimit"] == 1 and fails["cronJob"] is None
    assert fails["failedAt"] is not None and fails["longRunning"] is False


def test_active_job_is_listed_only_after_it_runs_longer_than_the_setting():
    assert "still-running" not in by_name(jobs.failed_jobs(load("broken", "jobs"), NOW, 60))
    later = NOW + timedelta(hours=2)
    row = by_name(jobs.failed_jobs(load("broken", "jobs"), later, 60))["still-running"]
    assert (
        row["failed"] is False and row["longRunning"] is True and 120 <= row["activeMinutes"] < 130
    )
    assert "still-running" not in by_name(jobs.failed_jobs(load("broken", "jobs"), later, 180))


def test_healthy_namespace_has_no_failed_jobs():
    assert jobs.failed_jobs(load("healthy", "jobs"), NOW, 60) == []


def test_deadline_exceeded_job_and_cronjob_owner():
    job = copy.deepcopy(named(load("broken", "jobs"), "fails"))
    job["metadata"]["ownerReferences"] = [
        {"kind": "CronJob", "name": "nightly", "uid": "u", "controller": True}
    ]
    cond = next(c for c in job["status"]["conditions"] if c["type"] == "Failed")
    cond.update(
        {"reason": "DeadlineExceeded", "message": "Job was active longer than specified deadline"}
    )
    (row,) = jobs.failed_jobs([job], NOW, 60)
    assert row["failureReason"] == "DeadlineExceeded" and row["cronJob"] == "nightly"


def test_suspended_or_inactive_job_is_not_long_running():
    job = copy.deepcopy(named(load("broken", "jobs"), "still-running"))
    later = NOW + timedelta(days=3)
    job["spec"]["suspend"] = True
    assert jobs.failed_jobs([job], later, 60) == []
    job["spec"]["suspend"] = False
    job["status"]["active"] = 0
    assert jobs.failed_jobs([job], later, 60) == []


def test_job_counts():
    assert jobs.job_counts(load("broken", "jobs")) == {
        "total": 2,
        "failed": 1,
        "active": 1,
        "complete": 0,
    }
    assert jobs.job_counts([]) == {"total": 0, "failed": 0, "active": 0, "complete": 0}


# ----------------------------------------------------- resource-quotas ----


def test_broken_namespace_quota_is_near_its_limit():
    rows = quotas.quota_usage(load("broken", "resourcequotas"), 0.8, 200)
    by_resource = {(r["quota"], r["resource"]): r for r in rows}
    pods_row = by_resource[("tight", "pods")]
    assert pods_row["hard"] == "12" and int(pods_row["used"]) >= 10
    assert pods_row["ratio"] >= 0.8 and pods_row["atOrAbove"] is True
    jobs_row = by_resource[("tight", "count/jobs.batch")]
    assert jobs_row["ratio"] == 0.5 and jobs_row["atOrAbove"] is False
    assert rows[0]["resource"] == "pods"  # highest ratio first


def test_healthy_namespace_quota_has_headroom():
    (row,) = quotas.quota_usage(load("healthy", "resourcequotas"), 0.8, 200)
    assert row["resource"] == "pods" and row["ratio"] < 0.8 and row["atOrAbove"] is False


def test_threshold_setting_decides_at_or_above():
    q = [
        {
            "metadata": {"name": "q"},
            "status": {
                "hard": {"cpu": "2", "memory": "1Gi"},
                "used": {"cpu": "1500m", "memory": "512Mi"},
            },
        }
    ]
    rows = {r["resource"]: r for r in quotas.quota_usage(q, 0.75, 200)}
    assert rows["cpu"]["ratio"] == 0.75 and rows["cpu"]["atOrAbove"] is True
    assert rows["memory"]["ratio"] == 0.5 and rows["memory"]["atOrAbove"] is False
    assert all(not r["atOrAbove"] for r in quotas.quota_usage(q, 0.9, 200))
    assert all(r["atOrAbove"] for r in quotas.quota_usage(q, 0.5, 200))


def test_quota_edges_zero_hard_not_yet_computed_and_bad_quantity():
    zero = [{"metadata": {"name": "z"}, "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}}}]
    (row,) = quotas.quota_usage(zero, 0.8, 200)
    assert row["ratio"] is None and row["atOrAbove"] is True
    fresh = [{"metadata": {"name": "f"}, "spec": {"hard": {"pods": "10"}}}]
    (row,) = quotas.quota_usage(fresh, 0.8, 200)
    assert row["used"] is None and row["ratio"] is None and row["atOrAbove"] is None
    odd = [
        {"metadata": {"name": "o"}, "status": {"hard": {"memory": "1Gb"}, "used": {"memory": "1"}}}
    ]
    with pytest.raises(ValueError):
        quotas.quota_usage(odd, 0.8, 200)


def test_no_quotas_is_an_empty_list():
    assert quotas.quota_usage([], 0.8, 200) == []


# --------------------------------------------------------- pdb-coverage ----


def coverage(ns, **replace):
    args = {k: load(ns, k) for k in ("deployments", "statefulsets", "pdbs")}
    args.update(replace)
    return pdb.pdb_coverage(args["deployments"], args["statefulsets"], args["pdbs"], 200)


def test_broken_namespace_multi_replica_workload_without_pdb_and_zero_disruption_pdb():
    uncovered, zero, inspected = coverage("broken")
    assert uncovered == [{"kind": "Deployment", "name": "no-pdb", "replicas": 2}]
    (crash,) = zero
    assert crash["name"] == "crashloop" and crash["disruptionsAllowed"] == 0
    assert (
        crash["currentHealthy"] == 0 and crash["desiredHealthy"] == 1 and crash["minAvailable"] == 1
    )
    assert crash["selects"] == [{"kind": "Deployment", "name": "crashloop"}]
    assert inspected == {"deployments": 7, "statefulsets": 1, "pdbs": 1}


def test_healthy_namespace_is_covered_and_allows_disruptions():
    uncovered, zero, inspected = coverage("healthy")
    assert uncovered == [] and zero == []
    assert inspected == {"deployments": 2, "statefulsets": 1, "pdbs": 1}


def test_pdb_matches_template_labels_with_multi_label_selectors():
    # classic compared single label pairs, so a multi-label selector mis-matched
    deploy = copy.deepcopy(named(load("broken", "deployments"), "no-pdb"))
    deploy["spec"]["template"]["metadata"]["labels"] = {"app": "no-pdb", "tier": "fe"}

    def pdb_for(labels):
        return {
            "metadata": {"name": "p"},
            "spec": {"selector": {"matchLabels": labels}},
            "status": {"disruptionsAllowed": 1},
        }

    assert (
        coverage(
            "broken",
            deployments=[deploy],
            statefulsets=[],
            pdbs=[pdb_for({"app": "no-pdb", "tier": "fe"})],
        )[0]
        == []
    )
    assert (
        coverage(
            "broken",
            deployments=[deploy],
            statefulsets=[],
            pdbs=[pdb_for({"app": "no-pdb", "tier": "be"})],
        )[0]
        != []
    )


def test_pdb_selector_edge_cases_empty_selects_all_and_missing_selects_none():
    deploy = named(load("broken", "deployments"), "no-pdb")
    base = {"metadata": {"name": "p"}, "status": {"disruptionsAllowed": 1}}
    assert (
        coverage(
            "broken",
            deployments=[deploy],
            statefulsets=[],
            pdbs=[{**base, "spec": {"selector": {}}}],
        )[0]
        == []
    )
    assert (
        coverage("broken", deployments=[deploy], statefulsets=[], pdbs=[{**base, "spec": {}}])[0]
        != []
    )


def test_statefulset_with_several_replicas_and_no_pdb_is_listed():
    sts = copy.deepcopy(named(load("broken", "statefulsets"), "broken-sts"))
    sts["spec"]["replicas"] = 3
    uncovered = coverage("broken", deployments=[], statefulsets=[sts], pdbs=[])[0]
    assert uncovered == [{"kind": "StatefulSet", "name": "broken-sts", "replicas": 3}]


def test_pdb_without_status_is_not_listed_as_zero_disruption():
    pending = {"metadata": {"name": "p"}, "spec": {"selector": {"matchLabels": {"app": "x"}}}}
    assert coverage("broken", deployments=[], statefulsets=[], pdbs=[pending])[1] == []


# ----------------------------------------------- uid, bounds and text ----


def test_event_about_a_recreated_pod_is_not_attributed_to_the_new_pod_by_name():
    # a StatefulSet pod keeps its name across restarts; the old incarnation's events
    # carry the old uid and must not be matched to the current pod by name alone
    pod = copy.deepcopy(named(load("broken", "pods"), "broken-sts-0"))
    stale = ev("Pod", "broken-sts-0", "BackOff", "2026-11-03T18:20:00Z")
    stale["involvedObject"]["uid"] = "an-older-uid"
    current = ev("Pod", "broken-sts-0", "Unhealthy", "2026-11-03T18:21:00Z")
    current["involvedObject"]["uid"] = pod["metadata"]["uid"]
    rows = {
        r["reason"]: r
        for r in events.warning_events([stale, current], [pod], [], [], "1h", NOW, 200)
    }
    assert rows["BackOff"]["objectExists"] is False and rows["BackOff"]["owner"] is None
    assert (
        rows["Unhealthy"]["objectExists"] is True
        and rows["Unhealthy"]["owner"]["kind"] == "StatefulSet"
    )
    assert rows["BackOff"]["uid"] == "an-older-uid"


def test_incarnations_of_a_pod_are_grouped_apart():
    a, b = (ev("Pod", "p", "BackOff", "2026-11-03T18:20:00Z") for _ in range(2))
    a["involvedObject"]["uid"], b["involvedObject"]["uid"] = "u1", "u2"
    assert len(events.warning_events([a, b], [], [], [], "1h", NOW, 200)) == 2


def test_long_messages_are_cut_and_flagged():
    long = ev("Pod", "p", "Failed", "2026-11-03T18:20:00Z", message="x" * 5000)
    short = ev("Pod", "q", "Failed", "2026-11-03T18:20:00Z", message="short")
    rows = by_name(events.warning_events([long, short], [], [], [], "1h", NOW, 200))
    assert len(rows["p"]["message"]) == 1024 and rows["p"]["truncated"] is True
    assert rows["q"]["message"] == "short" and rows["q"]["truncated"] is False


def test_pod_messages_and_nested_lists_are_bounded():
    pod = copy.deepcopy(named(load("broken", "pods"), "bad-image-866577b69d-llmzv"))
    base = pod["status"]["containerStatuses"][0]
    pod["status"]["containerStatuses"] = []
    for i in range(60):
        status = copy.deepcopy(base)
        status["name"] = f"c{i}"
        status["state"] = {
            "waiting": {"reason": "ImagePullBackOff", "message": "m" * 3000 if i == 0 else "m"}
        }
        pod["status"]["containerStatuses"].append(status)
    (row,) = pods.unhealthy_pods([pod], [], [], 3)
    assert len(row["waiting"]) == 50 and row["truncated"] is True
    assert len(row["waiting"][0]["message"]) == 1024


def test_init_containers_count_and_are_marked():
    pod = copy.deepcopy(named(load("healthy", "pods"), "web-868f7b7477-ddzml"))
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "setup",
            "restartCount": 5,
            "ready": False,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {
                "terminated": {
                    "reason": "Error",
                    "exitCode": 1,
                    "finishedAt": "2026-11-03T18:00:00Z",
                }
            },
        }
    ]
    (row,) = pods.unhealthy_pods([pod], [], [], 3)
    assert row["waiting"][0]["init"] is True and row["restarts"][0]["init"] is True
    assert row["restarts"][0]["container"] == "setup"


def test_failed_job_message_is_cut():
    job = copy.deepcopy(named(load("broken", "jobs"), "fails"))
    next(c for c in job["status"]["conditions"] if c["type"] == "Failed")["message"] = "y" * 2000
    (row,) = jobs.failed_jobs([job], NOW, 60)
    assert len(row["failureMessage"]) == 1024 and row["truncated"] is True


def test_pdb_selects_list_is_bounded():
    deployments = []
    for i in range(60):
        d = copy.deepcopy(named(load("broken", "deployments"), "no-pdb"))
        d["metadata"]["name"] = f"d{i}"
        deployments.append(d)
    blocking = {
        "metadata": {"name": "all"},
        "spec": {"selector": {}, "minAvailable": 1},
        "status": {"disruptionsAllowed": 0},
    }
    _, zero, _ = coverage("broken", deployments=deployments, statefulsets=[], pdbs=[blocking])
    assert len(zero[0]["selects"]) == 50 and zero[0]["truncated"] is True


# ------------------------------------------------------------ k8s.py ----


@pytest.mark.parametrize(
    ("rc", "stderr", "code"),
    [
        (
            1,
            'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods" in the namespace "x"',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): namespaces "gone" not found', "not_found"),
        (
            1,
            "Unable to connect to the server: dial tcp 10.0.0.1:6443: connect: connection refused",
            "unreachable",
        ),
        (
            1,
            "Unable to connect to the server: dial tcp: lookup api.example: no such host",
            "unreachable",
        ),
        (
            1,
            "Unable to connect to the server: x509: certificate signed by unknown authority",
            "unreachable",
        ),
        (
            1,
            "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)",
            "timeout",
        ),
        (1, "error: context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "widgets"', "dependency"),
        (1, "something odd", "internal"),
    ],
)
def test_kubectl_failures_are_classified(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


# ------------------------------------------------------------ scripts ----


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


FAILURES = {
    "permission_denied": 'Error from server (Forbidden): {r} is forbidden: User "u" cannot list resource "{r}"',
    "not_found": 'Error from server (NotFound): {r} "x" not found',
    "unreachable": "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
}


class FakeCtx:
    """Serves captured JSON for `kubectl [-n ns] get <resource> [name] -o json`,
    returning a finished process like the SDK's ctx.run (it does not raise).
    `fail` names a resource whose call exits non-zero with the stderr of `code`;
    `ns_dir` None makes every call NotFound. ctx.error records on `.errors`."""

    SHORT = {
        "namespaces": "namespace",
        "replicasets.apps": "replicasets",
        "jobs.batch": "jobs",
        "deployments.apps": "deployments",
        "statefulsets.apps": "statefulsets",
        "poddisruptionbudgets.policy": "pdbs",
    }

    def __init__(
        self, ns_dir, name, fail=None, code="permission_denied", rtype="kubernetes/namespace"
    ):
        self.resource = types.SimpleNamespace(name=name, type=rtype, cluster="c")
        self.settings = types.SimpleNamespace(
            max_items=200, restart_threshold=3, job_active_max_minutes=60, quota_ratio=0.8
        )
        self.ns_dir, self.fail, self.code = ns_dir, fail, code
        self.calls, self.errors = [], []

    @staticmethod
    def resource_of(argv):
        words = [a for a in argv[1:] if not a.startswith("-")]
        return next(
            w
            for w in words
            if w not in ("get", "rw-broken", "rw-healthy", "type=Warning", "gone", "x")
        )

    def run(self, argv):
        self.calls.append(argv)
        resource = self.resource_of(argv)
        if resource == self.fail:
            return Result(returncode=1, stderr=FAILURES[self.code].format(r=resource))
        if self.ns_dir is None:
            return Result(returncode=1, stderr=FAILURES["not_found"].format(r=resource))
        text = (DATA / f"{self.ns_dir}_{self.SHORT.get(resource, resource)}.json").read_text()
        if resource == "namespaces":
            text = json.dumps(json.loads(text)["items"][0])
        return Result(text)

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "message": message, "scope": scope})


def script(name):
    spec = importlib.util.spec_from_file_location(f"s_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TASKS = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
SCRIPTS = ["warning_events", "unhealthy_pods", "failed_jobs", "resource_quotas", "pdb_coverage"]
# primary fetches fail the task; secondary ones are recorded with ctx.error and the task keeps its facts
PRIMARY = {
    "warning_events": ["events"],
    "unhealthy_pods": ["pods"],
    "failed_jobs": ["jobs.batch"],
    "resource_quotas": ["resourcequotas"],
    "pdb_coverage": ["poddisruptionbudgets.policy"],
}
SECONDARY = {
    "warning_events": ["namespaces", "pods", "replicasets.apps", "jobs.batch"],
    "unhealthy_pods": ["namespaces", "replicasets.apps", "jobs.batch"],
    "failed_jobs": ["namespaces"],
    "resource_quotas": ["namespaces"],
    "pdb_coverage": ["namespaces", "deployments.apps", "statefulsets.apps"],
}


def declared(name):
    return next(t for t in TASKS if t["script"] == f"scripts/{name}.py")


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_returns_the_declared_outputs(name):
    ctx = FakeCtx("broken", "rw-broken")
    assert set(script(name).main(ctx)) == set(declared(name)["outputs"])
    assert ctx.errors == []


@pytest.mark.parametrize("code", sorted(FAILURES))
@pytest.mark.parametrize(
    ("name", "fail"), [(n, f) for n in SCRIPTS for f in PRIMARY[n]], ids=lambda v: str(v)
)
def test_a_failed_primary_call_raises_with_its_code_instead_of_returning_empty(name, fail, code):
    with pytest.raises(RwError) as exc:
        script(name).main(FakeCtx("broken", "rw-broken", fail=fail, code=code))
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("name", "fail"), [(n, f) for n in SCRIPTS for f in SECONDARY[n]], ids=lambda v: str(v)
)
def test_a_failed_secondary_call_is_an_error_on_the_result_and_the_primary_facts_stay(name, fail):
    ctx = FakeCtx("broken", "rw-broken", fail=fail)
    out = script(name).main(ctx)
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", fail)]
    assert set(out) == set(declared(name)["outputs"])


def test_an_identity_that_cannot_read_namespaces_gets_partial_results_not_a_failure():
    ctx = FakeCtx("healthy", "rw-healthy", fail="namespaces")
    out = script("unhealthy_pods").main(ctx)
    assert out["counts"]["total"] == 5
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]


def test_events_keep_their_facts_without_owners_when_pods_cannot_be_listed():
    ctx = FakeCtx("broken", "rw-broken", fail="pods")
    # the script reads the wall clock, so ask for a window that always covers the capture
    rows = by_name(script("warning_events").main(ctx, since="3650d")["events"])
    crash = rows["crashloop-77d67d4789-f6rbl"]
    assert crash["reason"] == "BackOff" and crash["owner"] is None and crash["objectExists"] is None
    assert rows["fails"]["objectExists"] is True  # Jobs were listed


def test_pdb_coverage_marks_what_it_could_not_inspect():
    ctx = FakeCtx("broken", "rw-broken", fail="deployments.apps")
    out = script("pdb_coverage").main(ctx)
    assert out["inspected"]["deployments"] is None and out["inspected"]["statefulsets"] == 1
    assert (
        out["uncovered"] == []
    )  # not a claim that nothing is uncovered: the error says deployments were not read
    assert ctx.errors[0]["scope"] == "deployments.apps"


def test_scripts_read_exactly_the_resources_their_permissions_declare():
    for name in SCRIPTS:
        ctx = FakeCtx("broken", "rw-broken")
        script(name).main(ctx)
        called = {ctx.resource_of(argv).split(".")[0] for argv in ctx.calls}
        granted = {r for p in declared(name)["permissions"] for r in p["resources"]}
        assert called == granted, name


@pytest.mark.parametrize("name", SCRIPTS)
def test_missing_namespace_raises_not_found(name):
    with pytest.raises(RwError) as exc:
        script(name).main(FakeCtx(None, "gone"))
    assert exc.value.code == "not_found"


def test_other_target_type_is_invalid_input():
    with pytest.raises(RwError) as exc:
        script("unhealthy_pods").main(FakeCtx("broken", "x", rtype="kubernetes/pod"))
    assert exc.value.code == "invalid_input"


def test_target_type_falls_back_to_the_path():
    ctx = FakeCtx("broken", "rw-broken")
    ctx.resource = types.SimpleNamespace(
        name="rw-broken", type=None, path="kubernetes/clusters/c/namespaces/rw-broken"
    )
    assert fetch.target_type(ctx) == "kubernetes/namespace"
    assert script("failed_jobs").main(ctx)["counts"]["total"] == 2


def test_bad_since_is_invalid_input_before_any_kubectl_call():
    ctx = FakeCtx("broken", "rw-broken")
    with pytest.raises(RwError) as exc:
        script("warning_events").main(ctx, since="yesterday")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_since_accepts_milliseconds():
    assert script("warning_events").main(FakeCtx("broken", "rw-broken"), since="500ms") == {
        "events": []
    }


def test_a_subprocess_timeout_is_a_timeout_error():
    import subprocess

    class Slow(FakeCtx):
        def run(self, argv):
            raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(RwError) as exc:
        script("failed_jobs").main(Slow("broken", "rw-broken"))
    assert exc.value.code == "timeout"
