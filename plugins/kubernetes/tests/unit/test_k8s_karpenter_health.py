"""Unit tests for k8s-karpenter-health's lib/ and scripts against JSON captured from the
kind-rw-plugins scenario (plugins/kubernetes/tests/kind, namespace rw-karpenter).

The CRDs there are synthetic and no Karpenter runs, so the status of each NodePool, NodeClaim
and EC2NodeClass was written by 30-k8s-karpenter-health.sh; the pods are real and the scheduler
wrote their PodScheduled condition and FailedScheduling events. The state was captured at one
moment; `NOW` is a fixed point half an hour after it (so the stuck NodeClaim is past the default
15 minutes and the events are inside the default 1h window)."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from k8s_karpenter_health import k8s
from k8s_karpenter_health import nodeclaims as nc
from k8s_karpenter_health import nodepool as np_
from k8s_karpenter_health import pending as pp
from k8s_karpenter_health.common import trunc
from k8s_karpenter_health.errors import RwError
from runwhen_capability.rwerrors import RwCommandError, classify_command_error

SKILL = Path(__file__).parents[2] / "skills" / "k8s-karpenter-health"
DATA = Path(__file__).parent / "data" / "k8s_karpenter_health"
NOW = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
SCRIPT_OF = {
    "nodepool-status": "nodepool_status",
    "nodeclaims": "nodeclaims",
    "pending-pods": "pending_pods",
}
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
TYPE = "kubernetes/nodepool"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def pool(name):
    return named(load("nodepools"), name)


def claim(name):
    return named(load("nodeclaims"), name)


def pod(name):
    return named(load("pending-pods"), name)


def schema_of(task, output):
    ref = TASKS[task]["outputs"][output]["schema"]
    return json.loads((SKILL / ref).read_text())


def check_outputs(task, out):
    for name, value in out.items():
        jsonschema.validate(value, schema_of(task, name))
    assert set(out) == set(TASKS[task]["outputs"])


def with_status(obj, conditions):
    obj = copy.deepcopy(obj)
    obj["status"] = {"conditions": conditions}
    return obj


def cond(ctype, status="True", reason=None, message=None, since=None):
    return {
        "type": ctype,
        "status": status,
        "reason": reason,
        "message": message,
        "lastTransitionTime": since,
    }


# ------------------------------------------------------------ helpers ----


def test_trunc_flags_text_cut_at_1024():
    assert trunc("short") == ("short", False)
    text, cut = trunc("y" * 3000)
    assert len(text) == 1024 and cut is True


# ------------------------------------------------------- nodepool-status ----


def test_healthy_pool_reports_conditions_policy_and_class_reference():
    v = np_.pool_view(pool("healthy-pool"))
    assert (v["ready"]["status"], v["ready"]["reason"]) == ("True", "Ready")
    assert v["notTrueConditions"] == []
    assert [c["type"] for c in v["conditions"]] == [
        "Ready",
        "ValidationSucceeded",
        "NodeClassReady",
    ]
    assert v["weight"] == 10 and v["consolidationPolicy"] == "WhenEmptyOrUnderutilized"
    assert v["consolidateAfter"] == "1m" and v["expireAfter"] == "720h"
    assert v["budgets"] == [
        {
            "nodes": "10%",
            "reasons": [],
            "schedule": None,
            "duration": None,
            "allowsNone": False,
            "allowsNoneDuringSchedule": False,
        }
    ]
    assert v["nodeClassRef"] == {
        "group": "karpenter.k8s.aws",
        "kind": "EC2NodeClass",
        "name": "default",
    }


def test_not_ready_pool_names_the_conditions_that_are_not_true():
    v = np_.pool_view(pool("broken-pool"))
    assert (v["ready"]["status"], v["ready"]["reason"]) == ("False", "NodeClassNotReady")
    assert v["ready"]["message"] == "NodeClassRef not ready"
    assert v["notTrueConditions"] == ["Ready", "NodeClassReady"]


def test_pool_with_no_status_is_unknown_not_ready():
    bare = {"metadata": {"name": "x", "generation": 1}, "spec": {}}
    v = np_.pool_view(bare)
    assert v["ready"] is None and v["conditions"] == [] and v["notTrueConditions"] == []
    assert v["nodeClassRef"] is None and v["budgets"] == [] and v["weight"] is None


def test_a_zero_budget_allows_no_disruption_and_reasons_are_kept():
    budgets = np_.pool_view(pool("full-pool"))["budgets"]
    assert [(b["nodes"], b["allowsNone"]) for b in budgets] == [("0", True), ("20%", False)]
    assert budgets[0]["reasons"] == ["Underutilized"]
    odd = np_.budget_view({"nodes": "0%", "schedule": "0 9 * * *", "duration": "8h"})
    # a zero budget that only applies on a schedule blocks disruption only inside the window
    assert odd["allowsNone"] is False and odd["allowsNoneDuringSchedule"] is True
    assert odd["schedule"] == "0 9 * * *"
    assert np_.budget_view({"nodes": "0"})["allowsNoneDuringSchedule"] is False
    assert np_.budget_view({"nodes": "ten"})["allowsNone"] is False
    assert np_.budget_view({})["nodes"] is None


def test_pool_without_budgets_reports_none_so_the_default_applies():
    v = np_.pool_view(pool("orphan-pool"))
    assert v["budgets"] == [] and v["budgetCount"] == 0


def test_v1beta1_pool_keeps_expire_after_under_disruption_and_class_api_version():
    old = {
        "metadata": {"name": "o"},
        "spec": {
            "disruption": {"expireAfter": "168h", "consolidationPolicy": "WhenUnderutilized"},
            "template": {
                "spec": {
                    "nodeClassRef": {
                        "apiVersion": "karpenter.k8s.aws/v1beta1",
                        "kind": "EC2NodeClass",
                        "name": "d",
                    }
                }
            },
        },
    }
    v = np_.pool_view(old)
    assert v["expireAfter"] == "168h" and v["consolidationPolicy"] == "WhenUnderutilized"
    assert v["nodeClassRef"] == {"group": "karpenter.k8s.aws", "kind": "EC2NodeClass", "name": "d"}
    assert np_.nodeclass_lookup(old) == ("ec2nodeclasses.karpenter.k8s.aws", "d")


def test_pool_text_and_lists_are_bounded():
    obj = copy.deepcopy(pool("broken-pool"))
    obj["status"]["conditions"][0]["message"] = "m" * 5000
    obj["status"]["conditions"] += [cond(f"C{i}", "False") for i in range(80)]
    obj["spec"]["disruption"]["budgets"] = [{"nodes": "1"} for _ in range(80)]
    v = np_.pool_view(obj)
    assert len(v["ready"]["message"]) == 1024 and v["ready"]["truncated"] is True
    assert (
        len(v["conditions"]) == 50 and len(v["budgets"]) == 50 and len(v["notTrueConditions"]) == 50
    )
    assert v["budgetCount"] == 80


def test_limits_report_usage_percent_against_the_limit():
    v = np_.limits_view(pool("healthy-pool"), 90)
    assert v["declared"] is True and v["thresholdPercent"] == 90.0
    by = {i["resource"]: i for i in v["items"]}
    assert by["cpu"] == {
        "resource": "cpu",
        "unit": "cores",
        "limit": 1000,
        "used": 8,
        "percent": 0.8,
        "atOrAbove": False,
        "exhausted": False,
    }
    assert by["memory"]["unit"] == "bytes" and by["memory"]["limit"] == 1000 * 2**30
    assert by["memory"]["used"] == 32 * 2**30 and by["memory"]["percent"] == 3.2
    # reported in status but not limited: usage is known, nothing to compare it to
    assert by["nodes"] == {
        "resource": "nodes",
        "unit": "count",
        "limit": None,
        "used": 2,
        "percent": None,
        "atOrAbove": None,
        "exhausted": None,
    }


def test_pool_at_its_cpu_limit_is_flagged():
    by = {i["resource"]: i for i in np_.limits_view(pool("full-pool"), 90)["items"]}
    assert by["cpu"]["percent"] == 100.0 and by["cpu"]["atOrAbove"] is True
    assert by["cpu"]["exhausted"] is True
    assert (
        by["nodes"]["limit"] == 3 and by["nodes"]["used"] == 3 and by["nodes"]["exhausted"] is True
    )
    assert by["memory"]["limit"] is None  # no memory limit on this pool


def test_limit_threshold_is_a_setting_and_a_millicore_limit_is_understood():
    obj = {"spec": {"limits": {"cpu": "500m"}}, "status": {"resources": {"cpu": "450m"}}}
    item = np_.limits_view(obj, 90)["items"][0]
    assert (item["limit"], item["used"], item["percent"], item["atOrAbove"]) == (
        0.5,
        0.45,
        90.0,
        True,
    )
    assert np_.limits_view(obj, 95)["items"][0]["atOrAbove"] is False


def test_limit_without_reported_usage_is_unknown_not_zero():
    obj = {"spec": {"limits": {"cpu": "10"}}}
    item = np_.limits_view(obj, 90)["items"][0]
    assert item["limit"] == 10 and item["used"] is None and item["percent"] is None
    assert item["atOrAbove"] is None and item["exhausted"] is None


def test_pool_without_limits_is_not_declared():
    v = np_.limits_view(pool("orphan-pool"), 90)
    assert v["declared"] is False and v["items"] == []


def test_nodeclass_lookup_knows_the_three_provider_types_and_no_other():
    assert np_.nodeclass_lookup(pool("healthy-pool")) == (
        "ec2nodeclasses.karpenter.k8s.aws",
        "default",
    )
    assert np_.nodeclass_lookup(pool("external-pool")) == (None, "kwok")
    for group, kind, plural in (
        ("karpenter.azure.com", "AKSNodeClass", "aksnodeclasses"),
        ("karpenter.k8s.gcp", "GCENodeClass", "gcenodeclasses"),
    ):
        obj = {
            "spec": {
                "template": {"spec": {"nodeClassRef": {"group": group, "kind": kind, "name": "n"}}}
            }
        }
        assert np_.nodeclass_lookup(obj) == (f"{plural}.{group}", "n")
    # the right group with a kind it does not have, and no reference at all
    odd = {
        "spec": {
            "template": {
                "spec": {
                    "nodeClassRef": {"group": "karpenter.k8s.aws", "kind": "Other", "name": "n"}
                }
            }
        }
    }
    assert np_.nodeclass_lookup(odd) == (None, "n")
    assert np_.nodeclass_lookup({"spec": {}}) == (None, None)


def test_nodeclass_views_ready_not_ready_missing_absent_unreadable_and_external():
    ready = np_.nodeclass_view(pool("healthy-pool"), named(load("ec2nodeclasses"), "default"), "ok")
    assert (ready["lookedUp"], ready["exists"], ready["ready"]) == (True, True, True)
    assert ready["readyCondition"]["reason"] == "Ready" and ready["typeInstalled"] is True
    broken = np_.nodeclass_view(pool("broken-pool"), named(load("ec2nodeclasses"), "broken"), "ok")
    assert broken["exists"] is True and broken["ready"] is False
    assert broken["readyCondition"]["reason"] == "SubnetsNotFound"
    gone = np_.nodeclass_view(pool("orphan-pool"), None, "not_found")
    assert gone["lookedUp"] is True and gone["exists"] is False and gone["ready"] is None
    absent = np_.nodeclass_view(pool("orphan-pool"), None, "absent")
    assert absent["exists"] is False and absent["typeInstalled"] is False
    denied = np_.nodeclass_view(pool("healthy-pool"), None, "denied")
    assert denied["lookedUp"] is False and denied["exists"] is None and denied["ready"] is None
    external = np_.nodeclass_view(pool("external-pool"), None, "external")
    assert (
        external["lookedUp"] is False and external["exists"] is None and external["ready"] is None
    )
    assert external["ref"]["kind"] == "KWOKNodeClass" and external["typeInstalled"] is None


def test_a_nodeclass_with_no_ready_condition_yet_is_unknown():
    v = np_.nodeclass_view(pool("healthy-pool"), {"metadata": {"name": "x"}}, "ok")
    assert v["exists"] is True and v["ready"] is None and v["readyCondition"] is None


# ------------------------------------------------------------ nodeclaims ----


def view(name, stuck_minutes=15, now=NOW):
    return nc.claim_view(claim(name), now, stuck_minutes)


def test_ready_claim_reports_node_and_instance_facts():
    v = view("healthy-pool-aaaaa")
    assert v["phase"] == "Ready" and v["stuck"] is False and v["deleting"] is False
    assert v["nodeName"] == "ip-10-0-1-10.ec2.internal" and v["providerID"].startswith("aws:///")
    assert (v["capacityType"], v["instanceType"], v["zone"]) == ("spot", "m5.large", "us-east-1a")
    assert [v[k]["status"] for k in ("launched", "registered", "initialized", "ready")] == [
        "True"
    ] * 4
    assert v["consolidatable"] is True and v["drifted"] is False and v["empty"] is False
    assert v["nodeClassRef"] == {
        "group": "karpenter.k8s.aws",
        "kind": "EC2NodeClass",
        "name": "default",
    }
    assert v["ageSeconds"] > 1700


def test_drifted_claim_is_marked():
    v = view("healthy-pool-bbbbb")
    assert v["drifted"] is True and v["capacityType"] == "on-demand" and v["phase"] == "Ready"


def test_a_claim_that_never_registers_is_stuck_once_older_than_the_setting():
    v = view("broken-pool-stuck1")
    assert v["phase"] == "Registering" and v["stuck"] is True
    assert v["launched"]["status"] == "True"
    assert (v["registered"]["status"], v["registered"]["reason"]) == ("Unknown", "NodeNotFound")
    assert v["ready"]["status"] == "Unknown" and v["nodeName"] is None
    assert v["ageSeconds"] > 15 * 60


def test_a_young_claim_that_is_not_ready_yet_is_not_stuck():
    # Classic flagged any claim older than the threshold; a claim still inside it is just starting.
    young = datetime(2026, 11, 3, 20, 30, tzinfo=timezone.utc)
    v = view("broken-pool-stuck1", now=young)
    assert v["phase"] == "Registering" and v["stuck"] is False
    assert view("broken-pool-stuck1", stuck_minutes=60)["stuck"] is False
    assert view("broken-pool-stuck1", stuck_minutes=1)["stuck"] is True


def test_a_claim_that_was_ready_and_went_not_ready_is_measured_from_that_transition():
    # Created long ago and Ready for most of its life: its age says nothing about how long it
    # has been unhealthy. Only the time since Ready last changed does.
    base = claim("healthy-pool-aaaaa")
    up = [cond("Launched"), cond("Registered"), cond("Initialized")]
    recent = with_status(base, [*up, cond("Ready", "False", since="2026-11-03T20:58:00Z")])
    v = nc.claim_view(recent, NOW, 15)
    assert v["phase"] == "NotReady" and v["ageSeconds"] > 1700
    assert v["notReadySeconds"] == 120 and v["stuck"] is False
    old = with_status(base, [*up, cond("Ready", "False", since="2026-11-03T20:30:00Z")])
    v = nc.claim_view(old, NOW, 15)
    assert v["notReadySeconds"] == 1800 and v["stuck"] is True
    # no Ready condition to measure from: the claim's own age is all there is
    bare = nc.claim_view(with_status(base, up), NOW, 15)
    assert bare["phase"] == "NotReady" and bare["notReadySeconds"] == bare["ageSeconds"]


def test_a_claim_that_never_became_ready_is_measured_from_creation():
    v = view("broken-pool-stuck1")
    assert v["phase"] == "Registering" and v["notReadySeconds"] == v["ageSeconds"]
    assert view("healthy-pool-aaaaa")["notReadySeconds"] is None  # Ready
    gone = copy.deepcopy(claim("broken-pool-stuck1"))
    gone["metadata"]["deletionTimestamp"] = "2026-11-03T20:58:00Z"
    assert nc.claim_view(gone, NOW, 15)["notReadySeconds"] is None


def test_phase_follows_the_first_condition_that_is_not_true():
    base = claim("broken-pool-stuck1")
    cases = [
        ([], "Launching"),
        ([cond("Launched", "False")], "Launching"),
        ([cond("Launched")], "Registering"),
        ([cond("Launched"), cond("Registered")], "Initializing"),
        ([cond("Launched"), cond("Registered"), cond("Initialized")], "NotReady"),
        ([cond("Launched"), cond("Registered"), cond("Initialized"), cond("Ready")], "Ready"),
        ([cond("Ready")], "Ready"),
    ]
    for conditions, phase in cases:
        assert nc.phase_of(with_status(base, conditions)) == phase, conditions


def test_a_claim_with_no_status_is_launching_not_healthy():
    bare = copy.deepcopy(claim("broken-pool-stuck1"))
    bare.pop("status")
    v = nc.claim_view(bare, NOW, 15)
    assert v["phase"] == "Launching" and v["stuck"] is True and v["launched"] is None


def test_a_deleting_claim_is_deleting_not_stuck():
    gone = copy.deepcopy(claim("broken-pool-stuck1"))
    gone["metadata"]["deletionTimestamp"] = "2026-11-03T20:58:00Z"
    v = nc.claim_view(gone, NOW, 15)
    assert v["phase"] == "Deleting" and v["deleting"] is True and v["stuck"] is False
    assert v["deletingSeconds"] == 120


def test_empty_condition_is_reported():
    obj = with_status(claim("healthy-pool-aaaaa"), [cond("Ready"), cond("Empty")])
    v = nc.claim_view(obj, NOW, 15)
    assert v["empty"] is True and v["phase"] == "Ready"
    assert nc.claim_view(with_status(obj, [cond("Empty", "False")]), NOW, 15)["empty"] is False


def test_claim_labels_missing_are_null_not_guessed():
    obj = copy.deepcopy(claim("healthy-pool-aaaaa"))
    obj["metadata"]["labels"] = {nc.POOL_LABEL: "healthy-pool"}
    obj["spec"].pop("nodeClassRef")
    v = nc.claim_view(obj, NOW, 15)
    assert v["capacityType"] is None and v["instanceType"] is None and v["zone"] is None
    assert v["nodeClassRef"] is None


def test_claims_are_matched_by_label_and_owner_uid():
    items = copy.deepcopy(load("nodeclaims")["items"])
    views, counts = nc.nodeclaims_for(items, pool("broken-pool"), NOW, 15, 200)
    assert sorted(v["name"] for v in views) == ["broken-pool-ready1", "broken-pool-stuck1"]
    # a claim left by an earlier pool of the same name (other owner uid) is not this pool's
    stranger = copy.deepcopy(named({"items": items}, "broken-pool-ready1"))
    stranger["metadata"]["name"] = "stranger"
    stranger["metadata"]["ownerReferences"][0]["uid"] = "someone-elses-uid"
    items.append(stranger)
    # a claim with no owner reference but the label is still counted
    loose = copy.deepcopy(stranger)
    loose["metadata"]["name"] = "loose"
    loose["metadata"].pop("ownerReferences")
    items.append(loose)
    views, _ = nc.nodeclaims_for(items, pool("broken-pool"), NOW, 15, 200)
    names = [v["name"] for v in views]
    assert "stranger" not in names and "loose" in names
    assert counts["total"] == 2


def test_claims_are_listed_stuck_first_and_counted_before_the_cap():
    items = load("nodeclaims")["items"]
    views, counts = nc.nodeclaims_for(items, pool("broken-pool"), NOW, 15, 200)
    assert [v["name"] for v in views] == ["broken-pool-stuck1", "broken-pool-ready1"]
    assert counts == {
        "total": 2,
        "ready": 1,
        "notReady": 1,
        "stuck": 1,
        "deleting": 0,
        "drifted": 0,
        "byPhase": {"Registering": 1, "Ready": 1},
        "byCapacityType": {"spot": 1, "on-demand": 1},
        "stuckMinutes": 15.0,
    }
    cut, counts = nc.nodeclaims_for(items, pool("broken-pool"), NOW, 15, 1)
    assert [v["name"] for v in cut] == ["broken-pool-stuck1"] and counts["total"] == 2


def test_healthy_pool_counts_show_nothing_stuck():
    _views, counts = nc.nodeclaims_for(
        load("nodeclaims")["items"], pool("healthy-pool"), NOW, 15, 200
    )
    assert counts["total"] == 2 and counts["ready"] == 2 and counts["stuck"] == 0
    assert counts["notReady"] == 0 and counts["drifted"] == 1
    assert counts["byCapacityType"] == {"on-demand": 1, "spot": 1}


def test_a_pool_with_no_claims_has_zero_counts():
    views, counts = nc.nodeclaims_for([], pool("orphan-pool"), NOW, 15, 200)
    assert views == [] and counts["total"] == 0 and counts["byPhase"] == {}


def test_claim_conditions_are_bounded():
    obj = copy.deepcopy(claim("healthy-pool-aaaaa"))
    obj["status"]["conditions"] += [cond(f"C{i}") for i in range(80)]
    obj["status"]["conditions"][0]["message"] = "m" * 3000
    v = nc.claim_view(obj, NOW, 15)
    assert len(v["conditions"]) == 50 and len(v["launched"]["message"]) == 1024
    assert v["launched"]["truncated"] is True


# ---------------------------------------------------------- pending pods ----


def test_pool_constraint_by_node_selector():
    assert pp.pool_constraint(pod("pending-full"), "full-pool") == (True, "nodeSelector")
    assert pp.pool_constraint(pod("pending-full"), "healthy-pool") == (False, "nodeSelector")


def test_pool_constraint_by_required_node_affinity_in():
    p = pod("pending-broken")
    assert pp.pool_constraint(p, "broken-pool") == (True, "nodeAffinity")
    assert pp.pool_constraint(p, "orphan-pool") == (True, "nodeAffinity")
    assert pp.pool_constraint(p, "full-pool") == (False, "nodeAffinity")


def test_a_pod_with_no_pool_constraint_could_go_to_any_pool():
    assert pp.pool_constraint(pod("pending-any"), "full-pool") == (True, "none")


def affinity(*terms):
    return {
        "spec": {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": t} for t in terms]
                    }
                }
            }
        }
    }


def expr(operator, *values, key="karpenter.sh/nodepool"):
    return {"key": key, "operator": operator, "values": list(values)}


def test_affinity_operators_and_ored_terms():
    # only In (and nodeSelector) select the pool; the other operators are their own class
    assert pp.pool_constraint(affinity([expr("NotIn", "a")]), "b") == (True, "nodeAffinityOther")
    assert pp.pool_constraint(affinity([expr("NotIn", "a")]), "a") == (False, "nodeAffinityOther")
    assert pp.pool_constraint(affinity([expr("Exists")]), "a") == (True, "nodeAffinityOther")
    assert pp.pool_constraint(affinity([expr("DoesNotExist")]), "a") == (
        False,
        "nodeAffinityOther",
    )
    mixed = affinity([expr("In", "a")], [expr("NotIn", "b")])
    assert pp.pool_constraint(mixed, "a") == (True, "nodeAffinityOther")  # not every term selects
    # terms are ORed: one that allows the pool is enough
    two = affinity([expr("In", "a")], [expr("In", "b")])
    assert pp.pool_constraint(two, "b") == (True, "nodeAffinity")
    assert pp.pool_constraint(two, "c") == (False, "nodeAffinity")
    # requirements inside a term are ANDed
    both = affinity([expr("In", "a", "b"), expr("NotIn", "a")])
    assert pp.pool_constraint(both, "b") == (True, "nodeAffinity")
    assert pp.pool_constraint(both, "a") == (False, "nodeAffinity")
    # a term with no requirement on the pool label leaves the pod unconstrained
    free = affinity([expr("In", "a")], [expr("In", "x", key="topology.kubernetes.io/zone")])
    assert pp.pool_constraint(free, "z") == (True, "none")
    # a requirement on another label alone constrains nothing here
    assert pp.pool_constraint(affinity([expr("In", "x", key="zone")]), "a") == (True, "none")


def test_selector_and_affinity_must_agree():
    both = affinity([expr("In", "a")])
    both["spec"]["nodeSelector"] = {"karpenter.sh/nodepool": "b"}
    assert pp.pool_constraint(both, "b") == (False, "nodeSelector")


def test_pending_pods_for_the_pool_that_the_pods_select():
    items = load("pending-pods")["items"]
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 200)
    # pending-full selects the pool, pending-any selects none; pending-broken wants other pools;
    # the bad-image pod is Pending (image pull) but already scheduled, which is not this task's concern
    assert [(r["name"], r["constraint"]) for r in rows] == [
        ("pending-full", "nodeSelector"),
        ("pending-any", "none"),
    ]
    assert counts == {
        "pending": 4,
        "unschedulable": 3,
        "couldBeServed": 2,
        "constrainedToPool": 1,
        "otherConstraint": 0,
        "unconstrained": 1,
        "selectOtherPool": 1,
        "daemonSetPods": 0,
        "pinnedToNode": 0,
    }
    full = rows[0]
    assert (
        full["namespace"] == "rw-karpenter"
        and full["uid"] == pod("pending-full")["metadata"]["uid"]
    )
    assert "didn't match Pod's node affinity/selector" in full["message"]
    assert full["requests"] == {"cpuMillicores": 500, "memoryBytes": 256 * 2**20}
    assert full["owner"] is None and full["ageSeconds"] > 1700
    assert full["unschedulableSeconds"] > 1700


def test_the_pool_a_pod_names_in_affinity_gets_it_first():
    rows, counts = pp.pending_pods_for(load("pending-pods")["items"], "broken-pool", NOW, 200)
    assert [(r["name"], r["constraint"]) for r in rows] == [
        ("pending-broken", "nodeAffinity"),
        ("pending-any", "none"),
    ]
    assert counts["constrainedToPool"] == 1 and counts["selectOtherPool"] == 1


def test_unconstrained_pod_needing_more_than_any_node_is_reported_with_its_requests():
    rows, _ = pp.pending_pods_for(load("pending-pods")["items"], "healthy-pool", NOW, 200)
    anyone = next(r for r in rows if r["name"] == "pending-any")
    assert anyone["requests"] == {"cpuMillicores": 512000, "memoryBytes": 2**40}
    assert "Insufficient cpu" in anyone["message"]


def test_a_pending_pod_the_scheduler_has_not_looked_at_yet_is_not_listed():
    items = copy.deepcopy(load("pending-pods")["items"])
    fresh = copy.deepcopy(named({"items": items}, "pending-any"))
    fresh["metadata"]["name"] = "fresh"
    fresh["status"].pop("conditions")
    items.append(fresh)
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 200)
    assert "fresh" not in [r["name"] for r in rows]
    assert counts["pending"] == 5 and counts["unschedulable"] == 3


def test_a_pod_that_is_not_pending_is_ignored_even_if_listed():
    items = copy.deepcopy(load("pending-pods")["items"])
    named({"items": items}, "pending-any")["status"]["phase"] = "Running"
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 200)
    assert [r["name"] for r in rows] == ["pending-full"] and counts["pending"] == 3


def test_unschedulable_for_another_reason_is_not_taken_for_unschedulable():
    items = copy.deepcopy(load("pending-pods")["items"])
    cond_ = named({"items": items}, "pending-any")["status"]["conditions"][0]
    cond_["reason"] = "SchedulerError"
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 200)
    assert [r["name"] for r in rows] == ["pending-full"] and counts["unschedulable"] == 2


def test_pending_pods_are_capped_after_counting_and_messages_are_bounded():
    items = copy.deepcopy(load("pending-pods")["items"])
    named({"items": items}, "pending-full")["status"]["conditions"][0]["message"] = "x" * 4000
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 1)
    assert [r["name"] for r in rows] == ["pending-full"] and counts["couldBeServed"] == 2
    assert len(rows[0]["message"]) == 1024 and rows[0]["truncated"] is True


def test_pod_requests_follow_the_effective_request_rule():
    def spec(**kw):
        return {"spec": kw}

    def c(cpu=None, mem=None, **kw):
        req = {k: v for k, v in (("cpu", cpu), ("memory", mem)) if v}
        return {"resources": {"requests": req}, **kw}

    assert pp.pod_requests(spec(containers=[c("250m", "1Gi"), c("250m")])) == {
        "cpuMillicores": 500,
        "memoryBytes": 2**30,
    }
    # the biggest init container beats the steady sum; a native sidecar is part of the steady sum
    init = spec(containers=[c("100m")], initContainers=[c("2")])
    assert pp.pod_requests(init)["cpuMillicores"] == 2000
    sidecar = spec(containers=[c("100m")], initContainers=[c("2", restartPolicy="Always")])
    assert pp.pod_requests(sidecar)["cpuMillicores"] == 2100
    assert (
        pp.pod_requests(spec(containers=[c("1")], overhead={"cpu": "100m"}))["cpuMillicores"]
        == 1100
    )
    assert (
        pp.pod_requests(spec(containers=[c("1")], resources={"requests": {"cpu": "4"}}))[
            "cpuMillicores"
        ]
        == 4000
    )
    assert pp.pod_requests(spec(containers=[c()])) == {"cpuMillicores": None, "memoryBytes": None}
    # a sidecar that started before an ordinary init container runs alongside it: 1 + 5, not 5
    before = spec(
        containers=[c("100m")],
        initContainers=[c("1", restartPolicy="Always"), c("5")],
    )
    assert pp.pod_requests(before)["cpuMillicores"] == 6000
    # a sidecar listed after the init container did not start before it: peak 5, steady 1.1
    after = spec(
        containers=[c("100m")],
        initContainers=[c("5"), c("1", restartPolicy="Always")],
    )
    assert pp.pod_requests(after)["cpuMillicores"] == 5000


def test_pod_owner_is_the_controller():
    p = copy.deepcopy(pod("pending-any"))
    p["metadata"]["ownerReferences"] = [
        {"kind": "Job", "name": "j", "controller": False},
        {"kind": "ReplicaSet", "name": "rs", "controller": True},
    ]
    row = pp.pod_view(p, pp.unschedulable_condition(p), "none", NOW)
    assert row["owner"] == {"kind": "ReplicaSet", "name": "rs"}


def owned_by(p, kind):
    p["metadata"]["ownerReferences"] = [{"kind": kind, "name": "owner", "controller": True}]
    return p


def pinned(p, node="kind-control-plane"):
    p["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}
                ]
            }
        }
    }
    return p


def test_daemonset_pods_are_counted_apart_and_not_listed():
    items = copy.deepcopy(load("pending-pods")["items"])
    ds = pinned(owned_by(copy.deepcopy(named({"items": items}, "pending-any")), "DaemonSet"))
    ds["metadata"]["name"] = "ds-pod"
    items.append(ds)
    rows, counts = pp.pending_pods_for(items, "full-pool", NOW, 200)
    assert "ds-pod" not in [r["name"] for r in rows]
    assert counts["daemonSetPods"] == 1 and counts["unschedulable"] == 4
    # a DaemonSet pod is not "unconstrained": it cannot go to a node Karpenter would make
    assert counts["unconstrained"] == 1 and counts["couldBeServed"] == 2
    # a DaemonSet pod that carries no matchFields pin is still a DaemonSet pod
    ds2 = owned_by(copy.deepcopy(named({"items": items}, "pending-any")), "DaemonSet")
    ds2["metadata"]["name"] = "ds-2"
    rows, counts = pp.pending_pods_for([*items, ds2], "full-pool", NOW, 200)
    assert counts["daemonSetPods"] == 2 and "ds-2" not in [r["name"] for r in rows]


def test_pods_pinned_to_a_node_by_name_are_counted_apart_and_not_listed():
    items = copy.deepcopy(load("pending-pods")["items"])
    one = pinned(copy.deepcopy(named({"items": items}, "pending-any")))
    one["metadata"]["name"] = "pinned"
    rows, counts = pp.pending_pods_for([*items, one], "full-pool", NOW, 200)
    assert "pinned" not in [r["name"] for r in rows]
    assert counts["pinnedToNode"] == 1 and counts["daemonSetPods"] == 0
    assert counts["unschedulable"] == 4 and counts["couldBeServed"] == 2
    # a term that pins by name and one that does not: not pinned (terms are ORed)
    either = copy.deepcopy(one)
    either["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ].append({"matchExpressions": [expr("In", "x", key="zone")]})
    either["metadata"]["name"] = "either"
    rows, counts = pp.pending_pods_for([*items, either], "full-pool", NOW, 200)
    assert "either" in [r["name"] for r in rows] and counts["pinnedToNode"] == 0


def test_pods_that_only_exclude_the_pool_are_not_counted_as_constrained_to_it():
    items = copy.deepcopy(load("pending-pods")["items"])
    other = copy.deepcopy(named({"items": items}, "pending-any"))
    other["metadata"]["name"] = "not-in"
    other["spec"]["affinity"] = affinity([expr("NotIn", "broken-pool")])["spec"]["affinity"]
    exists = copy.deepcopy(other)
    exists["metadata"]["name"] = "exists"
    exists["spec"]["affinity"] = affinity([expr("Exists")])["spec"]["affinity"]
    rows, counts = pp.pending_pods_for([*items, other, exists], "full-pool", NOW, 200)
    by = {r["name"]: r["constraint"] for r in rows}
    assert by["not-in"] == by["exists"] == "nodeAffinityOther"
    assert counts["constrainedToPool"] == 1 and counts["otherConstraint"] == 2
    # listed after the pods that select the pool, before the unconstrained ones
    assert [r["name"] for r in rows][0] == "pending-full"
    assert [r["constraint"] for r in rows][-1] == "none"


def rows_for(pool_name):
    return pp.pending_pods_for(load("pending-pods")["items"], pool_name, NOW, 200)[0]


def test_events_are_matched_to_the_pods_by_uid_and_grouped_per_pod_and_reason():
    rows = rows_for("full-pool")
    events = pp.pod_events(load("pod-events")["items"], rows, "1h", NOW, 200)
    by = {(e["pod"], e["reason"]): e for e in events}
    assert set(by) == {("pending-full", "FailedScheduling"), ("pending-any", "FailedScheduling")}
    # the two events of pending-any are one group: 1 + the series' 6
    assert by[("pending-any", "FailedScheduling")]["count"] == 7
    assert by[("pending-any", "FailedScheduling")]["lastSeen"] == "2026-11-03T20:29:03Z"
    assert by[("pending-full", "FailedScheduling")]["reportingComponent"] == "default-scheduler"
    assert by[("pending-full", "FailedScheduling")]["type"] == "Warning"
    assert "didn't match" in by[("pending-full", "FailedScheduling")]["message"]
    assert events[0]["pod"] == "pending-any"  # newest first


def test_events_of_a_pod_that_is_not_in_the_list_are_left_out():
    # pending-broken selects other pools for full-pool, and the bad-image events belong to a pod that is scheduled
    events = pp.pod_events(load("pod-events")["items"], rows_for("full-pool"), "1h", NOW, 200)
    assert "pending-broken" not in [e["pod"] for e in events]
    assert "bad-image-866577b69d-llmzv" not in [e["pod"] for e in events]


def test_an_event_about_an_earlier_pod_of_the_same_name_is_ignored():
    items = copy.deepcopy(load("pod-events")["items"])
    for e in items:
        if e["involvedObject"]["name"] == "pending-full":
            e["involvedObject"]["uid"] = "an-earlier-incarnation"
    events = pp.pod_events(items, rows_for("full-pool"), "1h", NOW, 200)
    assert "pending-full" not in [e["pod"] for e in events]


def test_nominated_and_other_normal_events_are_included_and_the_window_applies():
    nominated = {
        "type": "Normal",
        "reason": "Nominated",
        "message": "Pod should schedule on: nodeclaim/full-pool-xyz",
        "count": 1,
        "firstTimestamp": "2026-11-03T20:40:00Z",
        "lastTimestamp": "2026-11-03T20:40:00Z",
        "source": {"component": "karpenter"},
        "involvedObject": {"kind": "Pod", "uid": pod("pending-full")["metadata"]["uid"]},
        "metadata": {},
    }
    old = copy.deepcopy(nominated)
    old["reason"] = "Old"
    old["lastTimestamp"] = "2026-11-03T18:00:00Z"
    rows = rows_for("full-pool")
    events = pp.pod_events([nominated, old], rows, "1h", NOW, 200)
    assert [(e["reason"], e["type"], e["reportingComponent"]) for e in events] == [
        ("Nominated", "Normal", "karpenter")
    ]
    assert [e["reason"] for e in pp.pod_events([nominated, old], rows, "6h", NOW, 200)] == [
        "Nominated",
        "Old",
    ]
    assert pp.pod_events([nominated, old], rows, "10m", NOW, 200) == []


def test_event_messages_are_bounded_and_the_list_is_capped():
    items = copy.deepcopy(load("pod-events")["items"])
    for e in items:
        e["message"] = "m" * 3000
    events = pp.pod_events(items, rows_for("full-pool"), "1h", NOW, 200)
    assert all(len(e["message"]) == 1024 and e["truncated"] is True for e in events)
    assert len(pp.pod_events(items, rows_for("full-pool"), "1h", NOW, 1)) == 1


# --------------------------------------------------------------- scripts ----


class FakeCtx:
    """Replays captured JSON for kubectl calls, raising like the SDK's ctx.run on a non-zero exit.

    `fail` maps a kubectl noun to the stderr kubectl would print. `pools` replaces the captured
    NodePools; `classes` maps a NodeClass noun to its object list (an unlisted NodeClass type is
    not installed).
    """

    def __init__(
        self,
        name,
        fail=None,
        pools=None,
        classes=None,
        max_items=200,
        stuck_minutes=15,
        limit_warning_percent=90,
        rtype=TYPE,
        events=None,
        pods=None,
    ):
        self.resource = SimpleNamespace(name=name, type=rtype)
        self.settings = SimpleNamespace(
            max_items=max_items,
            stuck_minutes=stuck_minutes,
            limit_warning_percent=limit_warning_percent,
        )
        self.fail = fail or {}
        self.pools = pools if pools is not None else load("nodepools")["items"]
        self.classes = (
            classes
            if classes is not None
            else {"ec2nodeclasses.karpenter.k8s.aws": load("ec2nodeclasses")["items"]}
        )
        self.events = events if events is not None else load("pod-events")["items"]
        self.pods = pods if pods is not None else load("pending-pods")["items"]
        self.errors = []
        self.messages = []
        self.calls = []

    def now(self):
        return NOW

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope})
        self.messages.append(message)

    def run(self, argv, check=True):
        self.calls.append(argv)
        words, flags, it = [], {}, iter(argv[1:])
        for a in it:
            if a in ("-o", "-l", "--field-selector"):
                flags[a] = next(it)
            elif not a.startswith("-"):
                words.append(a)
            else:
                flags[a] = True
        _verb, noun, *rest = words
        for key, stderr in self.fail.items():
            if key == noun:
                return self._done(argv, 1, "", stderr, check)
        missing = None
        if noun == "nodepools.karpenter.sh":
            doc, missing = {"items": self.pools}, rest[0]
        elif noun == "nodeclaims.karpenter.sh":
            label = flags["-l"].split("=")
            doc = {
                "items": [
                    c
                    for c in load("nodeclaims")["items"]
                    if c["metadata"]["labels"].get(label[0]) == label[1]
                ]
            }
        elif noun in (
            "ec2nodeclasses.karpenter.k8s.aws",
            "aksnodeclasses.karpenter.azure.com",
            "gcenodeclasses.karpenter.k8s.gcp",
        ):
            if noun not in self.classes:
                plural = noun.split(".")[0]
                return self._done(
                    argv,
                    1,
                    "",
                    f'error: the server doesn\'t have a resource type "{plural}"',
                    check,
                )
            doc, missing = {"items": self.classes[noun]}, rest[0]
        elif noun == "pods":
            assert flags["--field-selector"] == "status.phase=Pending" and "-A" in flags
            doc = {"items": [p for p in self.pods if p["status"]["phase"] == "Pending"]}
        elif noun == "events":
            selector = dict(kv.split("=") for kv in flags["--field-selector"].split(","))
            assert selector["involvedObject.kind"] == "Pod" and "-A" in flags
            doc = {"items": [e for e in self.events if e["reason"] == selector["reason"]]}
        else:
            raise AssertionError(argv)
        if missing is not None:
            match = [i for i in doc["items"] if i["metadata"]["name"] == missing]
            if not match:
                err = f'Error from server (NotFound): {noun.split(".")[0]} "{missing}" not found'
                return self._done(argv, 1, "", err, check)
            doc = match[0]
        return self._done(argv, 0, json.dumps(doc), "", check)

    @staticmethod
    def _done(argv, rc, out, err, check):
        if rc and check:
            raise RwCommandError(
                classify_command_error(argv, rc, err),
                f"{argv[0]} exited {rc}: {err}",
                argv=argv,
                returncode=rc,
                stderr=err,
            )
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class LegacyCtx(FakeCtx):
    """A ctx.run that returns the CompletedProcess without raising (the stand-in SDK)."""

    def run(self, argv, check=True):
        return super().run(argv, check=False)


def script(name):
    spec = importlib.util.spec_from_file_location(f"kp_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


FORBIDDEN = (
    'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" '
    'cannot get resource "x" in API group "karpenter.sh" at the cluster scope'
)


def test_status_script_on_a_healthy_pool():
    ctx = FakeCtx("healthy-pool")
    out = script("nodepool_status")(ctx)
    check_outputs("nodepool-status", out)
    assert out["pool"]["ready"]["status"] == "True" and out["pool"]["notTrueConditions"] == []
    assert out["nodeClass"]["exists"] is True and out["nodeClass"]["ready"] is True
    assert {i["resource"]: i["atOrAbove"] for i in out["limits"]["items"]}["cpu"] is False
    assert ctx.errors == []
    assert [c[2] for c in ctx.calls] == [
        "nodepools.karpenter.sh",
        "ec2nodeclasses.karpenter.k8s.aws",
    ]


def test_status_script_on_a_pool_at_its_limit():
    out = script("nodepool_status")(FakeCtx("full-pool"))
    check_outputs("nodepool-status", out)
    cpu = next(i for i in out["limits"]["items"] if i["resource"] == "cpu")
    assert cpu["atOrAbove"] is True and cpu["exhausted"] is True
    assert out["pool"]["ready"]["status"] == "True"  # at its limit and still Ready


def test_status_script_honours_the_limit_setting():
    out = script("nodepool_status")(FakeCtx("healthy-pool", limit_warning_percent=0.5))
    assert next(i for i in out["limits"]["items"] if i["resource"] == "cpu")["atOrAbove"] is True


def test_status_script_on_a_pool_whose_nodeclass_is_not_ready():
    out = script("nodepool_status")(FakeCtx("broken-pool"))
    check_outputs("nodepool-status", out)
    assert out["pool"]["ready"]["status"] == "False"
    assert out["nodeClass"]["exists"] is True and out["nodeClass"]["ready"] is False
    assert out["nodeClass"]["readyCondition"]["reason"] == "SubnetsNotFound"


def test_missing_nodeclass_is_a_fact_not_an_error():
    ctx = FakeCtx("orphan-pool")
    out = script("nodepool_status")(ctx)
    check_outputs("nodepool-status", out)
    assert out["nodeClass"]["exists"] is False and out["nodeClass"]["ready"] is None
    assert out["nodeClass"]["lookedUp"] is True and ctx.errors == []


def test_nodeclass_type_not_installed_is_a_fact_not_an_error():
    ctx = FakeCtx("healthy-pool", classes={})
    out = script("nodepool_status")(ctx)
    check_outputs("nodepool-status", out)
    assert out["nodeClass"]["exists"] is False and out["nodeClass"]["typeInstalled"] is False
    assert ctx.errors == []


def test_external_nodeclass_group_is_not_looked_up():
    ctx = FakeCtx("external-pool")
    out = script("nodepool_status")(ctx)
    check_outputs("nodepool-status", out)
    nc_ = out["nodeClass"]
    assert nc_["lookedUp"] is False and nc_["exists"] is None and nc_["ready"] is None
    assert (nc_["ref"]["group"], nc_["ref"]["kind"]) == ("karpenter.kwok.sh", "KWOKNodeClass")
    assert ctx.errors == [] and len(ctx.calls) == 1  # only the pool was read


def test_azure_and_gcp_nodeclasses_are_read_from_their_own_groups():
    for group, kind, noun in (
        ("karpenter.azure.com", "AKSNodeClass", "aksnodeclasses.karpenter.azure.com"),
        ("karpenter.k8s.gcp", "GCENodeClass", "gcenodeclasses.karpenter.k8s.gcp"),
    ):
        p = copy.deepcopy(pool("external-pool"))
        p["spec"]["template"]["spec"]["nodeClassRef"] = {"group": group, "kind": kind, "name": "c"}
        klass = {"metadata": {"name": "c"}, "status": {"conditions": [cond("Ready")]}}
        ctx = FakeCtx("external-pool", pools=[p], classes={noun: [klass]})
        out = script("nodepool_status")(ctx)
        assert out["nodeClass"]["exists"] is True and out["nodeClass"]["ready"] is True
        assert ctx.calls[-1][2] == noun


def test_forbidden_nodeclass_keeps_the_pool_and_reports_the_scope():
    ctx = FakeCtx(
        "healthy-pool", fail={"ec2nodeclasses.karpenter.k8s.aws": FORBIDDEN % "ec2nodeclasses"}
    )
    out = script("nodepool_status")(ctx)
    check_outputs("nodepool-status", out)
    assert out["pool"]["ready"]["status"] == "True" and out["limits"]["declared"] is True
    assert out["nodeClass"]["exists"] is None and out["nodeClass"]["ready"] is None
    assert out["nodeClass"]["lookedUp"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "nodeclasses")]


def test_forbidden_pool_fails_instead_of_returning_empty():
    for name in ("nodepool_status", "nodeclaims", "pending_pods"):
        ctx = FakeCtx("healthy-pool", fail={"nodepools.karpenter.sh": FORBIDDEN % "nodepools"})
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "permission_denied"


def test_missing_pool_is_not_found_for_every_task():
    for name in ("nodepool_status", "nodeclaims", "pending_pods"):
        ctx = FakeCtx("nope")
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "not_found"
        assert len(ctx.calls) == 1  # nothing else was read for a pool that is not there


def test_unreachable_api_server_raises_and_is_not_swallowed_by_the_scoped_reads():
    err = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"
    for name, noun in (
        ("nodepool_status", "nodepools.karpenter.sh"),
        ("nodepool_status", "ec2nodeclasses.karpenter.k8s.aws"),
        ("nodeclaims", "nodeclaims.karpenter.sh"),
        ("pending_pods", "pods"),
        ("pending_pods", "events"),
    ):
        with pytest.raises(RwError) as exc:
            script(name)(FakeCtx("full-pool", fail={noun: err}))
        assert exc.value.code == "unreachable", (name, noun)


def test_a_ctx_run_that_returns_nonzero_is_checked_too():
    ctx = LegacyCtx("healthy-pool", fail={"nodepools.karpenter.sh": FORBIDDEN % "x"})
    with pytest.raises(RwError) as exc:
        script("nodepool_status")(ctx)
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        script("nodeclaims")(LegacyCtx("nope"))
    assert exc.value.code == "not_found"
    with pytest.raises(RwError) as exc:
        script("nodeclaims")(
            LegacyCtx("full-pool", fail={"nodeclaims.karpenter.sh": FORBIDDEN % "x"})
        )
    assert exc.value.code == "permission_denied"
    assert script("nodepool_status")(LegacyCtx("orphan-pool"))["nodeClass"]["exists"] is False


def test_other_resource_types_are_refused_before_any_kubectl_call():
    for name in ("nodepool_status", "nodeclaims", "pending_pods"):
        ctx = FakeCtx("x", rtype="kubernetes/deployment")
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_type_falls_back_to_the_path_plural():
    ctx = FakeCtx("healthy-pool", rtype=None)
    ctx.resource.path = "kubernetes/clusters/kind/nodepools/healthy-pool"
    assert script("nodepool_status")(ctx)["pool"]["name"] == "healthy-pool"
    ctx = FakeCtx("healthy-pool", rtype=None)
    ctx.resource.path = "kubernetes/clusters/kind/nodes/healthy-pool"
    with pytest.raises(RwError) as exc:
        script("nodepool_status")(ctx)
    assert exc.value.code == "invalid_input"


def test_nodeclaims_script_on_the_pool_with_a_stuck_claim():
    ctx = FakeCtx("broken-pool")
    out = script("nodeclaims")(ctx)
    check_outputs("nodeclaims", out)
    assert [(v["name"], v["phase"], v["stuck"]) for v in out["nodeClaims"]] == [
        ("broken-pool-stuck1", "Registering", True),
        ("broken-pool-ready1", "Ready", False),
    ]
    assert out["counts"]["stuck"] == 1 and out["counts"]["ready"] == 1
    # the label selector does the narrowing, so another pool's claims are never fetched
    assert ctx.calls[-1][:6] == [
        "kubectl",
        "get",
        "nodeclaims.karpenter.sh",
        "-l",
        "karpenter.sh/nodepool=broken-pool",
        "-o",
    ]


def test_nodeclaims_script_on_a_healthy_pool_and_one_with_no_claims():
    out = script("nodeclaims")(FakeCtx("healthy-pool"))
    check_outputs("nodeclaims", out)
    assert out["counts"]["stuck"] == 0 and out["counts"]["ready"] == 2
    empty = script("nodeclaims")(FakeCtx("orphan-pool"))
    assert empty["nodeClaims"] == [] and empty["counts"]["total"] == 0


def test_nodeclaims_script_honours_its_settings():
    out = script("nodeclaims")(FakeCtx("broken-pool", stuck_minutes=100000))
    assert out["counts"]["stuck"] == 0 and out["counts"]["notReady"] == 1
    assert len(script("nodeclaims")(FakeCtx("broken-pool", max_items=1))["nodeClaims"]) == 1


def test_forbidden_nodeclaim_list_fails_the_task_instead_of_returning_no_claims():
    ctx = FakeCtx("broken-pool", fail={"nodeclaims.karpenter.sh": FORBIDDEN % "nodeclaims"})
    with pytest.raises(RwError) as exc:
        script("nodeclaims")(ctx)
    assert exc.value.code == "permission_denied"


def test_pending_script_on_the_pool_the_pods_select():
    ctx = FakeCtx("full-pool")
    out = script("pending_pods")(ctx)
    check_outputs("pending-pods", out)
    assert [p["name"] for p in out["pods"]] == ["pending-full", "pending-any"]
    assert out["counts"]["constrainedToPool"] == 1 and out["counts"]["selectOtherPool"] == 1
    assert {e["pod"] for e in out["events"]} == {"pending-full", "pending-any"}
    assert ctx.errors == []


def test_pending_script_on_a_pool_nothing_waits_for_still_lists_unconstrained_pods():
    out = script("pending_pods")(FakeCtx("healthy-pool"))
    assert [p["name"] for p in out["pods"]] == ["pending-any"]
    assert out["counts"]["constrainedToPool"] == 0 and out["counts"]["unconstrained"] == 1


def test_pending_script_with_no_pending_pods_reads_no_events():
    ctx = FakeCtx("full-pool", pods=[])
    out = script("pending_pods")(ctx)
    check_outputs("pending-pods", out)
    # nothing was listed, so no event was read: null, not "no events"
    assert out["pods"] == [] and out["events"] is None and out["counts"]["pending"] == 0
    assert not any("events" in c for c in ctx.calls)


def test_events_are_read_per_provisioning_reason_not_as_every_pod_event():
    ctx = FakeCtx("full-pool")
    nominated = {
        "type": "Normal",
        "reason": "Nominated",
        "message": "Pod should schedule on: nodeclaim/full-pool-xyz",
        "lastTimestamp": "2026-11-03T20:50:00Z",
        "reportingComponent": "karpenter",
        "involvedObject": {"kind": "Pod", "uid": pod("pending-full")["metadata"]["uid"]},
        "metadata": {},
    }
    ctx.events = [*ctx.events, nominated]
    out = script("pending_pods")(ctx)
    selectors = [c[c.index("--field-selector") + 1] for c in ctx.calls if "events" in c]
    assert selectors == [
        "involvedObject.kind=Pod,reason=FailedScheduling",
        "involvedObject.kind=Pod,reason=Nominated",
        "involvedObject.kind=Pod,reason=FailedProvisioning",
    ]
    assert ("pending-full", "Nominated", "Normal") in {
        (e["pod"], e["reason"], e["type"]) for e in out["events"]
    }


def test_forbidden_events_keep_the_pods_and_make_events_null():
    ctx = FakeCtx("full-pool", fail={"events": FORBIDDEN % "events"})
    out = script("pending_pods")(ctx)
    check_outputs("pending-pods", out)
    assert out["events"] is None and len(out["pods"]) == 2
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "events")]
    assert len([c for c in ctx.calls if "events" in c]) == 1  # the first refusal ends the reads


def test_forbidden_pod_list_fails_the_task_instead_of_returning_no_pods():
    ctx = FakeCtx("full-pool", fail={"pods": FORBIDDEN % "pods"})
    with pytest.raises(RwError) as exc:
        script("pending_pods")(ctx)
    assert exc.value.code == "permission_denied"


def test_pending_script_validates_since_before_any_kubectl_call():
    ctx = FakeCtx("full-pool")
    with pytest.raises(RwError) as exc:
        script("pending_pods")(ctx, since="yesterday")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_pending_script_since_narrows_the_events():
    ctx = FakeCtx("full-pool")
    assert script("pending_pods")(ctx, since="1m")["events"] == []
    assert len(script("pending_pods")(FakeCtx("full-pool"), since="3650d")["events"]) == 2


def test_pending_script_caps_pods_by_the_setting():
    out = script("pending_pods")(FakeCtx("full-pool", max_items=1))
    assert [p["name"] for p in out["pods"]] == ["pending-full"] and out["counts"][
        "couldBeServed"
    ] == 2


def test_no_task_reads_a_secret_or_emits_environment_values():
    for name, pools in (
        ("nodepool_status", ("healthy-pool", "broken-pool")),
        ("nodeclaims", ("broken-pool",)),
        ("pending_pods", ("full-pool",)),
    ):
        for target in pools:
            ctx = FakeCtx(target)
            out = script(name)(ctx)
            for argv in ctx.calls:
                assert not any("secret" in a.lower() for a in argv), argv
                assert argv[-2:] == ["-o", "json"]
            text = json.dumps(out)
            assert '"env"' not in text and "valueFrom" not in text


# ------------------------------------------------------------ permissions ----

# kubectl noun -> (apiGroup, plural)
RESOURCES = {
    "nodepools.karpenter.sh": ("karpenter.sh", "nodepools"),
    "nodeclaims.karpenter.sh": ("karpenter.sh", "nodeclaims"),
    "ec2nodeclasses.karpenter.k8s.aws": ("karpenter.k8s.aws", "ec2nodeclasses"),
    "aksnodeclasses.karpenter.azure.com": ("karpenter.azure.com", "aksnodeclasses"),
    "gcenodeclasses.karpenter.k8s.gcp": ("karpenter.k8s.gcp", "gcenodeclasses"),
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

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector / -A) -> list;
    logs -> pods/log get; exec -> pods get + pods/exec create. Raises on anything else, so a
    skill that issues a new verb has to extend this mapping.
    """
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a == "--":
            break
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif a.startswith("-"):
            flags[a] = True
        else:
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named_object = (
            len(positional) > 2
            and "-l" not in flags
            and "--selector" not in flags
            and "-A" not in flags
        )
        return {(group, plural, "get" if named_object else "list")}
    if verb == "logs":
        return {("", "pods/log", "get")}
    if verb == "exec":
        return {("", "pods", "get"), ("", "pods/exec", "create")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def aks_gcp_pool(group, kind):
    p = copy.deepcopy(pool("external-pool"))
    p["spec"]["template"]["spec"]["nodeClassRef"] = {"group": group, "kind": kind, "name": "c"}
    return p


@pytest.mark.parametrize(
    "task,targets",
    [
        (
            "nodepool-status",
            ["healthy-pool", "full-pool", "broken-pool", "orphan-pool", "external-pool"],
        ),
        ("nodeclaims", ["broken-pool", "healthy-pool", "orphan-pool"]),
        ("pending-pods", ["full-pool", "healthy-pool"]),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, targets):
    needed = set()
    for target in targets:
        ctx = FakeCtx(target)
        script(SCRIPT_OF[task])(ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    if task == "nodepool-status":  # the NodeClass types of the other providers
        for group, kind, noun in (
            ("karpenter.azure.com", "AKSNodeClass", "aksnodeclasses.karpenter.azure.com"),
            ("karpenter.k8s.gcp", "GCENodeClass", "gcenodeclasses.karpenter.k8s.gcp"),
        ):
            ctx = FakeCtx("external-pool", pools=[aks_gcp_pool(group, kind)], classes={noun: []})
            script("nodepool_status")(ctx)
            needed |= {n for argv in ctx.calls for n in rbac_needs(argv)}
    missing = needed - declared(TASKS[task])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_each_task_declares_only_what_it_calls():
    status = declared(TASKS["nodepool-status"])
    claims = declared(TASKS["nodeclaims"])
    pending = declared(TASKS["pending-pods"])
    assert not {r for _g, r, _v in status} & {"nodeclaims", "pods", "events"}
    assert {r for _g, r, _v in claims} == {"nodepools", "nodeclaims"}
    assert {r for _g, r, _v in pending} == {"nodepools", "pods", "events"}
    assert claims == {("karpenter.sh", "nodepools", "get"), ("karpenter.sh", "nodeclaims", "list")}
    # nodepool-status declares exactly the NodeClass types it can read, no more
    assert {(g, r) for g, r, _v in status} - {("karpenter.sh", "nodepools")} == {
        (g, p) for (g, _k), p in ((k, v) for k, v in np_.NODECLASS_TYPES.items())
    }


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "get", "nodepools.karpenter.sh", "p", "-o", "json"]) == {
        ("karpenter.sh", "nodepools", "get")
    }
    assert rbac_needs(["kubectl", "get", "nodeclaims.karpenter.sh", "-l", "a=b", "-o", "json"]) == {
        ("karpenter.sh", "nodeclaims", "list")
    }
    assert rbac_needs(["kubectl", "get", "pods", "-A", "--field-selector", "x", "-o", "json"]) == {
        ("", "pods", "list")
    }


# ------------------------------------------------------------ k8s.py ----


def test_classify_maps_stderr_to_d64_codes():
    cases = {
        (1, "Error from server (Forbidden): x"): "permission_denied",
        (1, 'Error from server (NotFound): x "y" not found'): "not_found",
        (1, "Unable to connect to the server: connection refused"): "unreachable",
        (1, "context deadline exceeded"): "timeout",
        (1, "Too Many Requests"): "rate_limited",
        (1, 'error: the server doesn\'t have a resource type "nodepools"'): "dependency",
        (127, ""): "dependency",
        (124, ""): "timeout",
        (1, "something odd"): "internal",
    }
    for (rc, text), code in cases.items():
        assert k8s.classify(rc, text) == code


def test_kubectl_output_that_is_not_json_is_an_internal_error():
    class Ctx:
        def run(self, argv, check=True):
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "internal"


def test_ctx_error_text_is_bounded():
    ctx = FakeCtx(
        "healthy-pool", fail={"ec2nodeclasses.karpenter.k8s.aws": FORBIDDEN % ("x" * 4000)}
    )
    script("nodepool_status")(ctx)
    assert ctx.errors and all(len(m) <= 1100 for m in ctx.messages)
