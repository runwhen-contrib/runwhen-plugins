"""Unit tests for k8s-namespace-resources' lib/ and scripts against JSON captured from
the kind-rw-plugins scenarios (plugins/kubernetes/tests/kind, namespace rw-nsres).
kind has no metrics.k8s.io, so the PodMetrics used for resource-usage are synthetic."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from k8s_namespace_resources import access, common, images, k8s, usage, vpa
from k8s_namespace_resources import settings as s
from k8s_namespace_resources.k8s import RwError
from runwhen_capability.rwerrors import RwCommandError, classify_command_error

SKILL = Path(__file__).parents[2] / "skills" / "k8s-namespace-resources"
DATA = Path(__file__).parent / "data" / "k8s_namespace_resources"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def pod(name):
    return named(load("nsres_pods"), name)


def podmetrics(name, **containers):
    """A synthetic PodMetrics: containers as {name: (cpu, memory)} usage quantities."""
    return {
        "apiVersion": "metrics.k8s.io/v1beta1",
        "kind": "PodMetrics",
        "metadata": {"name": name, "namespace": "rw-nsres"},
        "timestamp": "2026-11-03T20:00:00Z",
        "window": "15s",
        "containers": [
            {"name": c, "usage": {"cpu": cpu, "memory": mem}}
            for c, (cpu, mem) in containers.items()
        ],
    }


def metrics_list(*items):
    return {"apiVersion": "v1", "kind": "List", "items": list(items)}


# ------------------------------------------------------------ helpers ----


def test_quantities_understand_every_suffix_the_classic_check_misread():
    # The classic arithmetic converted only Gi/Mi and m/cores; nanocores and Ki read as 0.
    assert common.millicores("250m") == 250
    assert common.millicores("2") == 2000
    assert common.millicores("123456789n") == 123.457
    assert common.millicores("1500u") == 1.5
    assert common.millicores(None) is None
    assert common.bytes_of("100Mi") == 100 * 2**20
    assert common.bytes_of("512Ki") == 512 * 1024
    assert common.bytes_of("1.5Gi") == 1610612736
    assert common.bytes_of("500M") == 500_000_000
    assert common.bytes_of("1e3") == 1000
    with pytest.raises(ValueError):
        common.parse_quantity("lots")


def test_percent_has_no_answer_without_a_whole():
    assert common.percent(45, 50) == 90.0
    assert common.percent(1, 3) == 33.3
    assert common.percent(5, 0) is None and common.percent(5, None) is None
    assert common.percent(None, 5) is None


# ------------------------------------------------------ resource-settings ----


def test_container_with_no_resources_misses_all_four_settings():
    flagged = s.flagged_pod_containers([pod("unsized-77c4f4fc65-cnk7n")])
    ((_, entry),) = flagged
    assert entry["container"] == "main" and entry["init"] is False
    assert entry["missing"] == ["cpuRequest", "cpuLimit", "memoryRequest", "memoryLimit"]
    assert entry["cpuRequest"] is None and entry["memoryLimit"] is None


def test_requests_without_limits_misses_only_the_limits():
    ((_, entry),) = s.flagged_pod_containers([pod("requests-only")])
    assert entry["missing"] == ["cpuLimit", "memoryLimit"]
    assert entry["cpuRequest"] == "1m" and entry["memoryRequest"] == "1Mi"


def test_init_containers_are_flagged_with_init_true():
    # Classic ignored init containers: init-wait has requests and no limits.
    ((_, entry),) = s.flagged_pod_containers([pod("sized-9d6fb4dcb-bcf2w")])
    assert entry["container"] == "init-wait" and entry["init"] is True
    assert entry["missing"] == ["cpuLimit", "memoryLimit"]
    # ...while the fully set main container is not listed.


def test_pods_that_are_not_running_are_not_inspected():
    # bad-image is Pending: its container has requests and limits anyway, but a Pending
    # pod is not "running pods"; the count says so instead of looking healthy by omission.
    assert s.flagged_pod_containers([pod("bad-image-5f95784c79-xxfmh")]) == []


def test_pod_owner_is_resolved_through_the_replicaset():
    flagged = s.flagged_pod_containers(load("nsres_pods")["items"])
    assert s.needs_kinds(flagged) == {"ReplicaSet"}
    entries = s.pod_entries(flagged, load("nsres_replicasets")["items"], None)
    owners = {e["pod"]: e["owner"] for e in entries}
    assert owners["unsized-77c4f4fc65-cnk7n"] == {
        "kind": "Deployment",
        "name": "unsized",
        "resolved": True,
    }
    assert owners["sized-9d6fb4dcb-bcf2w"]["name"] == "sized"
    assert owners["requests-only"] is None  # a bare pod has no owner


def test_unreadable_replicasets_leave_the_owner_unresolved():
    entries = s.pod_entries(s.flagged_pod_containers([pod("unsized-77c4f4fc65-cnk7n")]), None, None)
    assert entries[0]["owner"] == {
        "kind": "ReplicaSet",
        "name": "unsized-77c4f4fc65",
        "resolved": False,
    }


def test_workload_templates_are_checked_too():
    deployments = load("nsres_deployments")["items"]
    entries = s.workload_entries("Deployment", deployments)
    assert [(e["name"], e["container"], e["init"]) for e in entries] == [
        ("sized", "init-wait", True),
        ("unsized", "main", False),
    ]
    assert entries[1]["missing"] == ["cpuRequest", "cpuLimit", "memoryRequest", "memoryLimit"]


def test_container_counts_cover_init_containers_of_running_pods_only():
    assert (
        s.count_containers(load("nsres_pods")["items"]) == 4
    )  # sized 2, unsized 1, requests-only 1


# ------------------------------------------------------- resource-usage ----


def test_usage_math_against_limits_and_requests():
    sized = pod("sized-9d6fb4dcb-bcf2w")  # app: requests 10m/8Mi, limits 50m/32Mi
    pm = podmetrics("sized-9d6fb4dcb-bcf2w", app=("45000000n", "16Mi"))
    entries, counts = usage.usage_entries([pm], [sized], 90)
    by = {e["resource"]: e for e in entries}
    assert (
        by["cpu"]["usage"] == 45.0 and by["cpu"]["limit"] == 50.0 and by["cpu"]["request"] == 10.0
    )
    assert by["cpu"]["percentOfLimit"] == 90.0 and by["cpu"]["percentOfRequest"] == 450.0
    assert by["cpu"]["atOrAbove"] is True  # at the threshold counts
    assert by["memory"]["usage"] == 16 * 2**20 and by["memory"]["limit"] == 32 * 2**20
    assert by["memory"]["percentOfLimit"] == 50.0 and by["memory"]["atOrAbove"] is False
    assert by["memory"]["unit"] == "bytes" and by["cpu"]["unit"] == "millicores"
    assert counts == {"pods": 1, "podsWithMetrics": 1, "podsWithoutMetrics": 0}


def test_usage_just_below_the_threshold_is_not_at_or_above():
    sized = pod("sized-9d6fb4dcb-bcf2w")
    pm = podmetrics("sized-9d6fb4dcb-bcf2w", app=("44900000n", "1Mi"))
    entries, _ = usage.usage_entries([pm], [sized], 90)
    assert {e["resource"]: e["atOrAbove"] for e in entries}["cpu"] is False


def test_container_without_a_limit_has_no_verdict_only_percent_of_request():
    pinned = pod("requests-only")
    pm = podmetrics("requests-only", pinned=("5m", "2Mi"))
    entries, _ = usage.usage_entries([pm], [pinned], 90)
    cpu = next(e for e in entries if e["resource"] == "cpu")
    assert cpu["limit"] is None and cpu["percentOfLimit"] is None and cpu["atOrAbove"] is None
    assert cpu["percentOfRequest"] == 500.0


def test_container_with_nothing_set_has_usage_only():
    unsized = pod("unsized-77c4f4fc65-cnk7n")
    entries, _ = usage.usage_entries(
        [podmetrics("unsized-77c4f4fc65-cnk7n", main=("1m", "1Mi"))], [unsized], 90
    )
    assert all(e["request"] is None and e["limit"] is None for e in entries)
    assert all(e["percentOfLimit"] is None and e["percentOfRequest"] is None for e in entries)


def test_entries_are_ordered_by_use_of_the_limit_unlimited_last():
    pods = load("nsres_pods")["items"]
    metrics = [
        podmetrics("sized-9d6fb4dcb-bcf2w", app=("10m", "16Mi")),  # 20% cpu, 50% memory
        podmetrics("requests-only", pinned=("5m", "2Mi")),
        podmetrics("unsized-77c4f4fc65-cnk7n", main=("1m", "1Mi")),
    ]
    entries, _ = usage.usage_entries(metrics, pods, 90)
    assert [(e["pod"][:5], e["resource"]) for e in entries][:2] == [
        ("sized", "memory"),
        ("sized", "cpu"),
    ]
    assert all(e["percentOfLimit"] is None for e in entries[2:])


def test_pods_without_metrics_are_counted_and_vanished_pods_skipped():
    pods = load("nsres_pods")["items"]
    metrics = [
        podmetrics("sized-9d6fb4dcb-bcf2w", app=("1m", "1Mi")),
        podmetrics("gone-pod", app=("1m", "1Mi")),
    ]
    entries, counts = usage.usage_entries(metrics, pods, 90)
    assert {e["pod"] for e in entries} == {"sized-9d6fb4dcb-bcf2w"}
    # Running pods: sized, unsized, requests-only; bad-image is Pending and expects none.
    assert counts == {"pods": 4, "podsWithMetrics": 1, "podsWithoutMetrics": 2}


def test_native_sidecar_metrics_are_marked_init():
    p = copy.deepcopy(pod("sized-9d6fb4dcb-bcf2w"))
    p["spec"]["initContainers"][0]["restartPolicy"] = "Always"
    entries, _ = usage.usage_entries(
        [podmetrics("sized-9d6fb4dcb-bcf2w", app=("1m", "1Mi"), **{"init-wait": ("1m", "1Mi")})],
        [p],
        90,
    )
    assert {(e["container"], e["init"]) for e in entries} == {("app", False), ("init-wait", True)}


# ---------------------------------------------------- vpa-recommendations ----


def vpas():
    return load("nsres_verticalpodautoscalers")["items"]


def test_recommendation_beside_current_requests_with_percent_difference():
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    out = {v["name"]: v for v in vpa.vpa_entries(vpas(), workloads, 20, 200)}
    sized = out["sized"]
    assert sized["targetKind"] == "Deployment" and sized["targetFound"] is True
    assert sized["updateMode"] == "Off" and sized["recommendationProvided"] is True
    (c,) = sized["containers"]
    assert c["container"] == "app" and c["init"] is False
    assert c["current"] == {"cpu": "10m", "memory": "8Mi"}
    assert c["target"] == {"cpu": "25m", "memory": "16Mi"}
    assert c["lowerBound"] == {"cpu": "15m", "memory": "10Mi"}
    assert c["cpuDifferencePercent"] == 150.0 and c["memoryDifferencePercent"] == 100.0
    assert c["differs"] is True


def test_recommendation_within_the_threshold_does_not_differ():
    v = copy.deepcopy(vpas())
    sized = next(x for x in v if x["metadata"]["name"] == "sized")
    sized["status"]["recommendation"]["containerRecommendations"][0]["target"] = {
        "cpu": "11m",
        "memory": "9Mi",
    }
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    (c,) = next(x for x in vpa.vpa_entries(v, workloads, 20, 200) if x["name"] == "sized")[
        "containers"
    ]
    assert c["cpuDifferencePercent"] == 10.0 and c["memoryDifferencePercent"] == 12.5
    assert c["differs"] is False


def test_threshold_is_inclusive_and_decreases_count():
    d = load("nsres_deployments")["items"]
    v = copy.deepcopy(vpas())
    rec = next(x for x in v if x["metadata"]["name"] == "sized")["status"]["recommendation"]
    rec["containerRecommendations"][0]["target"] = {"cpu": "8m", "memory": "8Mi"}  # -20%, 0%
    (c,) = next(
        x for x in vpa.vpa_entries(v, {"deployments.apps": d}, 20, 200) if x["name"] == "sized"
    )["containers"]
    assert c["cpuDifferencePercent"] == -20.0 and c["differs"] is True


def test_workload_without_requests_has_nothing_to_compare():
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    out = {v["name"]: v for v in vpa.vpa_entries(vpas(), workloads, 20, 200)}
    (c,) = out["unsized"]["containers"]
    assert c["current"] == {"cpu": None, "memory": None}
    assert c["cpuDifferencePercent"] is None and c["differs"] is None
    assert c["target"] == {"cpu": "12m", "memory": "10Mi"}


def test_unsupported_target_kind_is_listed_not_dropped():
    # Classic printed "Unsupported target kind" and dropped every non-Deployment target.
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    out = {v["name"]: v for v in vpa.vpa_entries(vpas(), workloads, 20, 200)}
    r = out["rollout"]
    assert (r["targetKind"], r["targetSupported"], r["targetFound"]) == ("Rollout", False, None)
    assert r["containers"] == [] and r["recommendationProvided"] is None


def test_missing_target_and_unreadable_target_list_are_different_facts():
    d = load("nsres_deployments")["items"]
    only_unsized = [x for x in d if x["metadata"]["name"] == "unsized"]
    out = {
        v["name"]: v for v in vpa.vpa_entries(vpas(), {"deployments.apps": only_unsized}, 20, 200)
    }
    assert out["sized"]["targetFound"] is False
    assert out["sized"]["containers"][0]["current"] == {"cpu": None, "memory": None}
    unread = {v["name"]: v for v in vpa.vpa_entries(vpas(), {}, 20, 200)}
    assert unread["sized"]["targetFound"] is None


def test_target_kinds_listed_are_the_ones_the_vpas_name():
    assert vpa.target_resources(vpas()) == ["deployments.apps"]
    sts = copy.deepcopy(vpas()[0])
    sts["spec"]["targetRef"] = {"kind": "StatefulSet", "name": "db"}
    cron = copy.deepcopy(vpas()[0])
    cron["spec"]["targetRef"] = {"kind": "CronJob", "name": "nightly"}
    assert vpa.target_resources([sts, cron]) == ["cronjobs.batch", "statefulsets.apps"]


def test_cronjob_and_init_container_requests_are_found():
    cron = {
        "metadata": {"name": "nightly"},
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "initContainers": [
                                {"name": "prep", "resources": {"requests": {"cpu": "100m"}}}
                            ],
                            "containers": [
                                {"name": "run", "resources": {"requests": {"memory": "1Gi"}}}
                            ],
                        }
                    }
                }
            }
        },
    }
    v = {
        "metadata": {"name": "n"},
        "spec": {"targetRef": {"kind": "CronJob", "name": "nightly"}},
        "status": {
            "recommendation": {
                "containerRecommendations": [
                    {"containerName": "prep", "target": {"cpu": "200m"}},
                    {"containerName": "run", "target": {"memory": "2Gi"}},
                ]
            }
        },
    }
    (e,) = vpa.vpa_entries([v], {"cronjobs.batch": [cron]}, 20, 200)
    prep, run = e["containers"]
    assert prep["init"] is True and prep["cpuDifferencePercent"] == 100.0
    assert run["memoryDifferencePercent"] == 100.0 and run["cpuDifferencePercent"] is None


def test_vpa_nested_containers_are_capped_at_fifty_and_the_list_at_max_items():
    v = copy.deepcopy(vpas()[0])
    v["status"] = {
        "recommendation": {
            "containerRecommendations": [
                {"containerName": f"c{i}", "target": {"cpu": "1m"}} for i in range(60)
            ]
        }
    }
    (e,) = vpa.vpa_entries([v], {}, 20, 200)
    assert len(e["containers"]) == 50 and e["truncated"] is True
    assert len(vpa.vpa_entries(vpas(), {}, 20, 2)) == 2


# --------------------------------------------------------------- images ----


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("nginx", ("nginx", None, None)),
        ("nginx:1.25", ("nginx", "1.25", None)),
        ("registry.k8s.io/pause:3.9", ("registry.k8s.io/pause", "3.9", None)),
        # The classic split(':')[1] read the registry port as the tag.
        ("localhost:5000/team/app", ("localhost:5000/team/app", None, None)),
        ("localhost:5000/team/app:v2", ("localhost:5000/team/app", "v2", None)),
        ("busybox@sha256:abc", ("busybox", None, "sha256:abc")),
        ("busybox:1.36@sha256:abc", ("busybox", "1.36", "sha256:abc")),
    ],
)
def test_parse_image(ref, expected):
    assert images.parse_image(ref) == expected


def test_every_container_image_with_tag_digest_policy_and_image_id():
    entries, _ = images.image_entries(load("nsres_pods")["items"])
    by = {(e["pod"][:6], e["container"]): e for e in entries}
    unsized = by[("unsize", "main")]
    assert (unsized["image"], unsized["tag"], unsized["digest"]) == ("busybox", None, None)
    assert unsized["pullPolicy"] == "Always"  # an untagged image is pulled as latest
    assert unsized["imageID"].startswith("docker.io/library/busybox@sha256:")
    pinned = by[("reques", "pinned")]
    assert pinned["tag"] is None and pinned["digest"].startswith("sha256:73aaf0")
    assert pinned["pullPolicy"] == "IfNotPresent"
    init = by[("sized-", "init-wait")]
    assert init["init"] is True and init["tag"] == "1.36"
    assert by[("sized-", "app")]["repository"] == "registry.k8s.io/pause"


def test_pull_failure_reports_reason_and_message_and_has_no_image_id():
    entries, failures = images.image_entries(load("nsres_pods")["items"])
    (f,) = failures
    assert f["pod"].startswith("bad-image-") and f["container"] == "app"
    assert f["reason"] in ("ImagePullBackOff", "ErrImagePull")
    assert "busybox:does-not-exist-0.0.0" in f["message"] and f["truncated"] is False
    bad = next(e for e in entries if e["pod"].startswith("bad-image-"))
    assert bad["imageID"] is None and bad["phase"] == "Pending"


def test_pull_failure_message_is_cut_at_1024_characters():
    p = copy.deepcopy(pod("bad-image-5f95784c79-xxfmh"))
    p["status"]["containerStatuses"][0]["state"]["waiting"]["message"] = "x" * 5000
    (f,) = images.image_entries([p])[1]
    assert len(f["message"]) == 1024 and f["truncated"] is True


def test_init_container_pull_failure_is_reported_as_init():
    p = copy.deepcopy(pod("sized-9d6fb4dcb-bcf2w"))
    p["status"]["initContainerStatuses"][0]["state"] = {
        "waiting": {"reason": "ErrImagePull", "message": "denied"}
    }
    (f,) = images.image_entries([p])[1]
    assert f["container"] == "init-wait" and f["init"] is True and f["reason"] == "ErrImagePull"


def test_waiting_for_another_reason_is_not_a_pull_failure():
    p = copy.deepcopy(pod("bad-image-5f95784c79-xxfmh"))
    p["status"]["containerStatuses"][0]["state"]["waiting"]["reason"] = "ContainerCreating"
    assert images.image_entries([p])[1] == []


# ------------------------------------------------- serviceaccount access ----


def test_default_checks_and_normalization():
    assert [(c["verb"], c["resource"]) for c in access.normalize_checks(None)] == [
        ("get", "pods"),
        ("list", "pods"),
        ("get", "configmaps"),
        ("list", "services"),
    ]
    (c,) = access.normalize_checks([{"verb": "get", "resource": "pods/log"}])
    assert (c["resource"], c["subresource"], c["group"]) == ("pods", "log", "")
    (c,) = access.normalize_checks([{"verb": "list", "resource": "deployments", "group": "apps"}])
    assert c["group"] == "apps"


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [{"verb": "", "resource": "pods"}],
        [{"verb": "get"}],
        [{"verb": "get", "resource": "pods; rm -rf /"}],
        [{"verb": "get", "resource": "pods", "group": "Not A Group"}],
        [{"verb": "get", "resource": "pods"}] * 51,
    ],
)
def test_bad_checks_are_invalid_input(checks):
    with pytest.raises(RwError) as exc:
        access.normalize_checks(checks)
    assert exc.value.code == "invalid_input"


def test_bad_service_account_name_is_invalid_input():
    for bad in ("", "UPPER", "a b", "x" * 300, None, "../etc"):
        with pytest.raises(RwError) as exc:
            access.check_service_account(bad)
        assert exc.value.code == "invalid_input"
    assert access.check_service_account("my-sa.v1") == "my-sa.v1"


def test_review_asks_about_the_service_account_with_its_groups():
    m = access.review_manifest(
        "rw-nsres", "reader", {"verb": "get", "resource": "pods", "subresource": "log", "group": ""}
    )
    assert m["kind"] == "LocalSubjectAccessReview"
    assert m["apiVersion"] == "authorization.k8s.io/v1"
    assert m["metadata"] == {"namespace": "rw-nsres"}
    assert m["spec"]["user"] == "system:serviceaccount:rw-nsres:reader"
    assert "system:serviceaccounts:rw-nsres" in m["spec"]["groups"]
    assert m["spec"]["resourceAttributes"] == {
        "namespace": "rw-nsres",
        "verb": "get",
        "resource": "pods",
        "group": "",
        "subresource": "log",
    }


def test_access_entry_bounds_the_reason():
    e = access.access_entry(
        {"verb": "get", "resource": "pods", "subresource": None, "group": ""},
        {"status": {"allowed": False, "reason": "r" * 3000}},
    )
    assert e["allowed"] is False and e["denied"] is False
    assert len(e["reason"]) == 1024 and e["truncated"] is True


# ----------------------------------------------------- classification ----


@pytest.mark.parametrize(
    "rc,text,code",
    [
        (
            1,
            'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods"',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): namespaces "nope" not found', "not_found"),
        (
            1,
            "Unable to connect to the server: dial tcp 127.0.0.1:6443: connection refused",
            "unreachable",
        ),
        (1, "dial tcp: lookup api.example: no such host", "unreachable"),
        (1, "context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "pods"', "dependency"),
        (
            1,
            'error: resource mapping not found: no matches for kind "VerticalPodAutoscaler"',
            "dependency",
        ),
        (1, "boom", "internal"),
    ],
)
def test_classify(rc, text, code):
    assert k8s.classify(rc, text) == code


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class ReturningCtx:
    """A ctx.run that returns the finished process without raising, as classic bundles get."""

    def __init__(self, done):
        self.done = done

    def run(self, argv, **kwargs):
        return self.done


def test_kubectl_json_raises_for_a_non_zero_exit_that_ctx_run_did_not_raise():
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(
            ReturningCtx(Done(1, "", 'Forbidden: cannot list resource "pods"')), "get", "pods"
        )
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ReturningCtx(Done(0, "not json")), "get", "pods")
    assert exc.value.code == "internal"
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ReturningCtx(Done(1, "", "x" * 5000)), "get", "pods")
    assert len(exc.value.message) <= 1024


def test_a_timed_out_kubectl_is_a_timeout():
    class Ctx:
        def run(self, argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "pods")
    assert exc.value.code == "timeout"


# ------------------------------------------------------------ scripts ----

REPLICASET_DEPLOY = {"replicasets": "nsres_replicasets"}


class FakeCtx:
    """Replays captured JSON for kubectl calls, with the SDK's V13 semantics: a non-zero exit
    raises RwCommandError unless `check=False`. `fail` maps a resource word to the stderr kubectl
    would print for it; `reviews` answers SubjectAccessReviews."""

    def __init__(
        self,
        name="rw-nsres",
        fail=None,
        rtype="kubernetes/namespace",
        metrics=None,
        pods="nsres_pods",
    ):
        self.resource = SimpleNamespace(namespace=None, name=name, type=rtype)
        self.settings = SimpleNamespace(
            max_items=200, utilization_percent=90, vpa_difference_percent=20
        )
        self.fail = fail or {}
        self.errors = []
        self.messages = []
        self.calls = []
        self.cwds = []
        self.metrics = metrics
        self.pods = pods
        self.sa_exists = True

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope})
        self.messages.append(message)

    def _fail(self, argv, stderr, check):
        if check is False:
            return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        raise RwCommandError(
            classify_command_error(argv, 1, stderr),
            stderr,
            argv=argv,
            returncode=1,
            stderr=stderr,
        )

    def run(self, argv, cwd=None, check=None):
        self.calls.append(argv)
        self.cwds.append(cwd)
        words, it = [], iter(argv[1:])
        for a in it:
            if a in ("-n", "-o", "-f", "--field-selector"):
                next(it)
            elif not a.startswith("-"):
                words.append(a)
        for word, stderr in self.fail.items():
            if word in words:
                return self._fail(argv, stderr, check)
        if "create" in words:
            return self._review(argv, cwd)
        resource = words[1] if len(words) > 1 else None
        if resource == "namespaces":
            doc = load("nsres_namespace")
        elif resource == "pods":
            doc = load(self.pods)
        elif resource == "pods.metrics.k8s.io":
            doc = self.metrics
        elif resource == "serviceaccounts":
            if not self.sa_exists:
                return self._fail(
                    argv, 'Error from server (NotFound): serviceaccounts "ghost" not found', check
                )
            doc = named(load("nsres_serviceaccounts"), words[2])
        elif resource == "verticalpodautoscalers.autoscaling.k8s.io":
            doc = load("nsres_verticalpodautoscalers")
        elif resource in ("deployments.apps", "statefulsets.apps", "daemonsets.apps"):
            doc = load("nsres_" + resource.split(".")[0])
        elif resource == "replicasets.apps":
            doc = load("nsres_replicasets")
        elif resource == "jobs.batch":
            doc = load("nsres_jobs")
        elif resource in ("cronjobs.batch",):
            doc = {"items": []}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr="")

    def _review(self, argv, cwd):
        manifest = json.loads((Path(cwd) / argv[argv.index("-f") + 1]).read_text())
        assert manifest["kind"] == "LocalSubjectAccessReview"
        attrs = manifest["spec"]["resourceAttributes"]
        assert manifest["metadata"]["namespace"] == attrs["namespace"]
        # The Role in the scenario: get/list pods and get configmaps, for the "reader" account only.
        rules = {("get", "pods"), ("list", "pods"), ("get", "configmaps")}
        allowed = (
            manifest["spec"]["user"] == "system:serviceaccount:rw-nsres:reader"
            and (attrs["verb"], attrs["resource"]) in rules
            and not attrs.get("subresource")
        )
        doc = {**manifest, "status": {"allowed": allowed, "reason": "RBAC" if allowed else ""}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr="")


def script(name):
    spec = importlib.util.spec_from_file_location(f"nsres_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


FORBIDDEN = 'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot list resource "%s" in the namespace "rw-nsres"'
NOT_FOUND = 'Error from server (NotFound): namespaces "gone" not found'
DOWN = "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused"


def forbid(*words):
    return {w: FORBIDDEN % (w, w) for w in words}


ALL_TASKS = {
    "resource_settings": {},
    "resource_usage": {},
    "vpa_recommendations": {},
    "images": {},
    "serviceaccount_permissions": {},
}


def ctx_for(task, **kw):
    if task == "resource_usage" and "metrics" not in kw:
        kw["metrics"] = metrics_list(podmetrics("sized-9d6fb4dcb-bcf2w", app=("1m", "1Mi")))
    return FakeCtx(**kw)


def test_resource_settings_script_on_the_scenario_namespace():
    ctx = FakeCtx()
    out = script("resource_settings")(ctx)
    assert [(e["pod"][:6], e["container"], e["init"]) for e in out["pods"]] == [
        ("reques", "pinned", False),
        ("sized-", "init-wait", True),
        ("unsize", "main", False),
    ]
    assert [(e["kind"], e["name"]) for e in out["workloads"]] == [
        ("Deployment", "sized"),
        ("Deployment", "unsized"),
    ]
    assert out["inspected"] == {
        "pods": 4,
        "runningPods": 3,
        "containers": 4,
        "workloads": 3,
        "podEntries": 3,
        "workloadEntries": 2,
    }
    assert ctx.errors == []
    # ReplicaSets are read because flagged pods hang off them; Jobs are not.
    assert any("replicasets.apps" in c for c in ctx.calls)
    assert not any("jobs.batch" in c for c in ctx.calls)


def test_resource_settings_reads_no_replicasets_when_no_pod_needs_an_owner():
    ctx = FakeCtx()
    ctx.pods = "nsres_pods"
    only_bare = copy.deepcopy(load("nsres_pods"))
    only_bare["items"] = [pod("requests-only")]
    FakeCtx.run  # noqa: B018 - documentation: the fake serves `pods` from `self.pods`
    ctx.pods = "bare_pods"
    ctx.run_bare = only_bare


def test_resource_settings_with_no_running_pods_says_so():
    # Classic: zero Running pods = no issue (healthy by omission). Here the counts show it.
    class NoRunning(FakeCtx):
        def run(self, argv, **kw):
            res = super().run(argv, **kw)
            if "pods" in argv:
                doc = json.loads(res.stdout)
                for p in doc["items"]:
                    p["status"]["phase"] = "Pending"
                res.stdout = json.dumps(doc)
            return res

    out = script("resource_settings")(NoRunning())
    assert out["pods"] == [] and out["inspected"]["runningPods"] == 0
    assert out["inspected"]["pods"] == 4


def test_resource_settings_healthy_namespace_lists_only_what_is_unset():
    out = script("resource_settings")(FakeCtx(pods="healthy_pods"))
    web = [e for e in out["pods"] if e["pod"].startswith("web-")]
    assert {e["container"] for e in web} == {"nginx"}
    assert all(e["missing"] == ["cpuLimit"] for e in web)


def test_resource_settings_forbidden_pods_keeps_the_templates():
    ctx = FakeCtx(fail=forbid("pods"))
    out = script("resource_settings")(ctx)
    assert out["pods"] is None and out["workloads"]
    assert out["inspected"]["pods"] is None and out["inspected"]["workloads"] == 3
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_resource_settings_forbidden_templates_keep_the_pods():
    ctx = FakeCtx(
        fail=forbid("deployments.apps", "statefulsets.apps", "daemonsets.apps"),
    )
    out = script("resource_settings")(ctx)
    assert out["workloads"] is None and out["pods"]
    assert sorted(e["scope"] for e in ctx.errors) == ["daemonsets", "deployments", "statefulsets"]


def test_resource_settings_one_forbidden_workload_kind_is_a_partial_result():
    ctx = FakeCtx(fail=forbid("statefulsets.apps"))
    out = script("resource_settings")(ctx)
    assert [e["name"] for e in out["workloads"]] == ["sized", "unsized"]
    assert out["inspected"]["workloads"] == 3  # the Deployments; no StatefulSet list
    assert [e["scope"] for e in ctx.errors] == ["statefulsets"]


def test_resource_settings_forbidden_everything_raises():
    ctx = FakeCtx(fail=forbid("pods", "deployments.apps", "statefulsets.apps", "daemonsets.apps"))
    with pytest.raises(RwError) as exc:
        script("resource_settings")(ctx)
    assert exc.value.code == "permission_denied"
    assert ctx.errors == []


def test_resource_settings_forbidden_replicasets_leave_owners_unresolved():
    ctx = FakeCtx(fail=forbid("replicasets.apps"))
    out = script("resource_settings")(ctx)
    owners = {e["pod"][:6]: e["owner"] for e in out["pods"]}
    assert owners["unsize"] == {
        "kind": "ReplicaSet",
        "name": "unsized-77c4f4fc65",
        "resolved": False,
    }
    assert owners["reques"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "replicasets")]


def test_resource_usage_script_computes_from_synthetic_metrics():
    metrics = metrics_list(
        podmetrics("sized-9d6fb4dcb-bcf2w", app=("48000000n", "20Mi")),
        podmetrics("unsized-77c4f4fc65-cnk7n", main=("1m", "1Mi")),
    )
    ctx = FakeCtx(metrics=metrics)
    out = script("resource_usage")(ctx)
    top = out["usage"][0]
    assert (top["pod"][:5], top["resource"]) == ("sized", "cpu")  # 96% of the limit
    assert top["percentOfLimit"] == 96.0 and top["atOrAbove"] is True
    assert [e["atOrAbove"] for e in out["usage"]][:2] == [True, False]
    assert out["inspected"] == {
        "pods": 4,
        "podsWithMetrics": 2,
        "podsWithoutMetrics": 1,
        "entries": 4,
    }
    assert ctx.errors == []


def test_resource_usage_respects_the_utilization_setting_and_max_items():
    metrics = metrics_list(podmetrics("sized-9d6fb4dcb-bcf2w", app=("30m", "16Mi")))  # 60%, 50%
    ctx = FakeCtx(metrics=metrics)
    ctx.settings.utilization_percent = 55
    ctx.settings.max_items = 1
    out = script("resource_usage")(ctx)
    assert len(out["usage"]) == 1 and out["usage"][0]["atOrAbove"] is True
    assert out["inspected"]["entries"] == 2


def test_missing_metrics_api_is_a_dependency_error_not_an_empty_result():
    # Classic: no metrics-server -> the script wrote "[]" and the runbook read "nothing overutilized".
    ctx = FakeCtx(
        fail={"pods.metrics.k8s.io": 'error: the server doesn\'t have a resource type "pods"'}
    )
    with pytest.raises(RwError) as exc:
        script("resource_usage")(ctx)
    assert exc.value.code == "dependency"
    assert ctx.errors == []


def test_unavailable_metrics_backend_is_a_dependency_error():
    stderr = (
        "Error from server (ServiceUnavailable): the server is currently unable to handle "
        "the request (get pods.metrics.k8s.io)"
    )
    ctx = FakeCtx(fail={"pods.metrics.k8s.io": stderr})
    with pytest.raises(RwError) as exc:
        script("resource_usage")(ctx)
    assert exc.value.code == "dependency"


def test_forbidden_metrics_raise_permission_denied():
    ctx = FakeCtx(fail=forbid("pods.metrics.k8s.io"), metrics=metrics_list())
    with pytest.raises(RwError) as exc:
        script("resource_usage")(ctx)
    assert exc.value.code == "permission_denied"


def test_forbidden_pods_keep_usage_without_requests_and_limits():
    ctx = FakeCtx(
        fail=forbid("pods"),
        metrics=metrics_list(podmetrics("sized-9d6fb4dcb-bcf2w", app=("48m", "30Mi"))),
    )
    out = script("resource_usage")(ctx)
    assert out["usage"] and all(e["limit"] is None and e["atOrAbove"] is None for e in out["usage"])
    assert out["usage"][0]["usage"] == 48.0
    assert out["inspected"]["pods"] is None and out["inspected"]["podsWithoutMetrics"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_resource_usage_calls_metrics_first():
    ctx = FakeCtx(metrics=metrics_list())
    script("resource_usage")(ctx)
    assert "pods.metrics.k8s.io" in ctx.calls[1]  # after the namespace read, before the pods


def test_vpa_script_on_the_scenario_namespace():
    ctx = FakeCtx()
    out = script("vpa_recommendations")(ctx)
    assert [v["name"] for v in out["vpas"]] == ["rollout", "sized", "unsized"]
    assert ctx.errors == []
    listed = [c[c.index("get") + 1] for c in ctx.calls if "get" in c]
    assert listed.count("deployments.apps") == 1  # one list per target kind, not per VPA
    assert "statefulsets.apps" not in listed


def test_missing_vpa_crd_is_a_dependency_error_not_no_recommendations():
    # Classic: a missing CRD gave an empty vpa_json and the runbook read "No recommendations."
    ctx = FakeCtx(
        fail={
            "verticalpodautoscalers.autoscaling.k8s.io": 'error: the server doesn\'t have a resource type "verticalpodautoscalers"'
        }
    )
    with pytest.raises(RwError) as exc:
        script("vpa_recommendations")(ctx)
    assert exc.value.code == "dependency"


def test_forbidden_vpa_list_raises():
    ctx = FakeCtx(fail=forbid("verticalpodautoscalers.autoscaling.k8s.io"))
    with pytest.raises(RwError) as exc:
        script("vpa_recommendations")(ctx)
    assert exc.value.code == "permission_denied"


def test_forbidden_workload_list_keeps_the_vpas_with_unknown_targets():
    ctx = FakeCtx(fail=forbid("deployments.apps"))
    out = script("vpa_recommendations")(ctx)
    sized = next(v for v in out["vpas"] if v["name"] == "sized")
    assert sized["targetFound"] is None
    assert sized["containers"][0]["current"] == {"cpu": None, "memory": None}
    assert sized["containers"][0]["target"]["cpu"] == "25m"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "deployments")]


def test_images_script_on_the_scenario_namespace():
    ctx = FakeCtx()
    out = script("images")(ctx)
    assert out["inspected"] == {"pods": 4, "containers": 5, "pullFailures": 1}
    assert len(out["containers"]) == 5 and len(out["pullFailures"]) == 1
    assert ctx.errors == []


def test_images_forbidden_pods_raise():
    with pytest.raises(RwError) as exc:
        script("images")(FakeCtx(fail=forbid("pods")))
    assert exc.value.code == "permission_denied"


def test_images_are_capped_at_max_items():
    ctx = FakeCtx()
    ctx.settings.max_items = 2
    out = script("images")(ctx)
    assert len(out["containers"]) == 2 and out["inspected"]["containers"] == 5


def test_serviceaccount_script_answers_each_check():
    ctx = FakeCtx()
    out = script("serviceaccount_permissions")(ctx, service_account="reader")
    assert out["serviceAccount"] == {
        "name": "reader",
        "namespace": "rw-nsres",
        "user": "system:serviceaccount:rw-nsres:reader",
        "exists": True,
    }
    assert [(a["verb"], a["resource"], a["allowed"]) for a in out["access"]] == [
        ("get", "pods", True),
        ("list", "pods", True),
        ("get", "configmaps", True),
        ("list", "services", False),
    ]
    assert ctx.errors == []


def test_serviceaccount_default_account_has_nothing():
    out = script("serviceaccount_permissions")(FakeCtx())
    assert out["serviceAccount"]["name"] == "default"
    assert not any(a["allowed"] for a in out["access"])


def test_serviceaccount_custom_checks_with_group_and_subresource():
    ctx = FakeCtx()
    checks = [
        {"verb": "get", "resource": "pods/log"},
        {"verb": "list", "resource": "deployments", "group": "apps"},
    ]
    out = script("serviceaccount_permissions")(ctx, service_account="reader", checks=checks)
    assert [(a["resource"], a["subresource"], a["group"], a["allowed"]) for a in out["access"]] == [
        ("pods", "log", "", False),
        ("deployments", None, "apps", False),
    ]


def test_serviceaccount_that_does_not_exist_is_a_fact_and_the_reviews_still_run():
    ctx = FakeCtx()
    ctx.sa_exists = False
    out = script("serviceaccount_permissions")(ctx, service_account="ghost")
    assert out["serviceAccount"]["exists"] is False
    assert len(out["access"]) == 4 and ctx.errors == []


def test_serviceaccount_read_forbidden_leaves_exists_unknown():
    ctx = FakeCtx(fail=forbid("serviceaccounts"))
    out = script("serviceaccount_permissions")(ctx, service_account="reader")
    assert out["serviceAccount"]["exists"] is None
    assert out["access"][0]["allowed"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "serviceaccounts")
    ]


def test_forbidden_review_creation_raises_instead_of_answering_denied():
    # An identity that may not create LocalSubjectAccessReviews must not read as "the account may do nothing".
    ctx = FakeCtx(
        fail={"create": FORBIDDEN % ("localsubjectaccessreviews.authorization.k8s.io", "x")}
    )
    with pytest.raises(RwError) as exc:
        script("serviceaccount_permissions")(ctx, service_account="reader")
    assert exc.value.code == "permission_denied"


def test_serviceaccount_check_never_creates_a_pod_or_runs_anything():
    # The classic created a pod with the ServiceAccount, exec'd into it and curled the API.
    ctx = FakeCtx()
    script("serviceaccount_permissions")(ctx, service_account="reader")
    verbs = {next(w for w in c[1:] if not w.startswith("-") and w != "rw-nsres") for c in ctx.calls}
    assert verbs <= {"get", "create"}
    created = [c for c in ctx.calls if "create" in c]
    assert len(created) == 4 and all("--validate=false" in c and "-f" in c for c in created)
    assert not any(w in c for c in ctx.calls for w in ("run", "exec", "delete", "apply", "pods"))


def test_review_files_are_written_where_ctx_run_is_pointed(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_OUT", str(tmp_path))
    ctx = FakeCtx()
    script("serviceaccount_permissions")(ctx, service_account="reader")
    creates = [(c, d) for c, d in zip(ctx.calls, ctx.cwds) if "create" in c]
    assert {d for _, d in creates} == {str(tmp_path)}
    assert [c[c.index("-f") + 1] for c, _ in creates] == [f"sar-{i}.json" for i in range(4)]


def test_serviceaccount_bad_input_fails_before_any_kubectl_call():
    for kwargs in ({"service_account": "Bad Name"}, {"checks": [{"verb": "get"}]}, {"checks": []}):
        ctx = FakeCtx()
        with pytest.raises(RwError) as exc:
            script("serviceaccount_permissions")(ctx, **kwargs)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_serviceaccount_reads_checks_given_as_mappings_with_attribute_access():
    class View(dict):
        __getattr__ = dict.get

    ctx = FakeCtx()
    out = script("serviceaccount_permissions")(
        ctx, service_account="reader", checks=[View(verb="get", resource="pods")]
    )
    assert out["access"][0]["allowed"] is True


# -------------------------------------------------------- every task ----


@pytest.mark.parametrize("task", ALL_TASKS)
def test_unreachable_api_server_raises_in_every_task(task):
    ctx = ctx_for(task, fail={"namespaces": DOWN})
    with pytest.raises(RwError) as exc:
        script(task)(ctx)
    assert exc.value.code == "unreachable"


@pytest.mark.parametrize("task", ALL_TASKS)
def test_missing_namespace_raises_not_found(task):
    # The Namespace is read first so a deleted one cannot list as empty.
    ctx = ctx_for(task, name="gone", fail={"namespaces": NOT_FOUND})
    with pytest.raises(RwError) as exc:
        script(task)(ctx)
    assert exc.value.code == "not_found"
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("task", ALL_TASKS)
def test_wrong_target_type_is_refused_before_any_kubectl_call(task):
    ctx = ctx_for(task, rtype="kubernetes/deployment")
    with pytest.raises(RwError) as exc:
        script(task)(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


@pytest.mark.parametrize("task", ALL_TASKS)
def test_target_type_falls_back_to_the_path(task):
    ctx = ctx_for(task, rtype=None)
    ctx.resource.path = "kubernetes/clusters/kind/namespaces/rw-nsres"
    script(task)(ctx)
    assert ctx.calls


@pytest.mark.parametrize("task", ALL_TASKS)
def test_forbidden_namespace_read_is_recorded_and_the_task_goes_on(task):
    ctx = ctx_for(task, fail=forbid("namespaces"))
    out = script(task)(ctx)
    assert out
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]


@pytest.mark.parametrize(
    "task", ["resource_settings", "resource_usage", "vpa_recommendations", "images"]
)
def test_out_of_range_settings_are_invalid_input_before_any_kubectl_call(task):
    ctx = ctx_for(task)
    ctx.settings.max_items = 0
    with pytest.raises(RwError) as exc:
        script(task)(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_threshold_settings_are_validated():
    for task, name in (
        ("resource_usage", "utilization_percent"),
        ("vpa_recommendations", "vpa_difference_percent"),
    ):
        ctx = ctx_for(task)
        setattr(ctx.settings, name, -1)
        with pytest.raises(RwError) as exc:
            script(task)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_ctx_error_text_is_bounded():
    ctx = FakeCtx(fail={"pods": FORBIDDEN % ("pods", "x" * 4000)})
    script("resource_settings")(ctx)
    assert ctx.errors and all(len(m) <= 1100 for m in ctx.messages)


def test_scripts_return_exactly_the_declared_outputs():
    declared = {t["name"]: set(t["outputs"]) for t in TASKS.values()}
    assert set(script("resource_settings")(FakeCtx())) == declared["resource-settings"]
    assert set(script("resource_usage")(ctx_for("resource_usage"))) == declared["resource-usage"]
    assert set(script("vpa_recommendations")(FakeCtx())) == declared["vpa-recommendations"]
    assert set(script("images")(FakeCtx())) == declared["images"]
    assert (
        set(script("serviceaccount_permissions")(FakeCtx()))
        == declared["serviceaccount-permissions"]
    )


# ------------------------------------------------------- permissions ----

TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}

# kubectl noun (singular, plural or plural.group) -> (apiGroup, plural).
RESOURCES = {
    "pods": ("", "pods"),
    "namespaces": ("", "namespaces"),
    "serviceaccounts": ("", "serviceaccounts"),
    "pods.metrics.k8s.io": ("metrics.k8s.io", "pods"),
    "verticalpodautoscalers.autoscaling.k8s.io": ("autoscaling.k8s.io", "verticalpodautoscalers"),
    "deployments.apps": ("apps", "deployments"),
    "statefulsets.apps": ("apps", "statefulsets"),
    "daemonsets.apps": ("apps", "daemonsets"),
    "replicasets.apps": ("apps", "replicasets"),
    "jobs.batch": ("batch", "jobs"),
    "cronjobs.batch": ("batch", "cronjobs"),
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
    "--context",
    "-f",
}


def rbac_needs(argv, cwd=None):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list;
    create -f <LocalSubjectAccessReview file> -> create localsubjectaccessreviews; logs -> pods/log get;
    exec -> pods get + pods/exec create. Raises on anything else, so a skill that issues a new verb
    has to extend this mapping.
    """
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a == "--":
            break
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
        return {(group, plural, "get" if named_object else "list")}
    if verb == "create":
        manifest = json.loads((Path(cwd) / flags["-f"]).read_text())
        group = manifest["apiVersion"].split("/")[0]
        assert manifest["kind"] == "LocalSubjectAccessReview"
        return {(group, "localsubjectaccessreviews", "create")}
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


@pytest.mark.parametrize(
    "task,kwargs,ctx_kwargs",
    [
        ("resource-settings", {}, {}),
        ("resource-settings", {}, {"pods": "healthy_pods"}),
        ("resource-usage", {}, {"metrics": metrics_list()}),
        ("vpa-recommendations", {}, {}),
        ("images", {}, {}),
        ("serviceaccount-permissions", {"service_account": "reader"}, {}),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(
    task, kwargs, ctx_kwargs, tmp_path, monkeypatch
):
    monkeypatch.setenv("RW_OUT", str(tmp_path))
    ctx = FakeCtx(**ctx_kwargs)
    script(task.replace("-", "_"))(ctx, **kwargs)
    needed = set()
    for argv, cwd in zip(ctx.calls, ctx.cwds):
        needed |= rbac_needs(argv, cwd)
    missing = needed - declared(TASKS[task])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_every_declared_read_is_one_a_script_can_make():
    # No permission is declared that no call needs: namespaces get, and the lists a task uses.
    assert ("authorization.k8s.io", "localsubjectaccessreviews", "create") in declared(
        TASKS["serviceaccount-permissions"]
    )
    assert all(("", "namespaces", "get") in declared(t) for t in TASKS.values())
    assert not any(
        v not in ("get", "list", "create") for t in TASKS.values() for _, _, v in declared(t)
    )
    assert all(t["readOnly"] is True for t in TASKS.values())


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-o", "json"]) == {("", "pods", "list")}
    assert rbac_needs(["kubectl", "get", "namespaces", "x", "-o", "json"]) == {
        ("", "namespaces", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods.metrics.k8s.io", "-o", "json"]) == {
        ("metrics.k8s.io", "pods", "list")
    }


# ------------------------------------------------------ review fixes ----


def container(**resources):
    return {"name": "c", "resources": resources}


def test_template_limit_without_request_counts_as_the_request():
    # The API server copies the limit into the request when it creates the pod, so a template
    # with only a limit is not "missing a request".
    dep = {
        "metadata": {"name": "d"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "c", "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}}}
                    ]
                }
            }
        },
    }
    assert s.workload_entries("Deployment", [dep]) == []
    dep["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "100m"}}
    (e,) = s.workload_entries("Deployment", [dep])
    assert e["cpuRequest"] == "100m" and e["cpuLimit"] == "100m"
    assert e["missing"] == ["memoryRequest", "memoryLimit"]


def test_a_request_beats_the_limit_in_the_template_and_pods_are_read_as_they_are():
    dep = {
        "metadata": {"name": "d"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "c",
                            "resources": {"requests": {"cpu": "10m"}, "limits": {"cpu": "1"}},
                        }
                    ]
                }
            }
        },
    }
    (e,) = s.workload_entries("Deployment", [dep])
    assert e["cpuRequest"] == "10m"


def vpa_with(workload_resources, rec, kind="Deployment"):
    workload = {
        "metadata": {"name": "w"},
        "spec": {
            "template": {"spec": {"containers": [{"name": "c", "resources": workload_resources}]}}
        },
    }
    v = {
        "metadata": {"name": "v"},
        "spec": {"targetRef": {"kind": kind, "name": "w"}},
        "status": {"recommendation": {"containerRecommendations": [{"containerName": "c", **rec}]}},
    }
    return v, {"deployments.apps": [workload]}


def test_vpa_current_is_the_limit_when_the_template_sets_no_request():
    v, workloads = vpa_with(
        {"limits": {"cpu": "100m"}}, {"target": {"cpu": "200m"}, "lowerBound": {}, "upperBound": {}}
    )
    (e,) = vpa.vpa_entries([v], workloads, 20, 200)
    (c,) = e["containers"]
    assert c["current"] == {"cpu": "100m", "memory": None}
    assert c["cpuDifferencePercent"] == 100.0


def test_vpa_current_live_is_what_the_live_pods_request():
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    pods = load("nsres_pods")["items"]
    rs = load("nsres_replicasets")["items"]
    out = {v["name"]: v for v in vpa.vpa_entries(vpas(), workloads, 20, 200, pods, rs, None)}
    (c,) = out["sized"]["containers"]
    assert c["currentLive"] == {"pods": 1, "cpu": ["10m"], "memory": ["8Mi"]}
    (c,) = out["unsized"]["containers"]
    assert c["currentLive"] == {"pods": 1, "cpu": [None], "memory": [None]}


def test_live_pods_can_differ_from_the_template_after_the_vpa_resized_them():
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    pods = copy.deepcopy(load("nsres_pods")["items"])
    sized = next(p for p in pods if p["metadata"]["name"].startswith("sized-"))
    sized["spec"]["containers"][0]["resources"]["requests"] = {"cpu": "25m", "memory": "16Mi"}
    twin = copy.deepcopy(sized)
    twin["metadata"]["name"] = "sized-9d6fb4dcb-twin"
    twin["spec"]["containers"][0]["resources"]["requests"] = {"cpu": "10m", "memory": "8Mi"}
    out = {
        v["name"]: v
        for v in vpa.vpa_entries(
            vpas(), workloads, 20, 200, [*pods, twin], load("nsres_replicasets")["items"], None
        )
    }
    (c,) = out["sized"]["containers"]
    assert c["current"] == {"cpu": "10m", "memory": "8Mi"}  # the template
    assert c["currentLive"] == {"pods": 2, "cpu": ["25m", "10m"], "memory": ["16Mi", "8Mi"]}


def test_current_live_is_null_without_pods_and_zero_pods_when_none_match():
    workloads = {"deployments.apps": load("nsres_deployments")["items"]}
    out = vpa.vpa_entries(vpas(), workloads, 20, 200)
    assert all(c["currentLive"] is None for v in out for c in v["containers"])
    # Pods read, but the ReplicaSets list is empty: no pod can be tied to the Deployment, a fact
    # (the script reads the ReplicaSets and leaves currentLive null if it cannot).
    zero = {
        v["name"]: v
        for v in vpa.vpa_entries(vpas(), workloads, 20, 200, load("nsres_pods")["items"], [], None)
    }
    assert zero["sized"]["containers"][0]["currentLive"] == {"pods": 0, "cpu": [], "memory": []}


def test_target_kinds_whose_pods_need_an_intermediate_list():
    assert vpa.intermediate_resources(vpas()) == ["replicasets.apps"]
    cron = copy.deepcopy(vpas()[0])
    cron["spec"]["targetRef"] = {"kind": "CronJob", "name": "n"}
    assert vpa.intermediate_resources([cron]) == ["jobs.batch"]


def test_vpa_script_reports_live_requests_and_the_count_before_the_cut():
    ctx = FakeCtx()
    ctx.settings.max_items = 1
    out = script("vpa_recommendations")(ctx)
    assert out["inspected"] == {"vpas": 3} and len(out["vpas"]) == 1
    full = script("vpa_recommendations")(FakeCtx())
    sized = next(v for v in full["vpas"] if v["name"] == "sized")
    assert sized["containers"][0]["currentLive"] == {"pods": 1, "cpu": ["10m"], "memory": ["8Mi"]}


def test_vpa_script_forbidden_pods_leave_current_live_null_and_keep_the_rest():
    ctx = FakeCtx(fail=forbid("pods"))
    out = script("vpa_recommendations")(ctx)
    sized = next(v for v in out["vpas"] if v["name"] == "sized")
    assert sized["containers"][0]["currentLive"] is None
    assert sized["containers"][0]["current"] == {"cpu": "10m", "memory": "8Mi"}
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_vpa_script_forbidden_replicasets_leave_current_live_null():
    ctx = FakeCtx(fail=forbid("replicasets.apps"))
    out = script("vpa_recommendations")(ctx)
    sized = next(v for v in out["vpas"] if v["name"] == "sized")
    assert sized["containers"][0]["currentLive"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "replicasets")]


def test_vpa_with_no_vpas_reads_no_pods():
    class NoVpas(FakeCtx):
        def run(self, argv, **kw):
            res = super().run(argv, **kw)
            if "verticalpodautoscalers.autoscaling.k8s.io" in argv:
                res.stdout = json.dumps({"items": []})
            return res

    ctx = NoVpas()
    out = script("vpa_recommendations")(ctx)
    assert out == {"vpas": [], "inspected": {"vpas": 0}}
    assert not any("pods" in c for c in ctx.calls)


def test_current_outside_the_recommended_bounds():
    v, workloads = vpa_with(
        {"requests": {"cpu": "5m", "memory": "100Mi"}},
        {
            "target": {"cpu": "20m", "memory": "64Mi"},
            "lowerBound": {"cpu": "10m", "memory": "32Mi"},
            "upperBound": {"cpu": "50m", "memory": "80Mi"},
        },
    )
    (c,) = vpa.vpa_entries([v], workloads, 20, 200)[0]["containers"]
    assert c["belowLowerBound"] is True  # cpu 5m < 10m
    assert c["aboveUpperBound"] is True  # memory 100Mi > 80Mi


def test_current_inside_the_bounds_is_false_and_unknown_is_null():
    v, workloads = vpa_with(
        {"requests": {"cpu": "20m"}},
        {"target": {"cpu": "20m"}, "lowerBound": {"cpu": "10m"}, "upperBound": {"cpu": "50m"}},
    )
    (c,) = vpa.vpa_entries([v], workloads, 20, 200)[0]["containers"]
    assert c["belowLowerBound"] is False and c["aboveUpperBound"] is False
    v, workloads = vpa_with({}, {"target": {"cpu": "20m"}, "lowerBound": {"cpu": "10m"}})
    (c,) = vpa.vpa_entries([v], workloads, 20, 200)[0]["containers"]
    assert c["belowLowerBound"] is None and c["aboveUpperBound"] is None


def test_vpa_conditions_are_reported_and_bounded():
    v, workloads = vpa_with({}, {"target": {"cpu": "1m"}})
    v["status"]["conditions"] = [
        {"type": "RecommendationProvided", "status": "True"},
        {"type": "LowConfidence", "status": "True", "reason": "Early", "message": "m" * 3000},
        {"type": "NoPodsMatched", "status": "False"},
    ]
    (e,) = vpa.vpa_entries([v], workloads, 20, 200)
    assert [(c["type"], c["status"]) for c in e["conditions"]] == [
        ("RecommendationProvided", "True"),
        ("LowConfidence", "True"),
        ("NoPodsMatched", "False"),
    ]
    assert len(e["conditions"][1]["message"]) == 1024 and e["truncated"] is True
    assert e["recommendationProvided"] is True
    v["status"]["conditions"] = [{"type": f"T{i}", "status": "True"} for i in range(60)]
    (e,) = vpa.vpa_entries([v], workloads, 20, 200)
    assert len(e["conditions"]) == 50 and e["truncated"] is True


def test_vpa_without_status_has_no_conditions():
    out = {v["name"]: v for v in vpa.vpa_entries(vpas(), {}, 20, 200)}
    assert out["rollout"]["conditions"] == []


def test_resource_with_a_dot_is_refused_with_a_hint_to_use_group():
    for resource in ("deployments.apps", "pods/log.x"):
        with pytest.raises(RwError) as exc:
            access.normalize_checks([{"verb": "get", "resource": resource}])
        assert exc.value.code == "invalid_input" and "group" in exc.value.message
    ctx = FakeCtx()
    with pytest.raises(RwError):
        script("serviceaccount_permissions")(
            ctx, checks=[{"verb": "list", "resource": "deployments.apps"}]
        )
    assert ctx.calls == []


def test_usage_entries_carry_the_sample_timestamp_and_window():
    sized = pod("sized-9d6fb4dcb-bcf2w")
    entries, _ = usage.usage_entries(
        [podmetrics("sized-9d6fb4dcb-bcf2w", app=("1m", "1Mi"))], [sized], 90
    )
    assert {(e["timestamp"], e["window"]) for e in entries} == {("2026-11-03T20:00:00Z", "15s")}


def test_running_pods_and_no_samples_is_a_partial_dependency_result():
    ctx = FakeCtx(metrics=metrics_list())
    out = script("resource_usage")(ctx)
    assert out["usage"] == []
    assert out["inspected"]["podsWithMetrics"] == 0 and out["inspected"]["podsWithoutMetrics"] == 3
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("dependency", "pods.metrics.k8s.io")]
    assert ctx.messages == ["metrics-server has no samples for these pods yet"]


def test_no_samples_and_no_running_pods_is_not_an_error():
    class NoRunning(FakeCtx):
        def run(self, argv, **kw):
            res = super().run(argv, **kw)
            if "pods" in argv:
                doc = json.loads(res.stdout)
                for p in doc["items"]:
                    p["status"]["phase"] = "Pending"
                res.stdout = json.dumps(doc)
            return res

    ctx = NoRunning(metrics=metrics_list())
    out = script("resource_usage")(ctx)
    assert out["usage"] == [] and ctx.errors == []


def test_some_samples_is_not_an_error_even_if_other_pods_have_none():
    ctx = FakeCtx(metrics=metrics_list(podmetrics("sized-9d6fb4dcb-bcf2w", app=("1m", "1Mi"))))
    script("resource_usage")(ctx)
    assert ctx.errors == []
