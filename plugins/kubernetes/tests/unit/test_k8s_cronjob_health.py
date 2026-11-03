"""Unit tests for k8s-cronjob-health's lib/ and scripts: schedule evaluation, Job runs,
error handling and permissions. Captured JSON comes from the `rw-cron` namespace of the
kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/30-k8s-cronjob-health.*);
the cluster state was captured at one moment and `NOW` is a fixed point just after it.
The cron search itself is tested in test_k8s_cronjob_health_cron.py."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from k8s_cronjob_health import common, fetch, k8s, runs, schedule
from k8s_cronjob_health.k8s import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

DATA = Path(__file__).parent / "data" / "k8s_cronjob_health"
SKILL = Path(__file__).parents[2] / "skills" / "k8s-cronjob-health"
UTC = timezone.utc
NS = "rw-cron"


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=UTC)


NOW = at("2026-11-03T12:10:20Z")  # for the synthetic objects below


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def cj(schedule_text="* * * * *", created="2026-11-03T12:00:00Z", last=None, success=None, **spec):
    """A CronJob object with only the fields the code reads."""
    body = {
        "metadata": {"name": "cj", "namespace": NS, "uid": "cj-uid", "creationTimestamp": created},
        "spec": {"schedule": schedule_text, **spec},
        "status": {},
    }
    if last:
        body["status"]["lastScheduleTime"] = last
    if success:
        body["status"]["lastSuccessfulTime"] = success
    return body


def evaluate(cronjob, now=NOW, since="1h", grace=60):
    return schedule.evaluate(cronjob, now, since, grace)


def job(
    name,
    start,
    state="Complete",
    finished=None,
    owner="cj-uid",
    reason=None,
    message=None,
    uid=None,
    **status,
):
    """A Job object owned by `owner` (uid)."""
    body = {
        "metadata": {
            "name": name,
            "namespace": NS,
            "uid": uid or f"{name}-uid",
            "creationTimestamp": start,
            "ownerReferences": [
                {"kind": "CronJob", "name": "cj", "uid": owner, "controller": True}
            ],
        },
        "spec": {"backoffLimit": 0},
        "status": {"startTime": start, **status},
    }
    if state in ("Complete", "Failed"):
        cond = {"type": state, "status": "True", "lastTransitionTime": finished or start}
        if reason:
            cond["reason"] = reason
        if message:
            cond["message"] = message
        body["status"]["conditions"] = [cond]
        if state == "Complete" and finished:
            body["status"]["completionTime"] = finished
    return body


def pod(name, job_obj, containers=(), init=(), phase="Failed"):
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [
                {
                    "kind": "Job",
                    "name": job_obj["metadata"]["name"],
                    "uid": job_obj["metadata"]["uid"],
                }
            ],
        },
        "status": {
            "phase": phase,
            "initContainerStatuses": list(init),
            "containerStatuses": list(containers),
        },
    }


def terminated(name, reason, code, finished="2026-11-03T12:00:40Z", last=False, restarts=0):
    t = {"reason": reason, "exitCode": code, "finishedAt": finished, "message": "SECRET-ish text"}
    if last:
        return {
            "name": name,
            "restartCount": restarts,
            "state": {"running": {}},
            "lastState": {"terminated": t},
        }
    return {"name": name, "restartCount": restarts, "state": {"terminated": t}}


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert common.parse_duration("90s").total_seconds() == 90
    assert common.parse_duration("1h30m").total_seconds() == 5400
    assert common.parse_duration("500ms").total_seconds() == 0.5
    assert common.parse_duration("2d").total_seconds() == 172800
    for bad in ("", "soon", "10", "5x", "1h junk"):
        with pytest.raises(ValueError):
            common.parse_duration(bad)


def test_trunc_and_nested_bounds():
    assert common.trunc(None) == (None, False)
    assert common.trunc("x" * 1024) == ("x" * 1024, False)
    assert common.trunc("x" * 1025) == ("x" * 1024, True)
    assert common.nested(range(50)) == (list(range(50)), False)
    assert common.nested(range(51))[1] is True and len(common.nested(range(51))[0]) == 50


# ----------------------------------------------------------- schedule ----


def test_a_cronjob_that_ran_its_last_slot_is_not_behind():
    out = evaluate(cj(last="2026-11-03T12:10:00Z", success="2026-11-03T12:10:05Z"))
    assert out["lastExpectedFire"] == "2026-11-03T12:10:00Z"
    assert out["nextExpectedFire"] == "2026-11-03T12:11:00Z"
    assert (out["missedFires"], out["missedFiresCapped"]) == (0, False)
    assert (out["missedSince"], out["behindSchedule"]) == (False, False)
    assert out["deadlineExceeded"] is None
    assert out["evaluatedTimeZone"] == "UTC"


def test_scheduled_slots_after_lastScheduleTime_are_missed_runs():
    out = evaluate(cj(last="2026-11-03T12:00:00Z"))  # 12:01 .. 12:09 are over 60s old at 12:10:20
    assert out["lastExpectedFire"] == "2026-11-03T12:10:00Z"
    assert out["missedFires"] == 9
    assert (out["missedSince"], out["behindSchedule"]) == (True, True)


def test_a_slot_inside_the_grace_period_is_not_yet_missed():
    # 12:10 fired 20s ago: the controller may still be starting it
    assert evaluate(cj(last="2026-11-03T12:09:00Z"))["behindSchedule"] is False
    assert evaluate(cj(last="2026-11-03T12:09:00Z"), grace=10)["behindSchedule"] is True


def test_missed_since_looks_only_at_the_window_while_behind_schedule_looks_at_everything():
    old = cj(last="2026-11-03T08:00:00Z", schedule_text="0 * * * *", created="2026-11-03T00:00:00Z")
    out = evaluate(old, since="5m")  # 09:00 .. 12:00 missed; 12:00 is 10 minutes old
    assert out["behindSchedule"] is True and out["missedSince"] is False
    assert evaluate(old, since="1h")["missedSince"] is True
    assert evaluate(old, since="1h")["missedFires"] == 4


def test_a_suspended_cronjob_expects_nothing():
    out = evaluate(cj(last="2026-11-03T08:00:00Z", suspend=True))
    assert out["lastExpectedFire"] == "2026-11-03T12:10:00Z"  # a fact about the schedule
    assert out["nextExpectedFire"] is None
    assert (out["missedFires"], out["missedSince"], out["behindSchedule"]) == (0, False, False)
    assert out["deadlineExceeded"] is None
    facts = schedule.schedule_facts(cj(suspend=True), out, [])
    assert facts["suspended"] is True and facts["forbidBlocked"] is False


def test_a_cronjob_that_never_ran_counts_from_its_creation():
    out = evaluate(cj(created="2026-11-03T12:05:30Z"))
    assert out["missedFires"] == 4  # 12:06 .. 12:09; 12:10 is inside the grace period
    assert out["behindSchedule"] is True


def test_a_schedule_that_has_not_fired_since_creation_has_no_expected_fire():
    out = evaluate(cj("0 3 * * *", created="2026-11-03T12:00:00Z"))
    assert out["lastExpectedFire"] is None
    assert out["nextExpectedFire"] == "2026-11-04T03:00:00Z"
    assert (out["missedFires"], out["behindSchedule"]) == (0, False)


def test_missed_fires_stop_counting_at_101():
    # the controller only warns (TooManyMissedTimes) past 100 and still starts the latest;
    # counting stops there so a long outage costs a bounded search
    out = evaluate(cj(last="2026-10-01T00:00:00Z", created="2026-09-01T00:00:00Z"))
    assert (out["missedFires"], out["missedFiresCapped"]) == (101, True)


def test_time_zone_decides_when_the_schedule_fires():
    # 03:00 in London is 02:00 UTC in summer time, 03:00 UTC in winter
    summer = evaluate(
        cj("0 3 * * *", created="2026-06-30T00:00:00Z", timeZone="Europe/London"),
        now=at("2026-07-01T12:00:00Z"),
    )
    assert summer["lastExpectedFire"] == "2026-07-01T02:00:00Z"
    assert summer["evaluatedTimeZone"] == "Europe/London"
    winter = evaluate(
        cj("0 3 * * *", created="2026-11-30T00:00:00Z", timeZone="Europe/London"),
        now=at("2026-12-01T12:00:00Z"),
    )
    assert winter["lastExpectedFire"] == "2026-12-01T03:00:00Z"


def test_starting_deadline_is_exceeded_only_when_the_latest_missed_slot_is_older_than_it():
    hourly = dict(
        schedule_text="0 * * * *", created="2026-11-03T00:00:00Z", last="2026-11-03T11:00:00Z"
    )
    # 12:00 is missed; at 12:03 it is 180s old, at 12:10:20 it is 620s old
    soon = evaluate(cj(startingDeadlineSeconds=300, **hourly), now=at("2026-11-03T12:03:00Z"))
    assert (soon["behindSchedule"], soon["deadlineExceeded"]) == (True, False)
    late = evaluate(cj(startingDeadlineSeconds=300, **hourly))
    assert (late["behindSchedule"], late["deadlineExceeded"]) == (True, True)
    assert evaluate(cj(**hourly))["deadlineExceeded"] is None  # no deadline set
    caught_up = dict(hourly, last="2026-11-03T12:00:00Z")
    assert evaluate(cj(startingDeadlineSeconds=300, **caught_up))["deadlineExceeded"] is False


def test_every_next_fire_and_window_chain_from_the_last_schedule_time():
    out = evaluate(
        cj("@every 1h", created="2026-11-03T00:00:00Z", last="2026-11-03T00:00:00Z"),
        now=at("2026-11-03T03:30:00Z"),
    )
    assert out["nextExpectedFire"] == "2026-11-03T04:00:00Z"
    assert out["lastExpectedFire"] == "2026-11-03T03:00:00Z"
    assert out["missedSince"] is True and out["missedFires"] == 3


def test_a_schedule_that_never_fires_is_flagged_not_hidden():
    out = evaluate(cj("0 0 31 2 *"))
    assert out["neverFires"] is True and out["nextExpectedFire"] is None
    assert (out["missedFires"], out["behindSchedule"]) == (0, False)
    assert (
        evaluate(cj("0 0 31 2 *", suspend=True))["neverFires"] is False
    )  # nothing is expected anyway
    assert evaluate(cj())["neverFires"] is False
    assert schedule.unevaluated()["neverFires"] is None


def test_the_starting_deadline_is_judged_against_itself_when_shorter_than_the_grace():
    hourly = dict(
        schedule_text="0 * * * *", created="2026-11-03T00:00:00Z", last="2026-11-03T11:00:00Z"
    )
    now = at("2026-11-03T12:00:45Z")  # the 12:00 slot is 45s old: inside the 60s grace
    out = evaluate(cj(startingDeadlineSeconds=30, **hourly), now=now)
    assert out["behindSchedule"] is False and out["deadlineExceeded"] is True
    assert evaluate(cj(startingDeadlineSeconds=300, **hourly), now=now)["deadlineExceeded"] is False
    # a slot already started is not a missed window
    done = dict(hourly, last="2026-11-03T12:00:00Z")
    assert evaluate(cj(startingDeadlineSeconds=30, **done), now=now)["deadlineExceeded"] is False


def test_every_schedules_run_from_the_last_schedule_time():
    out = evaluate(cj("@every 3m", last="2026-11-03T12:00:00Z"))
    assert out["lastExpectedFire"] == "2026-11-03T12:09:00Z"
    assert out["missedFires"] == 3  # 12:03, 12:06, 12:09


def test_an_unparseable_schedule_is_a_schedule_error_not_a_guess():
    with pytest.raises(schedule.ScheduleError) as exc:
        evaluate(cj("61 * * * *"))
    assert exc.value.code == "internal" and "61" in exc.value.message
    with pytest.raises(schedule.ScheduleError) as exc:
        evaluate(cj("* * * * *", timeZone="Mars/Olympus"))
    assert exc.value.code == "dependency"
    assert set(schedule.unevaluated()) == set(schedule.EVALUATED_FIELDS)
    assert set(schedule.unevaluated().values()) == {None}


def test_forbid_blocked_needs_forbid_an_active_job_and_missed_runs():
    active = [{"name": "j"}]
    behind = evaluate(cj(last="2026-11-03T12:00:00Z", concurrencyPolicy="Forbid"))
    assert (
        schedule.schedule_facts(cj(concurrencyPolicy="Forbid"), behind, active)["forbidBlocked"]
        is True
    )
    assert (
        schedule.schedule_facts(cj(concurrencyPolicy="Allow"), behind, active)["forbidBlocked"]
        is False
    )
    assert (
        schedule.schedule_facts(cj(concurrencyPolicy="Forbid"), behind, [])["forbidBlocked"]
        is False
    )
    fine = evaluate(cj(last="2026-11-03T12:10:00Z", concurrencyPolicy="Forbid"))
    assert (
        schedule.schedule_facts(cj(concurrencyPolicy="Forbid"), fine, active)["forbidBlocked"]
        is False
    )
    assert schedule.schedule_facts(cj(), schedule.unevaluated(), active)["forbidBlocked"] is None


def test_concurrency_policy_defaults_to_allow_and_success_age_is_a_fact():
    facts = schedule.schedule_facts(
        cj(last="2026-11-03T12:10:00Z", success="2026-11-03T12:05:03Z"), evaluate(cj()), []
    )
    assert facts["concurrencyPolicy"] == "Allow"
    assert facts["successOlderThanLastSchedule"] is True
    never = schedule.schedule_facts(cj(last="2026-11-03T12:10:00Z"), evaluate(cj()), [])
    assert never["successOlderThanLastSchedule"] is True and never["lastSuccessfulTime"] is None
    no_runs = schedule.schedule_facts(cj(), evaluate(cj()), [])
    assert no_runs["successOlderThanLastSchedule"] is None
    ok = schedule.schedule_facts(
        cj(last="2026-11-03T12:10:00Z", success="2026-11-03T12:10:05Z"), evaluate(cj()), []
    )
    assert ok["successOlderThanLastSchedule"] is False


def test_active_jobs_come_from_status_active_with_details_from_the_job_objects():
    cronjob = cj()
    cronjob["status"]["active"] = [{"name": "run-1"}, {"name": "gone"}]
    run = job("run-1", "2026-11-03T12:00:00Z", state="Running", active=1, failed=2)
    rows = {r["name"]: r for r in schedule.active_jobs(cronjob, [run], NOW)}
    assert rows["run-1"]["exists"] is True and rows["run-1"]["runningSeconds"] == 620
    assert (rows["run-1"]["activePods"], rows["run-1"]["failedPods"]) == (1, 2)
    assert rows["gone"]["exists"] is False and rows["gone"]["startTime"] is None
    assert (rows["run-1"]["suspended"], rows["gone"]["suspended"]) == (False, None)
    unknown = schedule.active_jobs(cronjob, None, NOW)
    assert [r["exists"] for r in unknown] == [None, None]  # Jobs not listed: not "gone"
    assert schedule.active_jobs(cj(), [], NOW) == []


def test_a_suspended_active_job_is_not_running():
    cronjob = cj()
    cronjob["status"]["active"] = [{"name": "held"}]
    held = job("held", "2026-11-03T12:00:00Z", state="Running")
    held["spec"]["suspend"] = True
    (row,) = schedule.active_jobs(cronjob, [held], NOW)
    assert row["suspended"] is True and row["runningSeconds"] is None
    assert row["startTime"] == "2026-11-03T12:00:00Z"
    entry = runs.run_entry(held, [], NOW)
    assert entry["status"] == "active" and entry["suspended"] is True
    assert entry["runningSeconds"] is None


def test_events_in_window_without_the_routine_per_job_ones():
    def ev(reason, last, etype="Normal", **extra):
        return {
            "type": etype,
            "reason": reason,
            "message": f"{reason}!",
            "lastTimestamp": last,
            "firstTimestamp": last,
            "count": 2,
            **extra,
        }

    events = [
        ev("SuccessfulCreate", "2026-11-03T12:10:00Z"),
        ev("SawCompletedJob", "2026-11-03T12:10:00Z"),
        ev("SuccessfulDelete", "2026-11-03T12:10:00Z"),
        ev("JobAlreadyActive", "2026-11-03T12:10:00Z"),
        ev("MissSchedule", "2026-11-03T12:05:00Z", "Warning"),
        ev("MissSchedule", "2026-11-03T09:00:00Z", "Warning"),  # outside the window
        {
            "type": "Warning",
            "reason": "NewStyle",
            "message": "m",
            "eventTime": "2026-11-03T12:09:00.000000Z",
            "series": {"count": 7, "lastObservedTime": "2026-11-03T12:09:30.000000Z"},
            "metadata": {},
        },
    ]
    rows = schedule.cronjob_events(events, "1h", NOW)
    assert [(r["type"], r["reason"]) for r in rows] == [
        ("Normal", "JobAlreadyActive"),
        ("Warning", "NewStyle"),
        ("Warning", "MissSchedule"),
    ]
    assert rows[1]["count"] == 7
    long = schedule.cronjob_events(
        [ev("MissSchedule", "2026-11-03T12:05:00Z", "Warning", message="x" * 3000)], "1h", NOW
    )
    assert len(long[0]["message"]) == 1024 and long[0]["truncated"] is True


# --------------------------------------------------------------- runs ----


def test_jobs_are_owned_by_uid_not_by_name():
    cronjob = cj()
    mine = job("mine", "2026-11-03T12:00:00Z")
    old_incarnation = job("theirs", "2026-11-03T12:00:00Z", owner="another-uid")
    bare = copy.deepcopy(mine)
    bare["metadata"]["name"] = "bare"
    del bare["metadata"]["ownerReferences"]
    assert [
        j["metadata"]["name"] for j in runs.owned_jobs([mine, old_incarnation, bare], cronjob)
    ] == ["mine"]


def test_run_status_comes_from_the_terminal_conditions():
    assert runs.job_status(job("a", "2026-11-03T12:00:00Z", "Complete")) == "succeeded"
    assert runs.job_status(job("b", "2026-11-03T12:00:00Z", "Failed")) == "failed"
    assert runs.job_status(job("c", "2026-11-03T12:00:00Z", "Running", active=1)) == "active"
    pending = job("d", "2026-11-03T12:00:00Z", "Running")
    pending["spec"]["suspend"] = True
    assert runs.job_status(pending) == "active"
    assert runs.run_entry(pending, [], NOW)["suspended"] is True


def test_a_finished_run_has_a_duration_an_active_one_a_running_time():
    done = job(
        "done", "2026-11-03T12:00:00Z", "Complete", finished="2026-11-03T12:00:05Z", succeeded=1
    )
    entry = runs.run_entry(done, [], NOW)
    assert (entry["status"], entry["durationSeconds"], entry["runningSeconds"]) == (
        "succeeded",
        5,
        None,
    )
    assert entry["finishedAt"] == "2026-11-03T12:00:05Z" and entry["succeededPods"] == 1
    active = runs.run_entry(job("a", "2026-11-03T12:05:20Z", "Running", active=1), [], NOW)
    assert (active["durationSeconds"], active["runningSeconds"], active["finishedAt"]) == (
        None,
        300,
        None,
    )


def test_a_failed_run_ends_at_the_failed_condition_and_carries_its_reason():
    failed = job(
        "f",
        "2026-11-03T12:00:00Z",
        "Failed",
        finished="2026-11-03T12:01:10Z",
        reason="BackoffLimitExceeded",
        message="Job has reached the specified backoff limit",
        failed=1,
    )
    entry = runs.run_entry(failed, [], NOW)
    assert entry["status"] == "failed" and entry["completionTime"] is None
    assert entry["durationSeconds"] == 70
    assert (entry["failureReason"], entry["failedPods"]) == ("BackoffLimitExceeded", 1)
    long = job("g", "2026-11-03T12:00:00Z", "Failed", reason="DeadlineExceeded", message="m" * 2000)
    entry = runs.run_entry(long, [], NOW)
    assert len(entry["failureMessage"]) == 1024 and entry["truncated"] is True


def test_pods_say_how_each_container_ended_and_never_carry_the_termination_message():
    j = job("f", "2026-11-03T12:00:00Z", "Failed", failed=1)
    p = pod("f-abc", j, [terminated("job", "Error", 3)])
    other = pod("x-abc", job("x", "2026-11-03T12:00:00Z"), [terminated("job", "Error", 1)])
    entry = runs.run_entry(j, [p, other], NOW)
    (row,) = entry["pods"]
    assert row["name"] == "f-abc" and row["phase"] == "Failed"
    assert row["containers"][0]["lastTermination"] == {
        "reason": "Error",
        "exitCode": 3,
        "signal": None,
        "finishedAt": "2026-11-03T12:00:40Z",
    }
    assert "SECRET-ish" not in json.dumps(entry)


def test_a_restarted_container_reports_its_previous_termination_and_init_containers_count():
    j = job("f", "2026-11-03T12:00:00Z", "Failed", failed=1)
    p = pod(
        "f-abc",
        j,
        containers=[terminated("job", "OOMKilled", 137, last=True, restarts=2)],
        init=[terminated("setup", "Error", 1)],
    )
    containers = runs.run_entry(j, [p], NOW)["pods"][0]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert by_name["job"]["init"] is False and by_name["job"]["state"] == "running"
    assert (
        by_name["job"]["lastTermination"]["reason"] == "OOMKilled"
        and by_name["job"]["restartCount"] == 2
    )
    assert by_name["setup"]["init"] is True and by_name["setup"]["lastTermination"]["exitCode"] == 1


def test_a_waiting_container_reports_why():
    j = job("f", "2026-11-03T12:00:00Z", "Running", active=1)
    p = pod(
        "f-abc",
        j,
        [{"name": "job", "restartCount": 0, "state": {"waiting": {"reason": "ImagePullBackOff"}}}],
        phase="Pending",
    )
    c = runs.run_entry(j, [p], NOW)["pods"][0]["containers"][0]
    assert (c["state"], c["waitingReason"], c["lastTermination"]) == (
        "waiting",
        "ImagePullBackOff",
        None,
    )


def test_pods_are_null_when_they_could_not_be_listed_and_empty_when_there_are_none():
    j = job("f", "2026-11-03T12:00:00Z")
    assert runs.run_entry(j, None, NOW)["pods"] is None
    assert runs.run_entry(j, [], NOW)["pods"] == []


def test_nested_pod_and_container_lists_stop_at_fifty():
    j = job("f", "2026-11-03T12:00:00Z", "Failed")
    many = [pod(f"p{i:03d}", j, [terminated("job", "Error", 1)]) for i in range(60)]
    entry = runs.run_entry(j, many, NOW)
    assert len(entry["pods"]) == 50 and entry["truncated"] is True
    wide = pod("w", j, [terminated(f"c{i}", "Error", 1) for i in range(60)])
    entry = runs.run_entry(j, [wide], NOW)
    assert (
        len(entry["pods"][0]["containers"]) == 50
        and entry["pods"][0]["containersTruncated"] is True
    )


def test_recent_runs_are_the_jobs_started_in_the_window_newest_first_with_totals_for_all():
    cronjob = cj()
    jobs = [
        job("old-ok", "2026-11-03T08:00:00Z", "Complete", finished="2026-11-03T08:00:05Z"),
        job("stuck", "2026-11-03T08:01:00Z", "Running", active=1),
        job("ok", "2026-11-03T12:08:00Z", "Complete", finished="2026-11-03T12:08:04Z"),
        job("bad", "2026-11-03T12:09:00Z", "Failed", reason="BackoffLimitExceeded"),
        job("other", "2026-11-03T12:09:30Z", owner="another-uid"),
    ]
    rows, counts = runs.recent_runs(cronjob, jobs, [], "1h", NOW)
    assert [r["name"] for r in rows] == ["bad", "ok"]
    assert counts == {
        "owned": 4,
        "inWindow": 2,
        "succeeded": 1,
        "failed": 1,
        "active": 0,
        "activeBeforeWindow": 1,
        "latestStatus": "failed",
        "latestStartTime": "2026-11-03T12:09:00Z",
    }
    rows, counts = runs.recent_runs(cronjob, jobs, [], "1m", NOW)
    assert rows == [] and counts["owned"] == 4 and counts["inWindow"] == 0
    assert counts["latestStatus"] == "failed"  # the empty window is not "never ran"


def test_a_cronjob_with_no_jobs_has_no_latest_status():
    rows, counts = runs.recent_runs(cj(), [], [], "1h", NOW)
    assert rows == [] and counts["owned"] == 0 and counts["latestStatus"] is None
    assert counts["latestStartTime"] is None


def test_a_job_without_a_start_time_counts_from_its_creation():
    pending = job("p", "2026-11-03T12:09:00Z", "Running")
    del pending["status"]["startTime"]
    rows, _ = runs.recent_runs(cj(), [pending], [], "1h", NOW)
    assert rows[0]["startTime"] == "2026-11-03T12:09:00Z" and rows[0]["status"] == "active"


# -------------------------------------------------------------- k8s.py ----


@pytest.mark.parametrize(
    ("rc", "stderr", "code"),
    [
        (
            1,
            'Error from server (Forbidden): jobs.batch is forbidden: User "u" cannot list resource "jobs" in API group "batch" in the namespace "x"',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): cronjobs.batch "gone" not found', "not_found"),
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
        (
            1,
            'Error from server (Forbidden): jobs.batch is forbidden: User "u" cannot list resource "jobs" '
            '... clusterrole.rbac.authorization.k8s.io "view" not found',
            "permission_denied",
        ),
    ],
)
def test_kubectl_failures_are_classified(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class ReturningCtx:
    """Like the classic contract's ctx.run: returns the finished process on a non-zero exit."""

    def __init__(self, done):
        self.done = done

    def run(self, argv):
        return self.done


def test_a_non_zero_exit_that_ctx_run_returned_still_raises_with_its_code():
    forbidden = Done(1, "", 'Error from server (Forbidden): cannot list resource "jobs"')
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ReturningCtx(forbidden), "get", "jobs.batch")
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ReturningCtx(Done(0, "not json")), "get", "jobs.batch")
    assert exc.value.code == "internal"
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ReturningCtx(Done(1, "", "x" * 5000)), "get", "jobs.batch")
    assert len(exc.value.message) <= 1024


def test_a_subprocess_timeout_is_a_timeout_error():
    import subprocess

    class Slow:
        def run(self, argv):
            raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Slow(), "get", "jobs.batch")
    assert exc.value.code == "timeout"


# ------------------------------------------------------------ scripts ----
# The scripts run against the captured rw-cron namespace through the SDK's own test
# context (ctx.run raises on a non-zero exit like the real runner; `stub_commands`
# matches the exact kubectl argv, so an unexpected call fails the test).

CAPTURED = at("2026-11-03T19:55:15Z")  # just after the data was captured


def load(kind):
    return json.loads((DATA / f"{kind}.json").read_text())["items"]


def named(items, name):
    return next(i for i in items if i["metadata"]["name"] == name)


def script(name):
    spec = importlib.util.spec_from_file_location(f"cj_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


FAILURES = {
    "permission_denied": 'Error from server (Forbidden): {r} is forbidden: User "u" cannot list resource "{r}" in API group "" in the namespace "rw-cron"',
    "not_found": 'Error from server (NotFound): {r} "x" not found',
    "unreachable": "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
    "timeout": "error: context deadline exceeded",
}

CALLS = {  # key -> (kubectl noun words, resource name used in the error text)
    "cronjob": "cronjobs.batch",
    "jobs": "jobs.batch",
    "pods": "pods",
    "events": "events",
}


def argvs(name):
    uid = named(load("cronjobs"), name)["metadata"]["uid"] if name != "nope" else "x"
    base = ("kubectl", "-n", NS, "get")
    return {
        "cronjob": (*base, "cronjobs.batch", name, "-o", "json"),
        "jobs": (*base, "jobs.batch", "-o", "json"),
        "pods": (*base, "pods", "-l", "job-name", "-o", "json"),
        "events": (*base, "events", "--field-selector", f"involvedObject.uid={uid}", "-o", "json"),
    }


def stubs(name, fail=None, code="permission_denied"):
    """kubectl answers for the CronJob `name`; `fail` is a key of CALLS whose call exits 1."""
    keys = argvs(name)
    out = {}
    docs = {
        "cronjob": None if name == "nope" else named(load("cronjobs"), name),
        "jobs": {"items": load("jobs")},
        "pods": {"items": load("pods")},
        "events": {"items": load("events")},
    }
    uid = None if name == "nope" else docs["cronjob"]["metadata"]["uid"]
    docs["events"] = {"items": [e for e in load("events") if e["involvedObject"]["uid"] == uid]}
    for key, argv in keys.items():
        if name == "nope" and key == "cronjob":
            out[argv] = (1, "", FAILURES["not_found"].format(r="cronjobs.batch"))
        elif key == fail:
            out[argv] = (1, "", FAILURES[code].format(r=CALLS[key]))
        else:
            out[argv] = (0, json.dumps(docs[key]), "")
    return out


def resource(name, rtype="kubernetes/cronjob", with_path=True):
    res = {"cluster": "kind", "namespace": NS, "name": name}
    if rtype:
        res["type"] = rtype
    if with_path:
        res["path"] = f"kubernetes/clusters/kind/namespaces/{NS}/cronjobs/{name}"
    return res


SETTINGS = {"maxItems": 200, "lateGraceSeconds": 60}


def run(
    task, name, fail=None, code="permission_denied", now=CAPTURED, settings=None, res=None, **inputs
):
    """(envelope, calls) for a task run on cronjob `name`."""
    with make_context(resource=res or resource(name), settings=settings or SETTINGS, now=now) as tc:
        calls = stub_commands(tc, stubs(name, fail, code))
        env = run_main(script(task), tc, **inputs)
    return env, calls


def schema_for(task, output):
    spec = next(t for t in TASKS if t["name"] == task)["outputs"][output]["schema"]
    return json.loads((SKILL / spec).read_text())


TASKS = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
TASK_SCRIPTS = {"schedule-health": "schedule_health", "recent-runs": "recent_runs"}
CRONJOBS = ["ok-every-minute", "always-fails", "paused", "forbid-long", "tz-daily"]


@pytest.mark.parametrize("name", CRONJOBS)
@pytest.mark.parametrize("task", TASK_SCRIPTS)
def test_every_output_of_every_scenario_cronjob_satisfies_its_schema(task, name):
    import jsonschema

    env, _ = run(TASK_SCRIPTS[task], name)
    assert env["status"] == "ok", env.get("errors")
    declared = next(t for t in TASKS if t["name"] == task)["outputs"]
    assert set(env["outputs"]) == set(declared)
    for output, value in env["outputs"].items():
        schema = schema_for(task, output)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(value, schema)


def test_schedule_health_of_the_cronjob_that_succeeds_every_minute():
    env, calls = run("schedule_health", "ok-every-minute")
    out = env["outputs"]
    s = out["schedule"]
    assert (s["suspended"], s["schedule"], s["timeZone"], s["concurrencyPolicy"]) == (
        False,
        "* * * * *",
        None,
        "Allow",
    )
    assert (s["lastScheduleTime"], s["lastSuccessfulTime"]) == (
        "2026-11-03T19:55:00Z",
        "2026-11-03T19:55:04Z",
    )
    assert (s["lastExpectedFire"], s["nextExpectedFire"]) == (
        "2026-11-03T19:55:00Z",
        "2026-11-03T19:56:00Z",
    )
    assert (s["missedFires"], s["missedSince"], s["behindSchedule"]) == (0, False, False)
    assert (s["activeCount"], s["forbidBlocked"], s["successOlderThanLastSchedule"]) == (
        0,
        False,
        False,
    )
    assert out["activeJobs"] == [] and out["events"] == []  # only the routine per-Job events exist
    assert len(calls) == 2  # no active Job: the Job list is not read


def test_schedule_health_of_a_forbid_cronjob_held_back_by_a_running_job():
    env, _ = run("schedule_health", "forbid-long")
    out = env["outputs"]
    s = out["schedule"]
    assert (s["concurrencyPolicy"], s["startingDeadlineSeconds"], s["activeCount"]) == (
        "Forbid",
        90,
        1,
    )
    assert s["lastScheduleTime"] == "2026-11-03T19:51:00Z" and s["lastSuccessfulTime"] is None
    assert s["missedFires"] == 3  # 19:52, 19:53, 19:54; 19:55 is inside the grace period
    assert (s["behindSchedule"], s["missedSince"], s["forbidBlocked"]) == (True, True, True)
    assert s["deadlineExceeded"] is False  # the 19:54 slot is 75s old, the deadline is 90s
    assert s["successOlderThanLastSchedule"] is True
    (active,) = out["activeJobs"]
    assert active["name"] == "forbid-long-29895591" and active["exists"] is True
    assert (active["runningSeconds"], active["activePods"]) == (255, 1)
    (event,) = out["events"]
    assert (event["type"], event["reason"], event["count"]) == ("Normal", "JobAlreadyActive", 4)
    assert "Forbid" in event["message"]


def test_the_starting_deadline_passes_for_the_slot_the_forbid_cronjob_skipped():
    env, _ = run("schedule_health", "forbid-long", now=at("2026-11-03T19:55:50Z"))
    assert env["outputs"]["schedule"]["deadlineExceeded"] is True  # 19:54 is 110s old, deadline 90s


def test_schedule_health_of_a_suspended_cronjob():
    env, _ = run("schedule_health", "paused")
    s = env["outputs"]["schedule"]
    assert s["suspended"] is True and s["lastScheduleTime"] is None
    assert (s["missedFires"], s["behindSchedule"], s["nextExpectedFire"]) == (0, False, None)
    assert s["lastExpectedFire"] == "2026-11-03T19:55:00Z"


def test_schedule_health_of_a_daily_cronjob_in_a_time_zone_that_has_not_fired_yet():
    env, _ = run("schedule_health", "tz-daily")
    s = env["outputs"]["schedule"]
    assert (s["timeZone"], s["evaluatedTimeZone"]) == ("Europe/London", "Europe/London")
    assert s["lastExpectedFire"] is None and s["nextExpectedFire"] == "2026-11-04T03:00:00Z"
    assert s["behindSchedule"] is False and s["lastScheduleTime"] is None


def test_recent_runs_of_the_cronjob_that_succeeds_every_minute():
    env, _ = run("recent_runs", "ok-every-minute")
    out = env["outputs"]
    assert [r["name"] for r in out["runs"]] == [
        "ok-every-minute-29895595",
        "ok-every-minute-29895594",
        "ok-every-minute-29895593",
    ]
    newest = out["runs"][0]
    assert (
        newest["status"],
        newest["startTime"],
        newest["finishedAt"],
        newest["durationSeconds"],
    ) == ("succeeded", "2026-11-03T19:55:00Z", "2026-11-03T19:55:04Z", 4)
    assert newest["scheduledTime"] == "2026-11-03T19:55:00Z" and newest["failureReason"] is None
    (pod,) = newest["pods"]
    assert pod["phase"] == "Succeeded"
    assert pod["containers"][0]["lastTermination"]["reason"] == "Completed"
    assert pod["containers"][0]["lastTermination"]["exitCode"] == 0
    assert out["counts"] == {
        "owned": 3,
        "inWindow": 3,
        "succeeded": 3,
        "failed": 0,
        "active": 0,
        "activeBeforeWindow": 0,
        "latestStatus": "succeeded",
        "latestStartTime": "2026-11-03T19:55:00Z",
    }


def test_recent_runs_of_the_cronjob_that_always_fails_say_why():
    env, _ = run("recent_runs", "always-fails")
    out = env["outputs"]
    assert out["counts"]["failed"] == 3 and out["counts"]["latestStatus"] == "failed"
    newest = out["runs"][0]
    assert newest["status"] == "failed" and newest["completionTime"] is None
    assert newest["failureReason"] == "BackoffLimitExceeded"
    assert "backoff limit" in newest["failureMessage"]
    assert newest["failedPods"] == 1 and newest["finishedAt"] is not None
    term = newest["pods"][0]["containers"][0]["lastTermination"]
    assert (term["reason"], term["exitCode"]) == ("Error", 3)


def test_recent_runs_of_the_forbid_cronjob_show_the_running_job():
    env, _ = run("recent_runs", "forbid-long")
    (run_,) = env["outputs"]["runs"]
    assert (run_["status"], run_["runningSeconds"], run_["finishedAt"]) == ("active", 255, None)
    assert run_["pods"][0]["containers"][0]["state"] == "running"
    assert run_["pods"][0]["containers"][0]["lastTermination"] is None
    assert env["outputs"]["counts"]["active"] == 1


def test_a_suspended_cronjob_that_never_ran_has_no_runs_at_all():
    env, calls = run("recent_runs", "paused")
    assert env["outputs"]["runs"] == []
    assert (
        env["outputs"]["counts"]["owned"] == 0 and env["outputs"]["counts"]["latestStatus"] is None
    )
    assert len(calls) == 2  # no owned Job: the pods are not listed


def test_a_window_that_misses_every_job_still_reports_what_the_cronjob_owns():
    env, _ = run("recent_runs", "always-fails", now=at("2026-11-03T21:00:00Z"), since="1h")
    out = env["outputs"]
    assert out["runs"] == [] and out["counts"]["owned"] == 3 and out["counts"]["inWindow"] == 0
    assert out["counts"]["latestStatus"] == "failed"


def test_max_items_caps_the_lists_but_counts_keep_the_totals():
    env, _ = run("recent_runs", "ok-every-minute", settings={"maxItems": 1, "lateGraceSeconds": 60})
    assert len(env["outputs"]["runs"]) == 1 and env["outputs"]["counts"]["inWindow"] == 3


def test_the_target_type_falls_back_to_the_path():
    env, _ = run("schedule_health", "paused", res=resource("paused", rtype=None))
    assert env["status"] == "ok"
    assert (
        fetch.target_type(
            type(
                "C",
                (),
                {
                    "resource": type(
                        "R", (), {"path": "kubernetes/clusters/k/namespaces/n/cronjobs/c"}
                    )()
                },
            )()
        )
        == "kubernetes/cronjob"
    )
    assert (
        fetch.target_type(
            type(
                "C",
                (),
                {
                    "resource": type(
                        "R", (), {"path": "kubernetes/clusters/k/namespaces/n/pods/p"}
                    )()
                },
            )()
        )
        is None
    )


# ------------------------------------------------------------- errors ----


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


@pytest.mark.parametrize("code", sorted(FAILURES))
@pytest.mark.parametrize("task", TASK_SCRIPTS.values())
def test_a_failed_cronjob_read_fails_the_task_with_its_code_and_returns_nothing(task, code):
    env, _ = run(task, "forbid-long", fail="cronjob", code=code)
    assert env["status"] == "failed" and env["outputs"] == {}
    assert [c for c, _ in codes(env)] == [code]


@pytest.mark.parametrize("task", TASK_SCRIPTS.values())
def test_a_missing_cronjob_is_not_found_not_an_empty_result(task):
    env, _ = run(task, "nope")
    assert env["status"] == "failed" and [c for c, _ in codes(env)] == ["not_found"]


@pytest.mark.parametrize("code", sorted(FAILURES))
def test_a_failed_job_list_fails_recent_runs_it_is_the_primary_read(code):
    env, _ = run("recent_runs", "always-fails", fail="jobs", code=code)
    assert env["status"] == "failed" and [c for c, _ in codes(env)] == [code]
    assert env["outputs"] == {}


def test_a_forbidden_pod_list_keeps_the_runs_and_nulls_their_pods():
    env, _ = run("recent_runs", "always-fails", fail="pods")
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "pods")]
    runs_ = env["outputs"]["runs"]
    assert len(runs_) == 3 and all(r["pods"] is None for r in runs_)
    assert runs_[0]["failureReason"] == "BackoffLimitExceeded"  # the Job's own facts stay


def test_a_forbidden_event_list_keeps_the_schedule_and_nulls_the_events():
    env, _ = run("schedule_health", "forbid-long", fail="events")
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "events")]
    assert env["outputs"]["events"] is None  # not read, which is not "no events"
    assert env["outputs"]["schedule"]["forbidBlocked"] is True


def test_a_forbidden_job_list_keeps_the_active_job_names_and_nulls_their_details():
    env, _ = run("schedule_health", "forbid-long", fail="jobs")
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "jobs.batch")]
    (active,) = env["outputs"]["activeJobs"]
    assert active["name"] == "forbid-long-29895591"
    assert (active["exists"], active["startTime"], active["runningSeconds"]) == (None, None, None)
    assert env["outputs"]["schedule"]["activeCount"] == 1


def test_a_job_list_is_not_needed_and_not_read_while_nothing_is_active():
    env, calls = run("schedule_health", "ok-every-minute", fail="jobs")
    assert env["status"] == "ok" and calls == [
        argvs("ok-every-minute")[k] for k in ("cronjob", "events")
    ]


def test_an_unreachable_cluster_fails_even_the_secondary_reads_it_never_gets_to():
    env, _ = run("schedule_health", "forbid-long", fail="cronjob", code="unreachable")
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


def test_a_schedule_that_cannot_be_evaluated_is_an_error_with_the_other_facts_kept(monkeypatch):
    broken = copy.deepcopy(named(load("cronjobs"), "paused"))
    broken["spec"]["schedule"] = "61 * * * *"
    stub = stubs("paused")
    stub[argvs("paused")["cronjob"]] = (0, json.dumps(broken), "")
    with make_context(resource=resource("paused"), settings=SETTINGS, now=CAPTURED) as tc:
        stub_commands(tc, stub)
        env = run_main(script("schedule_health"), tc)
    assert env["status"] == "partial" and codes(env) == [("internal", "schedule")]
    s = env["outputs"]["schedule"]
    assert s["suspended"] is True and s["schedule"] == "61 * * * *"
    assert (s["lastExpectedFire"], s["behindSchedule"], s["missedFires"]) == (None, None, None)


def test_a_schedule_that_never_fires_is_a_scoped_error_with_the_facts_kept():
    never = copy.deepcopy(named(load("cronjobs"), "ok-every-minute"))
    never["spec"]["schedule"] = "0 0 31 2 *"
    stub = stubs("ok-every-minute")
    stub[argvs("ok-every-minute")["cronjob"]] = (0, json.dumps(never), "")
    with make_context(resource=resource("ok-every-minute"), settings=SETTINGS, now=CAPTURED) as tc:
        stub_commands(tc, stub)
        env = run_main(script("schedule_health"), tc)
    assert env["status"] == "partial" and codes(env) == [("invalid_input", "schedule")]
    s = env["outputs"]["schedule"]
    assert s["neverFires"] is True and s["nextExpectedFire"] is None and s["suspended"] is False


@pytest.mark.parametrize("task", TASK_SCRIPTS.values())
def test_other_target_types_are_invalid_input_before_any_kubectl_call(task):
    env, calls = run(task, "paused", res=resource("paused", rtype="kubernetes/deployment"))
    assert env["status"] == "failed" and [c for c, _ in codes(env)] == ["invalid_input"]
    assert calls == []


@pytest.mark.parametrize("task", TASK_SCRIPTS.values())
@pytest.mark.parametrize("bad", ["yesterday", "10", "", "1h junk"])
def test_a_bad_since_is_invalid_input_before_any_kubectl_call(task, bad):
    env, calls = run(task, "paused", since=bad)
    assert env["status"] == "failed" and [c for c, _ in codes(env)] == ["invalid_input"]
    assert calls == []


@pytest.mark.parametrize(
    "bad", [{"maxItems": 0, "lateGraceSeconds": 60}, {"maxItems": 5, "lateGraceSeconds": -1}]
)
def test_out_of_range_settings_are_invalid_input_before_any_kubectl_call(bad):
    env, calls = run("schedule_health", "paused", settings=bad)
    assert env["status"] == "failed" and [c for c, _ in codes(env)] == ["invalid_input"]
    assert calls == []


def test_since_accepts_milliseconds_and_combinations():
    for since in ("500ms", "1h30m", "2d"):
        assert run("recent_runs", "paused", since=since)[0]["status"] == "ok"


def test_an_error_message_is_bounded():
    stub = stubs("always-fails")
    stub[argvs("always-fails")["pods"]] = (1, "", "Error from server (Forbidden): " + "x" * 5000)
    with make_context(resource=resource("always-fails"), settings=SETTINGS, now=CAPTURED) as tc:
        stub_commands(tc, stub)
        env = run_main(script("recent_runs"), tc)
    assert env["status"] == "partial" and len(env["errors"][0]["message"]) <= 1024


# ---------------------------------------------------------- permissions ----

# kubectl noun (as the scripts write it) -> (apiGroup, plural). Extend it when you copy this.
RESOURCES = {
    "cronjobs.batch": ("batch", "cronjobs"),
    "jobs.batch": ("batch", "jobs"),
    "pods": ("", "pods"),
    "events": ("", "events"),
}
VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-o",
    "-c",
    "--container",
    "--field-selector",
    "-l",
    "--selector",
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list.
    Raises on anything else, so a script that issues a new verb has to extend this.
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
    named_object = len(positional) > 2 and not (
        {"-l", "--selector", "--field-selector"} & set(flags)
    )
    return {(group, plural, "get" if named_object else "list")}


def granted(task):
    return {
        (g, r, v)
        for rule in next(t for t in TASKS if t["name"] == task)["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


@pytest.mark.parametrize("task", TASK_SCRIPTS)
def test_declared_permissions_are_exactly_what_the_scripts_call(task):
    needed = set()
    for name in CRONJOBS:
        for fail in (None, "jobs", "pods", "events"):
            env, calls = run(TASK_SCRIPTS[task], name, fail=fail)
            for argv in calls:
                needed |= rbac_needs(list(argv))
    assert needed == granted(task), (
        f"{task}: calls {sorted(needed)}, declares {sorted(granted(task))}"
    )


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "jobs.batch", "-o", "json"]) == {
        ("batch", "jobs", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "cronjobs.batch", "c", "-o", "json"]) == {
        ("batch", "cronjobs", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "job-name", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "events", "--field-selector", "a=b", "-o", "json"]
    ) == {("", "events", "list")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "-n", "x", "delete", "jobs.batch", "j"])


def test_both_tasks_only_read():
    assert all(t["readOnly"] is True for t in TASKS)
    assert all(v in ("get", "list") for t in TASK_SCRIPTS for (_, _, v) in granted(t))


def test_the_reference_time_comes_from_ctx_now_and_the_wall_clock_is_only_a_fallback():
    env, _ = run("schedule_health", "paused", now=at("2026-11-03T19:55:15Z"))
    assert env["outputs"]["schedule"]["lastExpectedFire"] == "2026-11-03T19:55:00Z"
    env, _ = run("schedule_health", "paused", now=at("2026-11-03T21:02:00Z"))
    assert env["outputs"]["schedule"]["lastExpectedFire"] == "2026-11-03T21:00:00Z"

    class NoNow:  # a stand-in context from before ctx.now existed
        def __init__(self, ctx):
            self._ctx = ctx

        def __getattr__(self, name):
            if name == "now":
                raise AttributeError(name)
            return getattr(self._ctx, name)

    with make_context(resource=resource("paused"), settings=SETTINGS) as tc:
        stub_commands(tc, stubs("paused"))
        with tc.environ():
            out = script("schedule_health")(NoNow(tc.ctx))
    assert out["schedule"]["suspended"] is True
