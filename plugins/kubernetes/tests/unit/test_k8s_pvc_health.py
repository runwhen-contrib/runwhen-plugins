"""Unit tests for k8s-pvc-health's lib/ and scripts against JSON captured from
the kind-rw-plugins scenarios (plugins/kubernetes/tests/kind). The cluster
state was captured at one moment; `NOW` is a fixed point just after it."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from k8s_pvc_health import k8s
from k8s_pvc_health.errors import RwError
from k8s_pvc_health import pvc as p

SKILL = Path(__file__).parents[2] / "skills" / "k8s-pvc-health"
DATA = Path(__file__).parent / "data" / "k8s_pvc_health"
NOW = datetime(2026, 11, 3, 18, 28, tzinfo=timezone.utc)


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def claim(ns, name):
    return named(load(f"{ns}_pvc"), name)


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert p.parse_duration("90s").total_seconds() == 90
    assert p.parse_duration("1h30m").total_seconds() == 5400
    assert p.parse_duration("500ms").total_seconds() == 0.5
    assert p.parse_duration("1m500ms").total_seconds() == 60.5
    for bad in ("", "soon", "10", "5x"):
        with pytest.raises(RwError) as exc:
            p.parse_duration(bad)
        assert exc.value.code == "invalid_input"


def test_parse_quantity_understands_every_suffix_not_only_gi_ti_pi():
    # The classic check converted only Gi/Ti/Pi, so a 100Mi claim was 0 and never flagged.
    assert p.parse_quantity("100Mi") == 100 * 2**20
    assert p.parse_quantity("512Ki") == 512 * 1024
    assert p.parse_quantity("1.5Gi") == 1610612736
    assert p.parse_quantity("500M") == 500_000_000
    assert p.parse_quantity("2G") == 2_000_000_000
    assert p.parse_quantity("1Ti") == 2**40
    assert p.parse_quantity("1e3") == 1000
    assert p.parse_quantity("1024") == 1024
    assert p.parse_quantity(None) is None
    with pytest.raises(ValueError):
        p.parse_quantity("lots")


# ------------------------------------------------------- binding-status ----


def test_pending_claim_reports_phase_and_missing_storage_class():
    view = p.claim_view(claim("broken", "pending"))
    assert view["phase"] == "Pending"
    assert view["storageClassName"] == "does-not-exist"
    assert view["volumeName"] is None
    assert view["capacity"]["requested"] == "100Mi"
    assert view["capacity"]["bound"] is None and view["capacity"]["boundBelowRequested"] is False
    sc = p.storage_class_view("does-not-exist", None, looked=True)
    assert sc["exists"] is False
    assert p.volume_view(claim("broken", "pending"), None, looked=True) is None


def test_bound_claim_reports_class_volume_and_capacity():
    data = claim("healthy", "data")
    view = p.claim_view(data)
    assert view["phase"] == "Bound"
    assert view["accessModes"] == ["ReadWriteOnce"]
    assert view["capacity"] == {
        "requested": "100Mi",
        "requestedBytes": 100 * 2**20,
        "bound": "100Mi",
        "boundBytes": 100 * 2**20,
        "boundBelowRequested": False,
    }
    sc = p.storage_class_view("standard", named(load("storageclasses"), "standard"), looked=True)
    assert sc["exists"] is True and sc["isDefault"] is True
    assert sc["provisioner"] == "rancher.io/local-path"
    assert sc["volumeBindingMode"] == "WaitForFirstConsumer"
    pv = named(load("pvs"), data["spec"]["volumeName"])
    vol = p.volume_view(data, pv, looked=True)
    assert vol["exists"] is True and vol["phase"] == "Bound"
    assert vol["capacity"] == "100Mi" and vol["reclaimPolicy"] == "Delete"
    assert vol["claimRefMatches"] is True and vol["terminating"] is False
    assert vol["finalizers"]


def test_bound_below_requested_when_a_resize_is_pending():
    c = copy.deepcopy(claim("healthy", "data"))
    c["spec"]["resources"]["requests"]["storage"] = "1Gi"
    c["status"]["conditions"] = [
        {"type": "FileSystemResizePending", "status": "True", "message": "waiting"}
    ]
    view = p.claim_view(c)
    assert view["capacity"]["boundBelowRequested"] is True
    assert view["conditions"][0]["type"] == "FileSystemResizePending"


def test_volume_deletion_is_read_from_its_deletion_timestamp_not_a_phase():
    # Classic "List PersistentVolumes in Terminating State" matched phase == Terminating,
    # which a PV never has. A PV being deleted still reports phase Bound.
    data = claim("healthy", "data")
    pv = copy.deepcopy(named(load("pvs"), data["spec"]["volumeName"]))
    pv["metadata"]["deletionTimestamp"] = "2026-11-03T18:20:00Z"
    vol = p.volume_view(data, pv, looked=True)
    assert vol["phase"] == "Bound"
    assert vol["terminating"] is True
    assert vol["deletionTimestamp"] == "2026-11-03T18:20:00Z"


def test_claim_ref_mismatch_and_missing_volume_are_facts():
    data = claim("healthy", "data")
    pv = copy.deepcopy(named(load("pvs"), data["spec"]["volumeName"]))
    pv["spec"]["claimRef"]["name"] = "someone-else"
    assert p.volume_view(data, pv, looked=True)["claimRefMatches"] is False
    gone = p.volume_view(data, None, looked=True)
    assert gone["exists"] is False and gone["phase"] is None


def test_unread_cluster_objects_are_null_not_false():
    data = claim("healthy", "data")
    sc = p.storage_class_view("standard", None, looked=False)
    assert sc["name"] == "standard" and sc["exists"] is None
    vol = p.volume_view(data, None, looked=False)
    assert (
        vol["name"] == data["spec"]["volumeName"] and vol["exists"] is None and vol["phase"] is None
    )


def test_claim_without_a_class_does_not_look_one_up():
    sc = p.storage_class_view(None, None, looked=True)
    assert sc["name"] is None and sc["exists"] is None


def test_events_for_the_pending_claim_are_grouped_and_in_window():
    events = load("broken_events")["items"]
    rows = p.claim_events(events, claim("broken", "pending"), "1h", NOW, 200)
    assert [(r["type"], r["reason"]) for r in rows] == [("Warning", "ProvisioningFailed")]
    assert rows[0]["count"] == 102 + 102 + 10
    assert rows[0]["firstSeen"] == "2026-11-03T17:33:03Z"
    assert rows[0]["lastSeen"] == "2026-11-03T18:27:00Z"
    assert "does-not-exist" in rows[0]["message"]
    assert rows[0]["reportingComponent"] == "persistentvolume-controller"


def test_waiting_for_first_consumer_is_a_normal_event_not_a_warning():
    events = load("broken_events")["items"]
    rows = p.claim_events(events, claim("broken", "stuck"), "1h", NOW, 200)
    assert [(r["type"], r["reason"]) for r in rows] == [("Normal", "WaitForFirstConsumer")]


def test_events_outside_the_window_or_of_another_claim_are_left_out():
    events = load("broken_events")["items"]
    pending = claim("broken", "pending")
    assert (
        p.claim_events(
            events, pending, "1m", datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc), 200
        )
        == []
    )
    recreated = copy.deepcopy(pending)
    recreated["metadata"]["uid"] = "a-different-uid"
    assert p.claim_events(events, recreated, "1h", NOW, 200) == []


def test_healthy_claim_events_are_all_normal():
    events = load("healthy_events")["items"]
    rows = p.claim_events(events, claim("healthy", "data"), "2h", NOW, 200)
    assert {r["type"] for r in rows} == {"Normal"}
    assert "ProvisioningSucceeded" in {r["reason"] for r in rows}
    assert len(p.claim_events(events, claim("healthy", "data"), "2h", NOW, 2)) == 2


# ---------------------------------------------------------- terminating ----


def test_stuck_claim_is_terminating_with_its_finalizers_and_age():
    t = p.termination(claim("broken", "stuck"), NOW)
    assert t["terminating"] is True
    assert t["deletionTimestamp"] == "2026-11-03T18:26:55Z"
    assert t["terminatingSeconds"] == 65
    assert "rw.test/hold" in t["finalizers"]
    assert t["phase"] == "Pending"


def test_live_claim_is_not_terminating():
    t = p.termination(claim("healthy", "data"), NOW)
    assert t["terminating"] is False
    assert t["deletionTimestamp"] is None and t["terminatingSeconds"] is None
    assert "kubernetes.io/pvc-protection" in t["finalizers"]


def test_pods_mounting_the_claim_by_name_or_by_statefulset_template():
    pods = load("healthy_pods")["items"]
    data = p.mounting_pods(pods, "data")
    assert [m["name"] for m in data] == [
        n for n in [x["metadata"]["name"] for x in pods] if n.startswith("data-user")
    ]
    pod = data[0]
    assert pod["phase"] == "Running" and pod["ready"] is True and pod["active"] is True
    assert pod["mounts"] == [
        {
            "container": "pause",
            "init": False,
            "volume": "data",
            "mountPath": "/data",
            "readOnly": False,
        }
    ]
    # db-0 names its volume "data" but the claim is data-db-0.
    assert [m["name"] for m in p.mounting_pods(pods, "data-db-0")] == ["db-0"]
    assert p.mounting_pods(pods, "pending") == []


def test_finished_pods_still_listed_but_no_longer_hold_the_claim():
    pods = copy.deepcopy(load("healthy_pods")["items"])
    for pod in pods:
        if pod["metadata"]["name"].startswith("data-user"):
            pod["status"]["phase"] = "Succeeded"
    row = p.mounting_pods(pods, "data")[0]
    assert row["phase"] == "Succeeded" and row["active"] is False


def test_generic_ephemeral_volume_claims_are_found_by_their_derived_name():
    pods = copy.deepcopy(load("healthy_pods")["items"])
    pod = pods[0]
    pod["spec"]["volumes"] = [
        {"name": "scratch", "ephemeral": {"volumeClaimTemplate": {"spec": {}}}}
    ]
    pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "scratch", "mountPath": "/scratch"}]
    rows = p.mounting_pods(pods, f"{pod['metadata']['name']}-scratch")
    assert [r["name"] for r in rows] == [pod["metadata"]["name"]]
    assert rows[0]["mounts"][0]["mountPath"] == "/scratch"


# ---------------------------------------------------------------- usage ----


def test_pick_usage_target_prefers_a_running_ready_pod_with_a_running_container():
    pods = load("healthy_pods")["items"]
    mounting = p.mounting_pods(pods, "data")
    objects = {x["metadata"]["name"]: x for x in pods}
    pod, container, path = p.pick_usage_target(mounting, objects)
    assert pod.startswith("data-user") and container == "pause" and path == "/data"


def test_pick_usage_target_is_none_without_a_running_pod():
    pods = copy.deepcopy(load("healthy_pods")["items"])
    for pod in pods:
        pod["status"]["phase"] = "Pending"
    mounting = p.mounting_pods(pods, "data")
    assert p.pick_usage_target(mounting, {x["metadata"]["name"]: x for x in pods}) is None
    assert p.pick_usage_target([], {}) is None


def test_pick_usage_target_skips_a_terminating_pod():
    pods = copy.deepcopy(load("healthy_pods")["items"])
    for pod in pods:
        pod["metadata"]["deletionTimestamp"] = "2026-11-03T18:00:00Z"
    mounting = p.mounting_pods(pods, "data")
    assert p.pick_usage_target(mounting, {x["metadata"]["name"]: x for x in pods}) is None


DF = """Filesystem     1024-blocks    Used Available Capacity Mounted on
/dev/sdb1           1000000  850000    150000      85% /data
"""


def test_parse_df_posix_output():
    df = p.parse_df(DF)
    assert df == {
        "filesystem": "/dev/sdb1",
        "mountedOn": "/data",
        "sizeBytes": 1000000 * 1024,
        "usedBytes": 850000 * 1024,
        "availableBytes": 150000 * 1024,
    }


@pytest.mark.parametrize(
    "bad", ["", "Filesystem 1024-blocks\n", "df: /data: No such file or directory\n", "a\nb c d\n"]
)
def test_unreadable_df_output_raises_rather_than_reporting_zero_usage(bad):
    with pytest.raises(ValueError):
        p.parse_df(bad)


def test_usage_at_or_above_threshold_is_a_boolean_from_the_setting():
    df = p.parse_df(DF)
    view = p.usage_view(df, "web-0", "app", "/data", 1, 85)
    assert view["percent"] == 85.0 and view["atOrAbove"] is True and view["thresholdPercent"] == 85
    assert p.usage_view(df, "web-0", "app", "/data", 1, 90)["atOrAbove"] is False
    below = p.parse_df(DF.replace("850000    150000", "849000    151000"))
    assert (
        p.usage_view(below, "web-0", "app", "/data", 1, 85)["atOrAbove"] is False
    )  # 84.9, df would print 85%


def test_small_volumes_are_measured_too():
    # Classic flagged only Gi/Ti/Pi claims; a 100Mi volume that is full must be at or above.
    df = p.parse_df(
        "Filesystem 1024-blocks Used Available Capacity Mounted on\ntmpfs 102400 102000 400 100% /data\n"
    )
    assert p.usage_view(df, "web-0", "app", "/data", 1, 85)["atOrAbove"] is True


def test_zero_size_filesystem_has_no_percent():
    df = p.parse_df(
        "Filesystem 1024-blocks Used Available Capacity Mounted on\nnone 0 0 0 - /data\n"
    )
    view = p.usage_view(df, "web-0", "app", "/data", 1, 85)
    assert view["percent"] is None and view["atOrAbove"] is None


# ----------------------------------------------------------- attachment ----


def mounting_on(nodes, phase="Running", terminating=False):
    """Mounting-pod rows for a claim, one per node."""
    return [
        {
            "name": f"p{i}",
            "phase": phase,
            "ready": True,
            "node": n,
            "active": phase not in ("Succeeded", "Failed"),
            "terminating": terminating,
            "mounts": [],
        }
        for i, n in enumerate(nodes)
    ]


def test_rwo_claim_on_one_node_has_no_conflict():
    data = claim("healthy", "data")
    pods = p.mounting_pods(load("healthy_pods")["items"], "data")
    access = p.attachment_access(data, pods)
    assert access["readWriteOnce"] is True
    assert access["nodes"] == ["rw-plugins-control-plane"]
    assert access["multiNodeConflict"] is False
    assert access["volumeName"] == data["spec"]["volumeName"]


def test_rwo_claim_mounted_from_two_nodes_is_a_conflict():
    data = claim("healthy", "data")
    access = p.attachment_access(data, mounting_on(["n1", "n2"]))
    assert access["nodes"] == ["n1", "n2"] and access["multiNodeConflict"] is True


def test_finished_pods_and_shared_access_modes_are_not_conflicts():
    data = copy.deepcopy(claim("healthy", "data"))
    assert (
        p.attachment_access(data, mounting_on(["n1"]) + mounting_on(["n2"], phase="Succeeded"))[
            "multiNodeConflict"
        ]
        is False
    )
    data["spec"]["accessModes"] = ["ReadWriteOnce", "ReadWriteMany"]
    access = p.attachment_access(data, mounting_on(["n1", "n2"]))
    assert access["readWriteOnce"] is False and access["multiNodeConflict"] is False


def va(name, pv, node, attached, attach_error=None):
    status = {"attached": attached}
    if attach_error:
        status["attachError"] = {"message": attach_error, "time": "2026-11-03T18:00:00Z"}
    return {
        "metadata": {"name": name},
        "spec": {
            "attacher": "ebs.csi.aws.com",
            "nodeName": node,
            "source": {"persistentVolumeName": pv},
        },
        "status": status,
    }


def test_volume_attachments_for_the_pv_with_state_and_errors():
    items = [
        va("csi-2", "pv-a", "n2", False, "rpc error: volume is attached to another node"),
        va("csi-1", "pv-a", "n1", True),
        va("csi-3", "pv-other", "n1", True),
    ]
    rows = p.volume_attachments(items, "pv-a", ["n1"])
    assert [r["name"] for r in rows] == ["csi-1", "csi-2"]
    assert (
        rows[0]["attached"] is True
        and rows[0]["attachError"] is None
        and rows[0]["nodeHasMountingPod"] is True
    )
    assert rows[1]["attached"] is False and "another node" in rows[1]["attachError"]
    assert rows[1]["nodeHasMountingPod"] is False


def test_captured_cluster_has_no_volume_attachments():
    # kind's local-path volumes are not attached through CSI: an empty list is the true answer here.
    assert p.volume_attachments(load("volumeattachments")["items"], "pvc-x", []) == []


# ------------------------------------------------------ error handling ----


@pytest.mark.parametrize(
    "rc,text,code",
    [
        (
            1,
            'Error from server (Forbidden): storageclasses "standard" is forbidden: User "x" cannot get resource',
            "permission_denied",
        ),
        (
            1,
            'Error from server (NotFound): storageclasses.storage.k8s.io "x" not found',
            "not_found",
        ),
        (
            1,
            "Unable to connect to the server: dial tcp 10.0.0.1:443: connect: connection refused",
            "unreachable",
        ),
        (1, "Unable to connect to the server: dial tcp: i/o timeout", "unreachable"),
        (
            1,
            "Unable to connect to the server: x509: certificate signed by unknown authority",
            "unreachable",
        ),
        (1, "dial tcp: lookup api.example: no such host", "unreachable"),
        (1, "context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "volumeattachments"', "dependency"),
        (
            126,
            'OCI runtime exec failed: exec failed: unable to start container process: exec: "df": executable file not found in $PATH',
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


class RunCtx:
    """Like the installed SDK's ctx.run: returns the CompletedProcess, never raises on a non-zero exit."""

    def __init__(self, done):
        self.done = done

    def run(self, argv):
        return self.done


def test_kubectl_json_raises_rw_error_for_a_non_zero_exit_that_ctx_run_did_not_raise():
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(
            RunCtx(
                Done(
                    1,
                    "",
                    'Error from server (Forbidden): pods is forbidden: cannot list resource "pods"',
                )
            ),
            "get",
            "pods",
        )
    assert exc.value.code == "permission_denied"


def test_kubectl_json_rejects_output_that_is_not_json():
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(RunCtx(Done(0, "not json")), "get", "pods")
    assert exc.value.code == "internal"


def test_run_checked_message_is_bounded():
    with pytest.raises(RwError) as exc:
        k8s.run_checked(RunCtx(Done(1, "", "x" * 5000)), ["kubectl", "get"])
    assert len(exc.value.message) < 1100


# ------------------------------------------------------------ scripts ----


class Skipped(Exception):
    pass


class FakeCtx:
    """Replays captured JSON for kubectl calls; `fail` maps a resource word to the stderr kubectl would print."""

    def __init__(
        self,
        ns,
        name,
        fail=None,
        exec_out=DF,
        pods="healthy_pods",
        rtype="kubernetes/persistentvolumeclaim",
    ):
        self.resource = SimpleNamespace(namespace=ns, name=name, type=rtype)
        self.settings = SimpleNamespace(max_items=200, usage_threshold_percent=85)
        self.fail = fail or {}
        self.errors = []
        self.messages = []
        self.calls = []
        self.exec_out = exec_out
        self.pods = pods

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope})
        self.messages.append(message)

    def skip(self, reason):
        raise Skipped(reason)

    def run(self, argv):
        self.calls.append(argv)
        words, it = [], iter(argv[1:])
        for a in it:
            if a in ("-n", "-o", "-c", "--field-selector"):
                next(it)
            elif a == "--":
                words.append("exec-args")
                break
            elif not a.startswith("-"):
                words.append(a)
        for word, stderr in self.fail.items():
            if word in words:
                return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        ns = self.resource.namespace.split("-")[1]
        if "exec" in argv:
            return SimpleNamespace(returncode=0, stdout=self.exec_out, stderr="")
        if "persistentvolumeclaims" in words:
            doc = claim(ns, words[-1])
        elif "pods" in words:
            doc = load(self.pods)
        elif "events" in words:
            doc = load(f"{ns}_events")
        elif "storageclass" in words:
            doc = named(load("storageclasses"), words[-1])
        elif "persistentvolume" in words:
            doc = named(load("pvs"), words[-1])
        elif "volumeattachments.storage.k8s.io" in words:
            doc = load("volumeattachments")
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr="")


def script(name):
    spec = importlib.util.spec_from_file_location(f"pvc_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


FORBIDDEN = 'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot get resource'


def test_binding_status_script_on_the_pending_claim():
    ctx = FakeCtx("rw-broken", "pending", pods="healthy_pods")
    # the fake has no StorageClass "does-not-exist": kubectl would say NotFound
    ctx.fail = {
        "storageclass": 'Error from server (NotFound): storageclass.storage.k8s.io "does-not-exist" not found'
    }
    out = script("binding_status")(ctx, since="2h")
    assert out["claim"]["phase"] == "Pending"
    assert out["storageClass"]["exists"] is False
    assert out["volume"] is None
    assert [e["reason"] for e in out["events"]] == ["ProvisioningFailed"]
    assert ctx.errors == []


def test_binding_status_script_on_the_healthy_claim():
    out = script("binding_status")(FakeCtx("rw-healthy", "data"), since="2h")
    assert out["claim"]["phase"] == "Bound"
    assert out["storageClass"]["exists"] is True
    assert out["volume"]["phase"] == "Bound"


def test_namespace_scoped_identity_still_gets_the_claim_and_events():
    ctx = FakeCtx(
        "rw-healthy",
        "data",
        fail={
            "storageclass": FORBIDDEN % "storageclasses",
            "persistentvolume": FORBIDDEN % "persistentvolumes",
        },
    )
    out = script("binding_status")(ctx, since="2h")
    assert out["claim"]["phase"] == "Bound"
    assert out["events"]
    assert out["storageClass"]["exists"] is None and out["volume"]["exists"] is None
    assert sorted(e["scope"] for e in ctx.errors) == ["persistentvolumes", "storageclasses"]
    assert {e["code"] for e in ctx.errors} == {"permission_denied"}


def test_forbidden_events_are_an_error_not_an_empty_list():
    ctx = FakeCtx("rw-healthy", "data", fail={"events": FORBIDDEN % "events"})
    out = script("binding_status")(ctx)
    assert out["events"] is None and out["claim"]["phase"] == "Bound"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "events")]


def test_forbidden_claim_raises():
    ctx = FakeCtx(
        "rw-broken",
        "pending",
        fail={"persistentvolumeclaims": FORBIDDEN % "persistentvolumeclaims"},
    )
    with pytest.raises(RwError) as exc:
        script("binding_status")(ctx)
    assert exc.value.code == "permission_denied"
    for task in ("terminating", "usage", "attachment"):
        with pytest.raises(RwError) as exc:
            script(task)(
                FakeCtx("rw-broken", "pending", fail={"persistentvolumeclaims": FORBIDDEN % "pvc"})
            )
        assert exc.value.code == "permission_denied"


def test_unreachable_cluster_object_read_raises_instead_of_degrading():
    ctx = FakeCtx(
        "rw-healthy", "data", fail={"storageclass": "Unable to connect to the server: i/o timeout"}
    )
    with pytest.raises(RwError) as exc:
        script("binding_status")(ctx)
    assert exc.value.code == "unreachable"
    assert ctx.errors == []


def test_unreachable_api_server_raises_in_every_task():
    down = "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused"
    for task in ("binding_status", "terminating", "usage", "attachment"):
        ctx = FakeCtx("rw-healthy", "data", fail={"persistentvolumeclaims": down})
        with pytest.raises(RwError) as exc:
            script(task)(ctx)
        assert exc.value.code == "unreachable"


def test_missing_claim_raises():
    ctx = FakeCtx(
        "rw-healthy",
        "gone",
        fail={
            "persistentvolumeclaims": 'Error from server (NotFound): persistentvolumeclaims "gone" not found'
        },
    )
    for task in ("binding_status", "terminating", "usage", "attachment"):
        with pytest.raises(RwError) as exc:
            script(task)(ctx)
        assert exc.value.code == "not_found"


def test_wrong_target_type_is_refused_before_any_kubectl_call():
    ctx = FakeCtx("rw-healthy", "web", rtype="kubernetes/deployment")
    with pytest.raises(RwError) as exc:
        script("terminating")(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_target_type_falls_back_to_the_path():
    ctx = FakeCtx("rw-healthy", "data", rtype=None)
    ctx.resource.path = "kubernetes/clusters/kind/namespaces/rw-healthy/persistentvolumeclaims/data"
    assert script("terminating")(ctx)["termination"]["name"] == "data"


def test_bad_since_is_invalid_input_before_any_kubectl_call():
    ctx = FakeCtx("rw-healthy", "data")
    with pytest.raises(RwError) as exc:
        script("binding_status")(ctx, since="yesterday")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_events_are_requested_by_claim_uid():
    ctx = FakeCtx("rw-healthy", "data")
    script("binding_status")(ctx, since="2h")
    selector = next(c for c in ctx.calls if "--field-selector" in c)[-3]
    assert f"involvedObject.uid={claim('healthy', 'data')['metadata']['uid']}" in selector


def test_ctx_error_text_is_bounded():
    ctx = FakeCtx("rw-healthy", "data", fail={"storageclass": FORBIDDEN % ("x" * 4000)})
    script("binding_status")(ctx, since="2h")
    assert ctx.errors  # reported, not raised
    assert all(len(m) <= 1100 for m in ctx.messages)


def test_usage_runs_nothing_but_the_df_exec():
    ctx = FakeCtx("rw-healthy", "data")
    script("usage")(ctx)
    execs = [c for c in ctx.calls if "exec" in c]
    assert len(execs) == 1 and execs[0][-3:] == ["df", "-Pk", "/data"]


def test_terminating_script_on_the_stuck_claim():
    out = script("terminating")(FakeCtx("rw-broken", "stuck"))
    assert out["termination"]["terminating"] is True
    assert "rw.test/hold" in out["termination"]["finalizers"]
    assert out["pods"] == []


def test_terminating_script_lists_pods_that_still_mount_the_claim():
    out = script("terminating")(FakeCtx("rw-healthy", "data"))
    assert [x["name"][:9] for x in out["pods"]] == ["data-user"]


def test_usage_script_runs_df_in_the_mounting_container():
    ctx = FakeCtx("rw-healthy", "data")
    out = script("usage")(ctx)["usage"]
    exec_call = next(c for c in ctx.calls if "exec" in c)
    assert exec_call[-3:] == ["df", "-Pk", "/data"] and "-c" in exec_call and "pause" in exec_call
    assert out["percent"] == 85.0 and out["atOrAbove"] is True and out["runningPods"] == 1


def test_usage_script_skips_when_no_running_pod_mounts_the_claim():
    with pytest.raises(Skipped, match="no running pod mounts this claim"):
        script("usage")(FakeCtx("rw-broken", "pending"))


def test_forbidden_exec_fails_the_usage_task():
    ctx = FakeCtx("rw-healthy", "data", fail={"exec": FORBIDDEN % "pods/exec"})
    with pytest.raises(RwError) as exc:
        script("usage")(ctx)
    assert exc.value.code == "permission_denied"


def test_container_without_df_fails_the_usage_task_as_dependency():
    gone = 'OCI runtime exec failed: exec: "df": executable file not found in $PATH'
    with pytest.raises(RwError) as exc:
        script("usage")(FakeCtx("rw-healthy", "data", fail={"exec": gone}))
    assert exc.value.code == "dependency"


def test_attachment_script_reports_access_pods_and_empty_attachments():
    out = script("attachment")(FakeCtx("rw-healthy", "data"))
    assert out["access"]["multiNodeConflict"] is False
    assert [x["name"][:9] for x in out["pods"]] == ["data-user"]
    assert out["volumeAttachments"] == []


def test_forbidden_volume_attachment_list_is_null_with_an_error_not_empty():
    ctx = FakeCtx(
        "rw-healthy",
        "data",
        fail={"volumeattachments.storage.k8s.io": FORBIDDEN % "volumeattachments"},
    )
    out = script("attachment")(ctx)
    assert out["volumeAttachments"] is None
    assert out["access"]["nodes"] == ["rw-plugins-control-plane"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "volumeattachments")
    ]


def test_unbound_claim_has_nothing_to_attach():
    out = script("attachment")(FakeCtx("rw-broken", "pending"))
    assert out["volumeAttachments"] == [] and out["access"]["volumeName"] is None


# ------------------------------------------------------------- bounds ----


def test_free_text_is_cut_at_1024_characters_with_a_flag():
    assert p.trunc("short") == ("short", False)
    text, cut = p.trunc("y" * 3000)
    assert len(text) == 1024 and cut is True
    assert p.trunc(None) == (None, False)


def test_event_and_condition_and_attachment_messages_are_bounded():
    events = copy.deepcopy(load("broken_events")["items"])
    for ev in events:
        ev["message"] = "m" * 5000
    rows = p.claim_events(events, claim("broken", "pending"), "1h", NOW, 200)
    assert len(rows[0]["message"]) == 1024 and rows[0]["truncated"] is True
    short = p.claim_events(
        load("broken_events")["items"], claim("broken", "pending"), "1h", NOW, 200
    )
    assert short[0]["truncated"] is False
    c = copy.deepcopy(claim("healthy", "data"))
    c["status"]["conditions"] = [{"type": "Resizing", "status": "True", "message": "z" * 2000}]
    cond = p.claim_view(c)["conditions"][0]
    assert len(cond["message"]) == 1024 and cond["truncated"] is True
    rows = p.volume_attachments([va("a", "pv", "n1", False, "e" * 3000)], "pv", [])
    assert len(rows[0]["attachError"]) == 1024 and rows[0]["truncated"] is True


def test_nested_lists_are_capped_at_fifty():
    c = copy.deepcopy(claim("healthy", "data"))
    c["metadata"]["finalizers"] = [f"f/{i}" for i in range(80)]
    assert len(p.termination(c, NOW)["finalizers"]) == 50
    pods = copy.deepcopy(load("healthy_pods")["items"])
    pod = next(x for x in pods if x["metadata"]["name"].startswith("data-user"))
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "data", "mountPath": f"/data/{i}"} for i in range(80)
    ]
    row = next(r for r in p.mounting_pods(pods, "data") if r["name"] == pod["metadata"]["name"])
    assert len(row["mounts"]) == 50


def test_init_containers_are_reported_with_init_true():
    pods = copy.deepcopy(load("healthy_pods")["items"])
    pod = next(x for x in pods if x["metadata"]["name"].startswith("data-user"))
    pod["spec"]["initContainers"] = [
        {"name": "prep", "image": "x", "volumeMounts": [{"name": "data", "mountPath": "/seed"}]}
    ]
    row = next(r for r in p.mounting_pods(pods, "data") if r["name"] == pod["metadata"]["name"])
    assert [(m["container"], m["init"]) for m in row["mounts"]] == [
        ("pause", False),
        ("prep", True),
    ]


# ----------------------------------------------------- review fixes ----

SCRIPT_FOR_TASK = {
    t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
}

# kubectl noun (singular, plural or plural.group) -> (apiGroup, plural). Extend it when you copy this.
RESOURCES = {
    **{
        r: ("", r)
        for r in (
            "pods",
            "events",
            "persistentvolumeclaims",
            "persistentvolumes",
            "nodes",
            "namespaces",
        )
    },
    "pod": ("", "pods"),
    "persistentvolume": ("", "persistentvolumes"),
    "storageclass": ("storage.k8s.io", "storageclasses"),
    "storageclasses": ("storage.k8s.io", "storageclasses"),
    "volumeattachments.storage.k8s.io": ("storage.k8s.io", "volumeattachments"),
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
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list;
    logs -> pods/log get; exec -> pods get + pods/exec create. Raises on anything else, so a
    skill that issues a new verb has to extend this mapping.
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
    "task,targets",
    [
        ("binding-status", [("rw-healthy", "data"), ("rw-broken", "pending")]),
        ("terminating", [("rw-broken", "stuck"), ("rw-healthy", "data")]),
        ("usage", [("rw-healthy", "data")]),
        ("attachment", [("rw-healthy", "data"), ("rw-broken", "pending")]),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, targets):
    needed = set()
    for ns, name in targets:
        ctx = FakeCtx(ns, name)
        if name == "pending":  # its StorageClass does not exist
            ctx.fail = {
                "storageclass": 'Error from server (NotFound): storageclass "does-not-exist" not found'
            }
        script(task.replace("-", "_"))(ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    missing = needed - declared(SCRIPT_FOR_TASK[task])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_usage_declares_pod_get_and_exec_create_and_get():
    perms = declared(SCRIPT_FOR_TASK["usage"])
    assert {("", "pods", "get"), ("", "pods/exec", "create"), ("", "pods/exec", "get")} <= perms


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-o", "json"]) == {("", "pods", "list")}
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "get", "storageclass", "standard", "-o", "json"]) == {
        ("storage.k8s.io", "storageclasses", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "logs", "p", "-c", "c"]) == {("", "pods/log", "get")}
    assert rbac_needs(["kubectl", "-n", "x", "exec", "p", "-c", "c", "--", "df"]) == {
        ("", "pods", "get"),
        ("", "pods/exec", "create"),
    }


def test_node_list_is_capped_but_the_conflict_uses_every_node():
    data = claim("healthy", "data")
    access = p.attachment_access(data, mounting_on([f"n{i:03d}" for i in range(80)]))
    assert len(access["nodes"]) == 50 and access["nodeCount"] == 80 and access["truncated"] is True
    assert access["multiNodeConflict"] is True
    one = p.attachment_access(data, mounting_on(["n1"]))
    assert one["nodeCount"] == 1 and one["truncated"] is False and one["terminatingNodes"] == []


def test_terminating_pod_on_another_node_is_not_a_conflict_but_is_reported():
    data = claim("healthy", "data")
    access = p.attachment_access(data, mounting_on(["n1"]) + mounting_on(["n2"], terminating=True))
    assert access["nodes"] == ["n1"] and access["terminatingNodes"] == ["n2"]
    assert access["multiNodeConflict"] is False


def test_read_write_once_pod_claim_conflicts_with_two_live_pods_on_any_nodes():
    data = copy.deepcopy(claim("healthy", "data"))
    data["spec"]["accessModes"] = ["ReadWriteOncePod"]
    same_node = p.attachment_access(data, mounting_on(["n1", "n1"]))
    assert same_node["readWriteOncePod"] is True and same_node["multiNodeConflict"] is True
    assert p.attachment_access(data, mounting_on(["n1"]))["multiNodeConflict"] is False
    assert (
        p.attachment_access(data, mounting_on(["n1"]) + mounting_on(["n1"], terminating=True))[
            "multiNodeConflict"
        ]
        is False
    )
    plain = p.attachment_access(claim("healthy", "data"), mounting_on(["n1"]))
    assert plain["readWriteOncePod"] is False


def test_unknown_pods_leave_the_node_facts_null():
    access = p.attachment_access(claim("healthy", "data"), None)
    assert (
        access["nodes"] is None
        and access["multiNodeConflict"] is None
        and access["nodeCount"] is None
    )
    assert access["accessModes"] == ["ReadWriteOnce"] and access["readWriteOnce"] is True
    rows = p.volume_attachments([va("a", "pv", "n1", True)], "pv", None)
    assert rows[0]["nodeHasMountingPod"] is None


def test_attachment_on_a_node_with_no_mounting_pod_is_reported():
    rows = p.volume_attachments([va("a", "pv", "n9", True)], "pv", ["n1"])
    assert rows[0]["attached"] is True and rows[0]["nodeHasMountingPod"] is False


def test_forbidden_pods_list_keeps_the_claim_facts_in_terminating():
    ctx = FakeCtx("rw-broken", "stuck", fail={"pods": FORBIDDEN % "pods"})
    out = script("terminating")(ctx)
    assert out["pods"] is None
    assert (
        out["termination"]["terminating"] is True
        and "rw.test/hold" in out["termination"]["finalizers"]
    )
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_forbidden_pods_list_keeps_the_claim_facts_in_attachment():
    ctx = FakeCtx("rw-healthy", "data", fail={"pods": FORBIDDEN % "pods"})
    out = script("attachment")(ctx)
    assert out["pods"] is None
    assert out["access"]["nodes"] is None and out["access"]["multiNodeConflict"] is None
    assert out["access"]["accessModes"] == ["ReadWriteOnce"] and out["access"]["volumeName"]
    assert out["volumeAttachments"] == []
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_forbidden_pods_list_fails_usage():
    with pytest.raises(RwError) as exc:
        script("usage")(FakeCtx("rw-healthy", "data", fail={"pods": FORBIDDEN % "pods"}))
    assert exc.value.code == "permission_denied"


def test_pod_vanishing_before_exec_is_internal_not_not_found():
    gone = 'Error from server (NotFound): pods "data-user-x" not found'
    with pytest.raises(RwError) as exc:
        script("usage")(FakeCtx("rw-healthy", "data", fail={"exec": gone}))
    assert exc.value.code == "internal"
    assert "went away before exec" in exc.value.message


BUSYBOX_DF = (DATA / "busybox_df.txt").read_text()


def test_parse_df_on_real_busybox_output():
    df = p.parse_df(BUSYBOX_DF)
    assert df["mountedOn"] == "/data" and df["filesystem"].startswith("/dev/")
    assert df["sizeBytes"] > df["usedBytes"] > 0 and df["availableBytes"] > 0


def test_usage_success_path_on_the_busybox_pod():
    ctx = FakeCtx("rw-healthy", "df-data", exec_out=BUSYBOX_DF)
    out = script("usage")(ctx)["usage"]
    assert out["pod"] == "df-user" and out["container"] == "busybox" and out["mountPath"] == "/data"
    assert 0 < out["percent"] < 100 and out["atOrAbove"] is False and out["runningPods"] == 1


def test_block_mode_claim_is_skipped_not_measured(monkeypatch):
    ctx = FakeCtx("rw-healthy", "data")
    real = ctx.run

    def run(argv):
        out = real(argv)
        if "persistentvolumeclaims" in argv:
            doc = json.loads(out.stdout)
            doc["spec"]["volumeMode"] = "Block"
            return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr="")
        return out

    ctx.run = run
    with pytest.raises(Skipped, match="block-mode"):
        script("usage")(ctx)
    assert not any("exec" in c for c in ctx.calls)
