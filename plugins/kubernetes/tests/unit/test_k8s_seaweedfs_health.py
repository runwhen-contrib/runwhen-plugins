"""Unit tests for k8s-seaweedfs-health: lib/ and the thin scripts, against JSON captured from the
kind-rw-plugins scenario rw-seaweedfs (two all-in-one `weed server -s3` pods: swfs-ok and swfs-broken).
Scripts run through runwhen_capability.testing: a real Context with the V13 semantics (ctx.run raises on
non-zero exit, ctx.error side channel, ctx.now fixed) and stubbed commands; no test reaches a cluster.
The S3 write task runs against a small simulator of the gateway behind the pod proxy."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_seaweedfs_health import api, discovery, k8s
from k8s_seaweedfs_health import buckets as buckets_mod
from k8s_seaweedfs_health.errors import RwError
from k8s_seaweedfs_health.k8s import proxy_path
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-seaweedfs-health"
DATA = Path(__file__).parent / "data" / "k8s_seaweedfs_health"
NOW = datetime(2026, 11, 3, 23, 0, 0, tzinfo=timezone.utc)
NS = "rw-seaweedfs"
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def load(name):
    return json.loads((DATA / name).read_text())


def text(name):
    return (DATA / name).read_text()


PODS = {r: load(f"pods_{r}.json") for r in ("ok", "broken")}
STS = {r: load(f"sts_{r}.json") for r in ("ok", "broken")}
POD = {r: PODS[r]["items"][0]["metadata"]["name"] for r in PODS}
IP = {r: PODS[r]["items"][0]["status"]["podIP"] for r in PODS}
SETTINGS = {
    "maxItems": 200,
    "maxPods": 20,
    "masterPort": 9333,
    "volumePort": 8080,
    "filerPort": 8888,
    "s3Port": 8333,
    "scheme": "http",
    "minFreeVolumeSlots": 1,
    "minFreeDiskPercent": 10,
    "garbageThreshold": 0.3,
    "maxBuckets": 50,
}


def script(name):
    spec = importlib.util.spec_from_file_location(f"swfs_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def sts_resource(rel):
    return {
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/swfs-{rel}",
        "cluster": "kind",
        "namespace": NS,
        "name": f"swfs-{rel}",
        "type": "kubernetes/statefulset",
    }


def ns_resource(name=NS, type_=True):
    res = {"path": f"kubernetes/clusters/kind/namespaces/{name}", "cluster": "kind", "name": name}
    if type_:
        res["type"] = "kubernetes/namespace"
    return res


def selector(rel):
    return f"app.kubernetes.io/name=seaweedfs,app.kubernetes.io/instance=swfs-{rel}"


def get_sts(rel):
    return ("kubectl", "-n", NS, "get", "statefulsets", f"swfs-{rel}", "-o", "json")


def get_pods(rel="ok"):
    return (
        "kubectl",
        "-n",
        NS,
        "get",
        "pods",
        "-l",
        selector(rel) if rel else "app.kubernetes.io/name=seaweedfs",
        "-o",
        "json",
    )


def get_workloads(rel, plural):
    return ("kubectl", "-n", NS, "get", plural, "-l", selector(rel), "-o", "json")


def raw(pod, port, path, verb="get"):
    return ("kubectl", verb, "--raw", proxy_path(NS, pod, port, path))


def ok(doc):
    return 0, doc if isinstance(doc, str) else json.dumps(doc), ""


RBAC = 'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot %s resource "%s" in API group "%s" in the namespace "rw-seaweedfs"'


def rbac(plural, verb="get", group=""):
    return 1, "", RBAC % (plural, verb, plural, group)


UPSTREAM = {
    "403": (1, "", "Error from server (Forbidden): unknown"),
    "404": (
        1,
        "",
        "Error from server (NotFound): the server could not find the requested resource",
    ),
    "500": (
        1,
        "",
        'Error from server (InternalError): an error on the server ("unknown") has prevented the request from succeeding',
    ),
    "refused": (
        1,
        "",
        "Error from server (ServiceUnavailable): error trying to reach service: dial tcp 10.244.0.9:9999: connect: connection refused",
    ),
    "pod": (1, "", 'Error from server (NotFound): pods "swfs-ok-0" not found'),
}


def release_stubs(rel, master=True, extra=None):
    """The kubectl answers every task needs, plus the master reads of a healthy release."""
    pod = POD[rel]
    stubs = {
        get_sts(rel): ok(STS[rel]),
        get_pods(rel): ok(PODS[rel]),
        get_workloads(rel, "statefulsets"): ok({"items": [STS[rel]]}),
        get_workloads(rel, "deployments"): ok({"items": []}),
    }
    if master:
        stubs |= {
            raw(pod, 9333, "cluster/status"): ok(text(f"cluster_status_{rel}.json")),
            raw(pod, 9333, "cluster/healthz"): ok(""),
            raw(pod, 9333, "dir/status"): ok(text(f"dir_status_{rel}.json")),
            raw(pod, 9333, "vol/status"): ok(text(f"vol_status_{rel}.json")),
            raw(pod, 8080, "status"): ok(text(f"volume_status_{rel}.json")),
            raw(pod, 8888, "healthz"): ok(""),
            raw(pod, 8888, "?metadata=true"): ok(text("filer_root.json")),
        }
    stubs.update(extra or {})
    return stubs


def run(task, resource, stubs, settings=None, dry_run=False, now=NOW, **inputs):
    """Run a script as the executor does: (envelope, errors, calls)."""
    with make_context(
        resource=resource, settings={**SETTINGS, **(settings or {})}, dry_run=dry_run, now=now
    ) as tc:
        calls = stub_commands(tc, stubs)
        env = run_main(script(task), tc, **inputs)
        return env, tc.errors, list(calls)


def validate(schema_file, value):
    schema = json.loads((SKILL / "schemas" / schema_file).read_text())
    Draft202012Validator.check_schema(schema)
    return [e.message for e in Draft202012Validator(schema).iter_errors(value)]


def check_outputs(task, env):
    spec = TASKS[task.replace("_", "-")]["outputs"]
    assert set(env["outputs"]) == set(spec)
    for out, s in spec.items():
        assert validate(Path(s["schema"]).name, env["outputs"][out]) == [], (task, out)


def codes(errors):
    return [(e["code"], e.get("scope")) for e in errors]


# ------------------------------------------------------------- pure: payloads ----


def test_cluster_view_single_master_has_no_peers():
    v = api.cluster_view(load("cluster_status_ok.json"))
    assert v["isLeader"] is True and v["leader"].endswith(":9333.19333")
    assert v["peers"] is None and v["peerCount"] is None


def test_cluster_view_reads_raft_peers_and_rejects_other_shapes():
    doc = {
        "IsLeader": False,
        "Leader": "m-0:9333",
        "Peers": ["m-0:9333", "m-1:9333", "m-2:9333"],
        "MaxVolumeId": 4,
    }
    v = api.cluster_view(doc)
    assert v["peerCount"] == 3 and v["isLeader"] is False and v["maxVolumeId"] == 4
    assert api.cluster_view({"IsLeader": False, "Leader": ""})["leader"] is None
    for bad in (
        {},
        {"IsLeader": "yes"},
        {"IsLeader": True, "Peers": [1, 2]},
        {"IsLeader": True, "Peers": "m-0"},
    ):
        with pytest.raises(RwError) as exc:
            api.cluster_view(bad)
        assert exc.value.code == "internal"


def test_slots_ok_has_room_and_broken_has_none():
    ok_slots = api.slots_view(load("dir_status_ok.json"), 1, 200)
    assert (ok_slots["max"], ok_slots["free"], ok_slots["belowMinFree"]) == (30, 16, False)
    broken = api.slots_view(load("dir_status_broken.json"), 1, 200)
    assert (broken["max"], broken["free"], broken["used"], broken["usedPercent"]) == (
        3,
        0,
        3,
        100.0,
    )
    assert broken["belowMinFree"] is True and broken["dataNodes"][0]["full"] is True
    assert api.slots_view(load("dir_status_broken.json"), 0, 200)["belowMinFree"] is False


def test_slots_without_topology_or_with_wrong_types_is_an_error():
    for bad in (
        {},
        {"Topology": {}},
        {"Topology": {"Max": "3", "Free": 0}},
        {"Topology": {"Max": 3, "Free": True}},
    ):
        with pytest.raises(RwError) as exc:
            api.slots_view(bad, 1, 200)
        assert exc.value.code == "internal"


def test_layouts_count_writable_volumes_per_collection():
    lay = api.layouts_view(load("dir_status_ok.json"), 200)
    assert lay["count"] == 2 and lay["withoutWritable"] == 0
    assert {(i["collection"], i["writableCount"]) for i in lay["items"]} == {
        ("photos", 7),
        ("logs", 7),
    }
    broken = api.layouts_view(load("dir_status_broken.json"), 200)
    assert broken["withoutWritable"] >= 1
    assert any(i["collection"] == "datab" and i["writableCount"] == 0 for i in broken["items"])


def test_layouts_with_null_layouts_is_empty_but_a_missing_key_is_an_error():
    doc = {"Topology": {"Max": 3, "Free": 3, "Layouts": None}}
    assert api.layouts_view(doc, 200)["count"] == 0
    with pytest.raises(RwError):
        api.layouts_view({"Topology": {"Max": 3, "Free": 3}}, 200)
    with pytest.raises(RwError):
        api.layouts_view(
            {
                "Topology": {
                    "Max": 3,
                    "Free": 3,
                    "Layouts": [
                        {"collection": "a", "replication": "000", "ttl": "", "writables": ["x"]}
                    ],
                }
            },
            200,
        )


def test_volumes_report_the_read_only_volume_and_collection_totals():
    v = api.volumes_view(load("vol_status_broken.json"), 0.3, 200)
    assert v["volumeCount"] == 3 and v["readOnlyVolumeCount"] == 1
    assert [(r["id"], r["collection"]) for r in v["readOnly"]] == [(2, "datab")]
    by = {c["collection"]: c for c in v["collections"]}
    assert by["datab"]["readOnlyVolumeCount"] == 1 and by["dataa"]["readOnlyVolumeCount"] == 0
    assert by["dataa"]["logicalBytes"] > 3_000_000 and by["dataa"]["fileCount"] == 1
    assert v["compactionCandidateCount"] == 0


def vol_doc(*volumes):
    """A /vol/status document: volumes are (node, volume dict)."""
    nodes = {}
    for node, vol in volumes:
        nodes.setdefault(node, []).append(vol)
    return {"Volumes": {"DataCenters": {"dc": {"rack": nodes}}}}


def vol(id_, coll="c", size=100, deleted=0, ro=False, files=1, repl=None, ttl=None):
    return {
        "Id": id_, "Size": size, "Collection": coll, "ReadOnly": ro, "FileCount": files, "DeleteCount": 0,
        "DeletedByteCount": deleted, "ReplicaPlacement": repl or {}, "Ttl": ttl or {"Count": 0, "Unit": 0},
    }  # fmt: skip


def test_replicas_count_once_logically_and_every_read_only_replica_is_listed():
    doc = vol_doc(
        ("a:8080", vol(1, size=100, ro=True, repl={"SameRackCount": 1})),
        ("b:8080", vol(1, size=100)),
    )
    v = api.volumes_view(doc, 0.3, 200)
    assert v["volumeCount"] == 1 and v["replicaCount"] == 2 and v["readOnlyVolumeCount"] == 1
    assert v["readOnlyReplicaCount"] == 1 and v["readOnly"][0]["node"] == "a:8080"
    assert v["readOnly"][0]["replication"] == "001"
    c = v["collections"][0]
    assert (c["logicalBytes"], c["physicalBytes"], c["replicaCount"]) == (100, 200, 2)


def test_compaction_candidates_are_volumes_over_the_garbage_threshold_worst_first():
    doc = vol_doc(
        ("a:8080", vol(1, size=100, deleted=50)),
        ("a:8080", vol(2, size=100, deleted=30)),
        ("a:8080", vol(3, size=100, deleted=29)),
        ("a:8080", vol(4, size=0)),
    )
    v = api.volumes_view(doc, 0.3, 200)
    assert [(c["id"], c["garbageRatio"]) for c in v["compactionCandidates"]] == [(1, 0.5), (2, 0.3)]
    assert api.volumes_view(doc, 0.6, 200)["compactionCandidateCount"] == 0
    assert v["collections"][0]["garbageRatio"] == round(109 / 300, 2)


def test_ttl_and_replication_strings():
    assert (
        api.ttl_string({"Count": 3, "Unit": 3}) == "3d"
        and api.ttl_string({"Count": 0, "Unit": 0}) == ""
    )
    assert api.replication_string({"DiffDataCenterCount": 1, "SameRackCount": 2}) == "102"
    assert api.replication_string({}) == "000"


@pytest.mark.parametrize(
    "bad",
    [{}, {"Volumes": {}}, {"Volumes": {"DataCenters": {"d": {"r": {"n": "x"}}}}},
     vol_doc(("n", {"Id": 1})), vol_doc(("n", {**vol(1), "ReadOnly": "no"}))],
)  # fmt: skip
def test_volumes_with_unexpected_payloads_raise(bad):
    with pytest.raises(RwError) as exc:
        api.volumes_view(bad, 0.3, 200)
    assert exc.value.code == "internal"


def test_disk_view_reads_disk_statuses_and_flags_low_free_space():
    d = api.disk_view(load("volume_status_ok.json"), 10, 200)
    assert d["disks"][0]["dir"] == "/data" and d["disks"][0]["belowMinFree"] is False
    assert d["volumeCount"] == 14 and d["readOnlyVolumeCount"] == 0
    doc = {
        "DiskStatuses": [
            {
                "dir": "/d",
                "all": 100,
                "used": 95,
                "free": 5,
                "percent_free": 5.0,
                "percent_used": 95.0,
            }
        ],
        "Volumes": [],
    }
    assert api.disk_view(doc, 10, 200)["disks"][0]["belowMinFree"] is True
    assert api.disk_view(doc, 5, 200)["disks"][0]["belowMinFree"] is False


def test_disk_view_does_not_read_a_schema_it_does_not_know_as_healthy():
    # the classic read .DiskUsages[]; a body without DiskStatuses is an error, never "no disks, fine"
    for bad in (
        {"DiskUsages": [{"dir": "/d", "percent_free": 50}], "Volumes": []},
        {"Volumes": []},
        {"DiskStatuses": None},
    ):
        with pytest.raises(RwError) as exc:
            api.disk_view(bad, 10, 200)
        assert exc.value.code == "internal"


def test_bucket_entry_quota_states():
    on = api.bucket_entry(load("bucket_broken_dataa.json"), "dataa")
    assert (on["quotaState"], on["quotaBytes"]) == ("enabled", 1048576)
    off = api.bucket_entry(load("bucket_broken_datab.json"), "datab")
    assert (off["quotaState"], off["quotaBytes"]) == ("disabled", 10485760)
    none = api.bucket_entry(load("bucket_ok_logs.json"), "logs")
    assert (none["quotaState"], none["quotaBytes"], none["extendedKeys"]) == ("none", None, [])
    with pytest.raises(RwError):
        api.bucket_entry(load("bucket_ok_logs.json"), "photos")  # the entry of another bucket


def test_extended_attribute_names_are_reported_without_values():
    doc = {**load("bucket_ok_logs.json"), "Extended": {"s3-owner": "c2VjcmV0", "x-amz-acl": "eHg="}}
    entry = api.bucket_entry(doc, "logs")
    assert entry["extendedKeys"] == ["s3-owner", "x-amz-acl"]
    assert "c2VjcmV0" not in json.dumps(entry)


def test_filer_root_must_be_the_root_entry():
    api.check_filer_root(load("filer_root.json"))
    for bad in ({}, {"FullPath": "/buckets"}, {"FullPath": 3}):
        with pytest.raises(RwError):
            api.check_filer_root(bad)


def test_collection_names_skip_the_default_collection_and_bytes_count_one_replica():
    doc = vol_doc(
        ("a", vol(1, "x", 10)), ("b", vol(1, "x", 10)), ("a", vol(2, "", 5)), ("a", vol(3, "y", 7))
    )
    assert api.collection_names(doc) == ["x", "y"]
    assert api.collection_bytes(doc) == {"x": 10, "": 5, "y": 7}


def test_root_tag_rejects_non_xml_and_returns_the_local_name():
    assert buckets_mod.root_tag(text("s3_list_bucket.xml"), "x") == "ListBucketResult"
    for bad in ("", "<html", "not xml", "x" * 2_000_000):
        with pytest.raises(RwError):
            buckets_mod.root_tag(bad, "x")


# ----------------------------------------------------------- pure: discovery ----


def pod_of(component, args=None, phase="Running", ip="10.0.0.1", deleting=False):
    meta = {"name": f"p-{component}", "labels": {"app.kubernetes.io/component": component}}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-03T22:00:00Z"
    return {
        "metadata": meta,
        "spec": {"containers": [{"name": "c", "args": args or []}]},
        "status": {"phase": phase, "podIP": ip},
    }


@pytest.mark.parametrize(
    "component,args,roles",
    [
        ("master", None, ["master"]),
        ("volume", None, ["volume"]),
        ("filer", None, ["filer"]),
        ("filer", ["filer", "-s3"], ["filer", "s3"]),
        ("filer", ["filer", "-s3=true"], ["filer", "s3"]),
        ("s3", None, ["s3"]),
        ("all-in-one", None, ["master", "volume", "filer", "s3"]),
        ("seaweedfs-all-in-one", None, ["master", "volume", "filer", "s3"]),
        ("admin", None, []),
        ("", None, []),
    ],
)
def test_roles_come_from_the_component_label_and_the_s3_flag(component, args, roles):
    assert discovery.roles_of(pod_of(component, args)) == roles


def test_only_running_pods_with_an_ip_that_are_not_deleting_are_queryable():
    assert discovery.queryable(pod_of("master"))
    assert not discovery.queryable(pod_of("master", phase="Pending"))
    assert not discovery.queryable(pod_of("master", ip=""))
    assert not discovery.queryable(pod_of("master", deleting=True))


def test_resolve_target_from_type_then_path():
    assert discovery.resolve_target("kubernetes/namespace") == "namespace"
    assert (
        discovery.resolve_target(None, "kubernetes/clusters/k/namespaces/x/statefulsets/s")
        == "statefulset"
    )
    assert discovery.resolve_target(None, "kubernetes/clusters/k/namespaces/x") == "namespace"
    for bad in ("kubernetes/deployment", "kubernetes/pod"):
        with pytest.raises(RwError) as exc:
            discovery.resolve_target(bad)
        assert exc.value.code == "invalid_input"


def test_release_of_statefulset_needs_the_seaweedfs_labels():
    assert discovery.release_of_statefulset(STS["ok"]) == "swfs-ok"
    other = copy.deepcopy(STS["ok"])
    other["metadata"]["labels"]["app.kubernetes.io/name"] = "postgres"
    with pytest.raises(RwError) as exc:
        discovery.release_of_statefulset(other)
    assert exc.value.code == "invalid_input"
    other = copy.deepcopy(STS["ok"])
    del other["metadata"]["labels"]["app.kubernetes.io/instance"]
    with pytest.raises(RwError) as exc:
        discovery.release_of_statefulset(other)
    assert exc.value.code == "invalid_input" and "release input" in exc.value.message


# ------------------------------------------------------- pure: proxy errors ----


class Done:
    def __init__(self, rc, err):
        self.returncode, self.stderr, self.stdout = rc, err, ""


@pytest.mark.parametrize(
    "result,code,kind",
    [
        (UPSTREAM["403"], "permission_denied", "upstream_denied"),
        (rbac("pods/proxy"), "permission_denied", "rbac"),
        (UPSTREAM["404"], "dependency", "upstream_missing"),
        (UPSTREAM["pod"], "not_found", "pod_missing"),
        (UPSTREAM["500"], "internal", "upstream_error"),
        (UPSTREAM["refused"], "unreachable", "unreachable"),
        (
            (
                1,
                "",
                "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
            ),
            "unreachable",
            "unreachable",
        ),
        (
            (
                1,
                "",
                "Error from server (Timeout): request did not complete within requested timeout",
            ),
            "timeout",
            "timeout",
        ),
        ((1, "", "Error from server (GatewayTimeout): ..."), "timeout", "timeout"),
        ((1, "", "Error from server (TooManyRequests): slow down"), "rate_limited", "rate_limited"),
        (
            (
                1,
                "",
                "Error from server (ServiceUnavailable): the server is currently unable to handle the request",
            ),
            "internal",
            "upstream_error",
        ),
        ((1, "", "something else entirely"), "internal", "upstream_error"),
        ((124, "", ""), "timeout", "timeout"),
    ],
)
def test_proxy_failures_are_classified_and_never_read_as_success(result, code, kind):
    err = k8s.proxy_failure(["kubectl", "get", "--raw", "/x"], Done(result[0], result[2]))
    assert (err.code, err.kind) == (code, kind)


def test_a_command_that_outlives_its_timeout_is_the_timeout_error():
    import subprocess

    class Ctx:
        def run(self, argv, **kw):
            raise subprocess.TimeoutExpired(argv, 20)

    with pytest.raises(RwError) as exc:
        k8s.proxy_get(Ctx(), NS, "p", 9333, "cluster/status")
    assert exc.value.code == "timeout"


def test_proxy_json_rejects_a_body_that_is_not_a_json_object():
    class Ctx:
        def __init__(self, out):
            self.out = out

        def run(self, argv, **kw):
            return SimpleNamespace(returncode=0, stdout=self.out, stderr="")

    for out in ("", "<html>login</html>", "[1]", "null", "3"):
        with pytest.raises(RwError) as exc:
            k8s.proxy_json(Ctx(out), NS, "p", 9333, "dir/status", "master /dir/status")
        assert exc.value.code == "internal"
    assert k8s.proxy_json(Ctx('{"a": 1}'), NS, "p", 9333, "x", "x") == {"a": 1}


def test_kubectl_json_raises_when_ctx_run_does_not():
    class Ctx:
        def run(self, argv, **kw):
            return Done(1, 'Error from server (Forbidden): x is forbidden: cannot get resource "x"')

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "permission_denied"


# ------------------------------------------------------------- cluster-status ----


@pytest.mark.parametrize("rel", ["ok", "broken"])
def test_cluster_status_of_a_healthy_release(rel):
    env, errors, _ = run("cluster_status", sts_resource(rel), release_stubs(rel))
    assert env["status"] == "ok" and errors == []
    check_outputs("cluster_status", env)
    out = env["outputs"]
    assert out["release"]["name"] == f"swfs-{rel}"
    w = out["release"]["workloads"]
    assert (w["count"], w["unread"], w["items"][0]["desired"], w["items"][0]["ready"]) == (
        1,
        [],
        1,
        1,
    )
    pod = out["release"]["pods"]["items"][0]
    assert pod["roles"] == ["master", "volume", "filer", "s3"] and pod["ready"] is True
    m = out["master"]
    assert (m["leaderCount"], m["leaderAgreement"], m["queriedCount"]) == (1, True, 1)
    assert m["members"][0]["healthz"] == "ok" and m["members"][0]["peers"] is None


def test_cluster_status_by_namespace_with_two_releases_needs_the_release_input():
    stubs = release_stubs("ok") | {
        get_pods(None): ok({"items": PODS["ok"]["items"] + PODS["broken"]["items"]})
    }
    env, errors, _ = run("cluster_status", ns_resource(), stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert "swfs-broken" in errors[0]["message"] and "swfs-ok" in errors[0]["message"]
    env, errors, _ = run("cluster_status", ns_resource(), stubs, release="swfs-ok")
    assert env["status"] == "ok" and env["outputs"]["release"]["name"] == "swfs-ok"


def test_cluster_status_by_namespace_with_one_release_finds_it():
    stubs = release_stubs("ok") | {get_pods(None): ok(PODS["ok"])}
    env, errors, calls = run("cluster_status", ns_resource(), stubs)
    assert env["status"] == "ok" and env["outputs"]["release"]["name"] == "swfs-ok"
    assert get_sts("ok") not in calls  # a namespace target reads no StatefulSet to find the release


def test_a_namespace_without_seaweedfs_pods_is_not_found_not_an_empty_release():
    env, errors, _ = run(
        "cluster_status",
        ns_resource("empty"),
        {
            (
                "kubectl",
                "-n",
                "empty",
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=seaweedfs",
                "-o",
                "json",
            ): ok({"items": []})
        },
    )
    assert env["status"] == "failed" and errors[0]["code"] == "not_found"


def test_release_input_must_match_the_statefulset_and_be_a_label_value():
    env, errors, _ = run(
        "cluster_status", sts_resource("ok"), release_stubs("ok"), release="swfs-broken"
    )
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    env, errors, calls = run("cluster_status", ns_resource(), {}, release="a b;c")
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_a_pod_that_is_not_running_is_listed_and_not_asked():
    pending = copy.deepcopy(PODS["ok"])
    pending["items"][0]["status"]["phase"] = "Pending"
    del pending["items"][0]["status"]["podIP"]
    stubs = release_stubs("ok", master=False) | {get_pods("ok"): ok(pending)}
    env, errors, calls = run("cluster_status", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and [e["code"] for e in errors] == ["unreachable"]
    m = env["outputs"]["master"]
    assert (
        m["queriedCount"] == 0
        and m["leaderAgreement"] is None
        and m["members"][0]["queried"] is False
    )
    assert "Pending" in m["members"][0]["notQueriedReason"]
    assert not [c for c in calls if "--raw" in c]


def test_master_healthz_error_status_is_unhealthy_not_healthy():
    stubs = release_stubs(
        "ok",
        extra={
            raw(POD["ok"], 9333, "cluster/healthz"): (
                1,
                "",
                "Error from server (ServiceUnavailable): the server is currently unable to handle the request",
            )
        },
    )
    env, errors, _ = run("cluster_status", sts_resource("ok"), stubs)
    assert (
        env["status"] == "ok" and env["outputs"]["master"]["members"][0]["healthz"] == "unhealthy"
    )


@pytest.mark.parametrize(
    "answer,code",
    [
        (UPSTREAM["refused"], "unreachable"),
        (UPSTREAM["404"], "dependency"),
        (UPSTREAM["500"], "internal"),
        (UPSTREAM["403"], "permission_denied"),
        (rbac("pods/proxy"), "permission_denied"),
    ],
)
def test_master_read_errors_keep_the_pod_facts_and_make_the_result_partial(answer, code):
    stubs = release_stubs(
        "ok",
        extra={
            raw(POD["ok"], 9333, "cluster/status"): answer,
            raw(POD["ok"], 9333, "cluster/healthz"): answer,
        },
    )
    env, errors, _ = run("cluster_status", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and {e["code"] for e in errors} == {code}
    assert env["outputs"]["release"]["pods"]["count"] == 1
    m = env["outputs"]["master"]["members"][0]
    assert m["queried"] is False and m["isLeader"] is None
    # a 5xx on healthz is the process answering unhealthy; every other failure is no answer at all
    assert m["healthz"] == ("unhealthy" if answer == UPSTREAM["500"] else None)


def test_unexpected_cluster_status_payload_is_an_error_not_a_leaderless_cluster():
    for body in ("{}", "[]", '{"IsLeader": "true"}', "<html>", ""):
        stubs = release_stubs("ok", extra={raw(POD["ok"], 9333, "cluster/status"): ok(body)})
        env, errors, _ = run("cluster_status", sts_resource("ok"), stubs)
        assert env["status"] == "partial" and errors[0]["code"] == "internal", body
        assert env["outputs"]["master"]["queriedCount"] == 0


def test_forbidden_statefulset_list_marks_workloads_unread_not_empty():
    stubs = release_stubs(
        "ok", extra={get_workloads("ok", "statefulsets"): rbac("statefulsets", "list", "apps")}
    )
    env, errors, _ = run("cluster_status", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and codes(errors) == [("permission_denied", "statefulsets")]
    w = env["outputs"]["release"]["workloads"]
    assert w["unread"] == ["statefulsets"] and w["count"] == 0


def two_masters():
    pods = copy.deepcopy(PODS["ok"])
    second = copy.deepcopy(pods["items"][0])
    second["metadata"]["name"] = "swfs-ok-1"
    second["status"]["podIP"] = "10.244.0.200"
    pods["items"].append(second)
    return pods


def test_two_masters_naming_different_leaders_do_not_agree_and_two_claims_are_counted():
    pods = two_masters()

    def leader(ip, me):
        return json.dumps({"IsLeader": me, "Leader": f"{ip}:9333", "Peers": ["a:9333", "b:9333"]})

    stubs = release_stubs("ok", extra={
        get_pods("ok"): ok(pods),
        raw("swfs-ok-0", 9333, "cluster/status"): ok(leader(IP["ok"], True)),
        raw("swfs-ok-1", 9333, "cluster/status"): ok(leader("10.244.0.200", True)),
        raw("swfs-ok-1", 9333, "cluster/healthz"): ok(""),
    })  # fmt: skip
    env, _, _ = run("cluster_status", sts_resource("ok"), stubs)
    m = env["outputs"]["master"]
    assert (m["leaderCount"], m["leaderAgreement"], len(m["leaders"])) == (2, False, 2)
    assert m["members"][0]["peerCount"] == 2


def test_init_containers_are_reported_with_init_true():
    pods = copy.deepcopy(PODS["ok"])
    pods["items"][0]["status"]["initContainerStatuses"] = [
        {
            "name": "setup",
            "ready": True,
            "restartCount": 0,
            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
        }
    ]
    env, _, _ = run(
        "cluster_status", sts_resource("ok"), release_stubs("ok", extra={get_pods("ok"): ok(pods)})
    )
    cs = env["outputs"]["release"]["pods"]["items"][0]["containers"]
    assert [(c["name"], c["init"], c["state"], c["reason"]) for c in cs] == [
        ("setup", True, "terminated", "Completed"),
        ("seaweedfs", False, "running", None),
    ]


def test_lists_are_capped_by_max_items_but_counts_are_real():
    pods = copy.deepcopy(PODS["ok"])
    for i in range(1, 4):
        extra = copy.deepcopy(pods["items"][0])
        extra["metadata"]["name"] = f"swfs-ok-{i}"
        extra["metadata"]["labels"]["app.kubernetes.io/component"] = "volume"
        pods["items"].append(extra)
    stubs = release_stubs("ok", extra={get_pods("ok"): ok(pods)})
    env, _, _ = run("cluster_status", sts_resource("ok"), stubs, settings={"maxItems": 2})
    p = env["outputs"]["release"]["pods"]
    assert (p["count"], len(p["items"]), p["truncated"]) == (4, 2, True)


# --------------------------------------------------------- failure of the target ----

ALL_TASKS = ["cluster_status", "volumes", "filer_status", "buckets", "s3_check"]


@pytest.mark.parametrize("task", ALL_TASKS)
def test_missing_statefulset_is_not_found_in_every_task(task):
    err = 'Error from server (NotFound): statefulsets.apps "swfs-nope" not found'
    res = {**sts_resource("ok"), "name": "swfs-nope"}
    stub = ("kubectl", "-n", NS, "get", "statefulsets", "swfs-nope", "-o", "json")
    env, errors, calls = run(task, res, {stub: (1, "", err)})
    assert env["status"] == "failed" and errors[0]["code"] == "not_found" and calls == [stub]


@pytest.mark.parametrize("task", ALL_TASKS)
def test_forbidden_target_and_unreachable_api_fail_every_task(task):
    env, errors, _ = run(
        task, sts_resource("ok"), {get_sts("ok"): rbac("statefulsets", "get", "apps")}
    )
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"
    down = (
        1,
        "",
        "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
    )
    env, errors, _ = run(task, sts_resource("ok"), {get_sts("ok"): down})
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


@pytest.mark.parametrize("task", ALL_TASKS)
def test_forbidden_pod_list_fails_every_task(task):
    stubs = {get_sts("ok"): ok(STS["ok"]), get_pods("ok"): rbac("pods", "list")}
    env, errors, _ = run(task, sts_resource("ok"), stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"


@pytest.mark.parametrize("task", ALL_TASKS)
def test_wrong_target_type_is_refused_before_any_kubectl_call(task):
    res = {
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/deployments/web",
        "cluster": "kind",
        "namespace": NS,
        "name": "web",
        "type": "kubernetes/deployment",
    }
    env, errors, calls = run(task, res, {})
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_target_type_falls_back_to_the_path():
    res = {k: v for k, v in sts_resource("ok").items() if k != "type"}
    env, _, _ = run("cluster_status", res, release_stubs("ok"))
    assert env["status"] == "ok"


def test_statefulset_of_another_application_is_invalid_input():
    other = copy.deepcopy(STS["ok"])
    other["metadata"]["labels"] = {"app": "db"}
    env, errors, _ = run("cluster_status", sts_resource("ok"), {get_sts("ok"): ok(other)})
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"


# ------------------------------------------------------------------- volumes ----


def test_volumes_of_the_healthy_release():
    env, errors, _ = run("volumes", sts_resource("ok"), release_stubs("ok"))
    assert env["status"] == "ok" and errors == []
    check_outputs("volumes", env)
    o = env["outputs"]
    assert (o["slots"]["max"], o["slots"]["free"], o["slots"]["belowMinFree"]) == (30, 16, False)
    assert o["layouts"]["withoutWritable"] == 0 and o["volumes"]["readOnly"] == []
    assert o["volumes"]["volumeCount"] == 14 and o["disk"]["queriedCount"] == 1
    assert o["disk"]["items"][0]["disks"][0]["belowMinFree"] is False


def test_volumes_of_the_broken_release_show_full_slots_and_the_read_only_volume():
    env, errors, _ = run("volumes", sts_resource("broken"), release_stubs("broken"))
    assert env["status"] == "ok" and errors == []
    check_outputs("volumes", env)
    o = env["outputs"]
    assert (o["slots"]["free"], o["slots"]["belowMinFree"]) == (0, True)
    assert [(r["id"], r["collection"]) for r in o["volumes"]["readOnly"]] == [(2, "datab")]
    assert o["layouts"]["withoutWritable"] >= 1
    assert o["disk"]["items"][0]["readOnlyVolumeCount"] == 1


def test_thresholds_are_settings():
    env, _, _ = run(
        "volumes",
        sts_resource("ok"),
        release_stubs("ok"),
        settings={"minFreeVolumeSlots": 17, "minFreeDiskPercent": 99},
    )
    o = env["outputs"]
    assert (
        o["slots"]["belowMinFree"] is True
        and o["disk"]["items"][0]["disks"][0]["belowMinFree"] is True
    )


def test_volume_server_failure_keeps_the_master_facts_as_partial():
    stubs = release_stubs("ok", extra={raw(POD["ok"], 8080, "status"): UPSTREAM["refused"]})
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and codes(errors) == [
        ("unreachable", f"volume/{POD['ok']}/status")
    ]
    assert env["outputs"]["slots"]["max"] == 30 and env["outputs"]["disk"]["queriedCount"] == 0


def test_volumes_with_every_proxy_read_denied_fails_instead_of_reporting_nothing():
    denied = rbac("pods/proxy")
    stubs = release_stubs(
        "ok",
        extra={
            raw(POD["ok"], 9333, "dir/status"): denied,
            raw(POD["ok"], 9333, "vol/status"): denied,
            raw(POD["ok"], 8080, "status"): denied,
        },
    )
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"
    assert env["outputs"] == {}


def test_a_dir_status_that_is_not_the_expected_document_is_not_an_empty_slot_list():
    stubs = release_stubs(
        "ok", extra={raw(POD["ok"], 9333, "dir/status"): ok('{"Topology": null}')}
    )
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and {e["code"] for e in errors} == {"internal"}
    assert env["outputs"]["slots"] is None and env["outputs"]["layouts"] is None
    assert env["outputs"]["volumes"]["volumeCount"] == 14


def test_a_volume_server_status_without_disk_statuses_is_an_error():
    stubs = release_stubs(
        "ok", extra={raw(POD["ok"], 8080, "status"): ok('{"Volumes": [], "DiskUsages": []}')}
    )
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and errors[0]["code"] == "internal"
    assert env["outputs"]["disk"]["items"] == []


def test_the_second_master_answers_when_the_first_does_not_and_the_first_failure_is_reported():
    pods = two_masters()
    stubs = release_stubs("ok", extra={
        get_pods("ok"): ok(pods),
        raw("swfs-ok-0", 9333, "dir/status"): UPSTREAM["refused"],
        raw("swfs-ok-0", 9333, "vol/status"): UPSTREAM["refused"],
        raw("swfs-ok-1", 9333, "dir/status"): ok(text("dir_status_ok.json")),
        raw("swfs-ok-1", 9333, "vol/status"): ok(text("vol_status_ok.json")),
        raw("swfs-ok-1", 8080, "status"): ok(text("volume_status_ok.json")),
    })  # fmt: skip
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and {e["scope"] for e in errors} == {"master/swfs-ok-0"}
    assert env["outputs"]["slots"]["max"] == 30
    assert (
        env["outputs"]["disk"]["queriedCount"] == 2
    )  # both pods serve the volume role in an all-in-one release


def test_no_running_master_is_unreachable():
    pending = copy.deepcopy(PODS["ok"])
    pending["items"][0]["status"]["phase"] = "Pending"
    stubs = release_stubs("ok", master=False, extra={get_pods("ok"): ok(pending)})
    env, errors, _ = run("volumes", sts_resource("ok"), stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"
    assert "Pending" in errors[0]["message"] or "not Running" in errors[0]["message"]


def test_volume_servers_beyond_max_pods_are_counted_but_not_asked():
    pods = copy.deepcopy(PODS["ok"])
    for i in range(1, 4):
        extra = copy.deepcopy(pods["items"][0])
        extra["metadata"]["name"] = f"swfs-ok-{i}"
        extra["status"]["podIP"] = f"10.244.1.{i}"
        extra["metadata"]["labels"]["app.kubernetes.io/component"] = "volume"
        pods["items"].append(extra)
    stubs = release_stubs(
        "ok",
        extra={
            get_pods("ok"): ok(pods),
            **{
                raw(f"swfs-ok-{i}", 8080, "status"): ok(text("volume_status_ok.json"))
                for i in range(1, 4)
            },
        },
    )
    env, _, calls = run("volumes", sts_resource("ok"), stubs, settings={"maxPods": 2})
    d = env["outputs"]["disk"]
    assert (d["podCount"], d["queriedCount"], d["truncated"]) == (4, 2, True)
    assert raw("swfs-ok-3", 8080, "status") not in calls


# --------------------------------------------------------------- filer-status ----


def test_filer_status_of_the_healthy_release():
    env, errors, _ = run("filer_status", sts_resource("ok"), release_stubs("ok"))
    assert env["status"] == "ok" and errors == []
    check_outputs("filer_status", env)
    o = env["outputs"]
    assert o["filer"]["items"] == [{"pod": POD["ok"], "healthz": "ok", "rootReadable": True}]
    v = o["volumeServers"]
    assert (v["podCount"], v["registeredCount"], v["dataNodeCount"], v["unmatchedDataNodes"]) == (
        1,
        1,
        1,
        [],
    )
    assert (
        v["items"][0]["registered"] is True and v["items"][0]["dataNodeUrl"] == f"{IP['ok']}:8080"
    )


def test_a_volume_server_the_master_does_not_list_is_unregistered_and_a_stranger_is_unmatched():
    topo = load("dir_status_ok.json")
    topo["Topology"]["DataCenters"][0]["Racks"][0]["DataNodes"][0]["Url"] = "10.9.9.9:8080"
    env, errors, _ = run(
        "filer_status",
        sts_resource("ok"),
        release_stubs("ok", extra={raw(POD["ok"], 9333, "dir/status"): ok(topo)}),
    )
    assert env["status"] == "ok" and errors == []
    v = env["outputs"]["volumeServers"]
    assert v["registeredCount"] == 0 and v["items"][0]["registered"] is False
    assert v["unmatchedDataNodes"] == ["10.9.9.9:8080"]


def test_filer_health_error_status_is_unhealthy_and_unreadable_root_is_reported():
    stubs = release_stubs(
        "ok",
        extra={
            raw(POD["ok"], 8888, "healthz"): UPSTREAM["500"],
            raw(POD["ok"], 8888, "?metadata=true"): ok('{"FullPath": "/x"}'),
        },
    )
    env, errors, _ = run("filer_status", sts_resource("ok"), stubs)
    f = env["outputs"]["filer"]["items"][0]
    assert env["status"] == "partial" and f["rootReadable"] is False and f["healthz"] == "unhealthy"
    assert codes(errors) == [("internal", f"filer/{POD['ok']}/root")]


def test_master_unreachable_leaves_registration_null_not_empty():
    stubs = release_stubs("ok", extra={raw(POD["ok"], 9333, "dir/status"): UPSTREAM["refused"]})
    env, errors, _ = run("filer_status", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and env["outputs"]["volumeServers"] is None
    assert codes(errors) == [("unreachable", "master/dir-status")]


def split_pods(*components):
    """The ok release as one pod per component (swfs-ok-0, -1, ...), each on its own IP."""
    pods = copy.deepcopy(PODS["ok"])
    base = pods["items"][0]
    pods["items"] = []
    for i, component in enumerate(components):
        pod = copy.deepcopy(base)
        pod["metadata"]["name"] = f"swfs-ok-{i}"
        pod["metadata"]["labels"]["app.kubernetes.io/component"] = component
        pod["status"]["podIP"] = f"10.244.5.{i + 1}"
        pod["spec"]["containers"][0]["args"] = [component]  # no -s3: a filer that serves no S3
        pods["items"].append(pod)
    return pods


def test_a_release_without_a_filer_has_no_filer_pods_and_is_not_an_error():
    pods = split_pods("master", "volume")
    stubs = release_stubs("ok", master=False, extra={
        get_pods("ok"): ok(pods),
        raw("swfs-ok-0", 9333, "dir/status"): ok(text("dir_status_ok.json")),
    })  # fmt: skip
    env, errors, _ = run("filer_status", sts_resource("ok"), stubs)
    assert env["status"] == "ok" and errors == []
    assert env["outputs"]["filer"]["podCount"] == 0 and env["outputs"]["filer"]["items"] == []
    assert (
        env["outputs"]["volumeServers"]["registeredCount"] == 0
    )  # the topology's node is not swfs-ok-1's IP


# -------------------------------------------------------------------- buckets ----


def s3_list(pod, bucket):
    return raw(pod, 8333, f"{bucket}/?list-type=2&max-keys=1")


def bucket_stubs(rel, extra=None):
    pod = POD[rel]
    names = {"ok": ["logs", "photos"], "broken": ["dataa", "datab", "datac"]}[rel]
    stubs = release_stubs(rel)
    stubs[raw(pod, 8333, "")] = ok(text("s3_list_buckets_broken.xml"))
    for n in names:
        stubs[s3_list(pod, n)] = ok(text("s3_list_bucket.xml"))
    entries = {
        "ok": {"photos": "bucket_ok_photos.json", "logs": "bucket_ok_logs.json"},
        "broken": {"dataa": "bucket_broken_dataa.json", "datab": "bucket_broken_datab.json"},
    }[rel]
    for n in names:
        stubs[raw(pod, 8888, f"buckets/{n}?metadata=true")] = (
            ok(text(entries[n])) if n in entries else UPSTREAM["404"]
        )
    stubs.update(extra or {})
    return stubs


def test_buckets_of_the_healthy_release_with_a_quota_on_one():
    env, errors, _ = run("buckets", sts_resource("ok"), bucket_stubs("ok"))
    assert env["status"] == "ok" and errors == []
    check_outputs("buckets", env)
    items = {b["name"]: b for b in env["outputs"]["buckets"]["items"]}
    assert set(items) == {"logs", "photos"}
    assert (
        items["photos"]["quotaState"] == "enabled"
        and items["photos"]["quotaBytes"] == 100 * 1024 * 1024
    )
    assert 1.8 < items["photos"]["quotaUsedPercent"] < 2.0
    assert items["logs"]["quotaState"] == "none" and items["logs"]["quotaUsedPercent"] is None
    assert (
        items["photos"]["anonymousList"] == "allowed"
        and env["outputs"]["gateway"]["anonymousListBuckets"] == "allowed"
    )


def test_buckets_of_the_broken_release_over_quota_disabled_quota_and_no_bucket_entry():
    extra = {s3_list(POD["broken"], "datac"): UPSTREAM["404"]}
    env, errors, _ = run("buckets", sts_resource("broken"), bucket_stubs("broken", extra))
    assert env["status"] == "ok" and errors == []
    check_outputs("buckets", env)
    items = {b["name"]: b for b in env["outputs"]["buckets"]["items"]}
    assert items["dataa"]["quotaState"] == "enabled" and items["dataa"]["quotaUsedPercent"] > 100
    assert (
        items["datab"]["quotaState"],
        items["datab"]["quotaBytes"],
        items["datab"]["quotaUsedPercent"],
    ) == ("disabled", 10 * 1024 * 1024, None)
    assert (
        items["datac"]["entryFound"],
        items["datac"]["anonymousList"],
        items["datac"]["quotaState"],
    ) == (False, "no_such_bucket", None)


def test_a_gateway_that_refuses_anonymous_listing_is_denied_not_unknown():
    pod = POD["ok"]
    denied = {
        raw(pod, 8333, ""): UPSTREAM["403"],
        s3_list(pod, "logs"): UPSTREAM["403"],
        s3_list(pod, "photos"): UPSTREAM["403"],
    }
    env, errors, _ = run("buckets", sts_resource("ok"), bucket_stubs("ok", denied))
    assert env["status"] == "ok" and errors == []
    assert env["outputs"]["gateway"]["anonymousListBuckets"] == "denied"
    assert {b["anonymousList"] for b in env["outputs"]["buckets"]["items"]} == {"denied"}


def test_anonymous_probe_errors_are_not_read_as_denied_or_allowed():
    pod = POD["ok"]
    env, errors, _ = run(
        "buckets",
        sts_resource("ok"),
        bucket_stubs(
            "ok",
            {
                s3_list(pod, "logs"): UPSTREAM["500"],
                raw(pod, 8333, ""): ok("<html>proxy error</html>"),
            },
        ),
    )
    assert env["status"] == "partial"
    items = {b["name"]: b for b in env["outputs"]["buckets"]["items"]}
    assert items["logs"]["anonymousList"] is None and items["photos"]["anonymousList"] == "allowed"
    assert env["outputs"]["gateway"]["anonymousListBuckets"] is None
    assert sorted(e["code"] for e in errors) == ["internal", "internal"]


def test_a_release_without_an_s3_gateway_has_no_anonymous_facts():
    pods = split_pods("master", "filer")
    stubs = release_stubs("ok", master=False, extra={
        get_pods("ok"): ok(pods),
        raw("swfs-ok-0", 9333, "vol/status"): ok(text("vol_status_ok.json")),
        raw("swfs-ok-1", 8888, "buckets/photos?metadata=true"): ok(text("bucket_ok_photos.json")),
        raw("swfs-ok-1", 8888, "buckets/logs?metadata=true"): ok(text("bucket_ok_logs.json")),
    })  # fmt: skip
    env, errors, _ = run("buckets", sts_resource("ok"), stubs)
    assert env["status"] == "ok" and env["outputs"]["gateway"] == {
        "podCount": 0,
        "anonymousListBuckets": None,
    }
    assert {b["anonymousList"] for b in env["outputs"]["buckets"]["items"]} == {None}


def test_buckets_beyond_max_buckets_are_counted_not_inspected():
    env, _, calls = run(
        "buckets", sts_resource("ok"), bucket_stubs("ok"), settings={"maxBuckets": 1}
    )
    b = env["outputs"]["buckets"]
    assert (b["count"], b["inspected"], b["truncated"]) == (2, 1, True)
    assert s3_list(POD["ok"], "photos") not in calls


def test_buckets_when_the_master_cannot_be_read_fails_or_is_partial_never_empty():
    stubs = bucket_stubs("ok", {raw(POD["ok"], 9333, "vol/status"): UPSTREAM["refused"]})
    env, errors, _ = run("buckets", sts_resource("ok"), stubs)
    assert env["status"] == "partial" and env["outputs"]["buckets"]["count"] is None
    assert (
        env["outputs"]["buckets"]["items"] == [] and env["outputs"]["buckets"]["truncated"] is False
    )
    assert env["outputs"]["gateway"]["anonymousListBuckets"] == "allowed"


# ------------------------------------------------------------------- s3-check ----


class Gateway:
    """The S3 gateway behind the pod proxy: buckets with objects, anonymous or not, with failures that
    can be injected per operation. Answers like `kubectl get|replace|delete --raw` (body only on 2xx)."""

    def __init__(self, pod, buckets=None, anonymous_write=True, anonymous_list=True, fail=None):
        self.pod, self.buckets = pod, {b: dict(o) for b, o in (buckets or {}).items()}
        self.anonymous_write, self.anonymous_list, self.fail = (
            anonymous_write,
            anonymous_list,
            dict(fail or {}),
        )
        self.mutations = []

    def run(self, argv, cwd=None, check=None):
        verb, uri = argv[1], argv[3]
        path = uri.split("/proxy/", 1)[1]
        bucket, _, key = path.split("?")[0].partition("/")
        op = {
            "get": "list" if (not key or "list-type" in path) else "get-object",
            "replace": "put",
            "delete": "delete",
        }[verb]
        op = {"list": "list", "get-object": "get", "put": "put", "delete": "delete"}[op]
        label = f"{op}-{'object' if key else 'bucket'}" if op != "list" else "list"
        if op in ("put", "delete"):
            self.mutations.append((op, path))
        if label in self.fail:
            return self.fail[label]
        if op == "list":
            if not self.anonymous_list:
                return UPSTREAM["403"]
            return ok(text("s3_list_bucket.xml")) if bucket in self.buckets else UPSTREAM["404"]
        if op in ("put", "delete") and not self.anonymous_write:
            return UPSTREAM["403"]
        if op == "put" and not key:
            self.buckets.setdefault(bucket, {})
            return ok("")
        if op == "put":
            self.buckets[bucket][key] = (Path(cwd) / argv[argv.index("-f") + 1]).read_text()
            return ok("")
        if op == "get":
            return (
                ok(self.buckets[bucket][key])
                if key in self.buckets.get(bucket, {})
                else UPSTREAM["404"]
            )
        if op == "delete":
            if key:
                self.buckets.get(bucket, {}).pop(key, None)
            else:
                self.buckets.pop(bucket, None)
            return ok("")


def s3_run(
    gateway, rel="ok", dry_run=False, extra_stubs=None, resource=None, settings=None, **inputs
):
    stubs = release_stubs(rel, master=False, extra=extra_stubs)
    with make_context(
        resource=resource or sts_resource(rel),
        settings={**SETTINGS, **(settings or {})},
        dry_run=dry_run,
        now=NOW,
    ) as tc:
        calls = stub_commands(tc, stubs)
        base = tc.ctx.run

        def run_(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
            if argv[:3] in (
                ["kubectl", "get", "--raw"],
                ["kubectl", "replace", "--raw"],
                ["kubectl", "delete", "--raw"],
            ):
                calls.append(tuple(argv))
                rc, out, err = gateway.run(argv, cwd)
                return tc.ctx._finish_run(list(argv), rc, out, err, check)
            return base(
                argv, cwd=cwd, timeout=timeout, env=env, inherit_env=inherit_env, check=check
            )

        tc.ctx.run = run_
        env = run_main(script("s3_check"), tc, **inputs)
        return env, tc.errors, list(calls), gateway


KEY = "runwhen-s3-check/20261103T230000Z.txt"


def test_s3_check_puts_reads_deletes_confirms_and_removes_the_bucket_it_created():
    env, errors, calls, gw = s3_run(Gateway(POD["ok"]))
    assert env["status"] == "ok" and errors == []
    check_outputs("s3_check", env)
    p = env["outputs"]["probe"]
    assert p["passed"] is True and p["contentMatches"] is True and p["anonymousWrite"] is True
    assert (p["bucketExisted"], p["bucketCreated"], p["cleanedUp"], p["leftBehind"]) == (
        False,
        True,
        True,
        [],
    )
    assert [s["name"] for s in p["steps"]] == [
        "create-bucket",
        "put-object",
        "get-object",
        "delete-object",
        "confirm-deleted",
        "delete-bucket",
    ]
    assert all(s["ok"] for s in p["steps"]) and p["key"] == KEY
    assert gw.buckets == {}  # nothing left behind


def test_s3_check_in_an_existing_bucket_touches_only_its_own_object():
    gw = Gateway(POD["ok"], buckets={"runwhen-s3-check": {"mine.txt": "keep"}})
    env, errors, _, gw = s3_run(gw)
    p = env["outputs"]["probe"]
    assert (
        env["status"] == "ok"
        and p["passed"] is True
        and p["bucketExisted"] is True
        and p["bucketCreated"] is False
    )
    assert [s["name"] for s in p["steps"]] == [
        "put-object",
        "get-object",
        "delete-object",
        "confirm-deleted",
    ]
    assert gw.buckets == {"runwhen-s3-check": {"mine.txt": "keep"}}
    assert ("delete", "runwhen-s3-check") not in gw.mutations


def test_s3_check_dry_run_lists_the_bucket_and_mutates_nothing():
    env, errors, calls, gw = s3_run(Gateway(POD["ok"]), dry_run=True)
    assert env["status"] == "ok" and env["dryRun"] is True
    check_outputs("s3_check", env)
    p = env["outputs"]["probe"]
    assert (
        p["steps"] == []
        and p["passed"] is None
        and p["plan"][0] == "create-bucket"
        and p["bucketExisted"] is False
    )
    assert gw.mutations == [] and not [c for c in calls if c[1] in ("replace", "delete")]


def test_s3_check_skips_when_the_gateway_refuses_an_anonymous_write_and_writes_nothing():
    gw = Gateway(POD["ok"], anonymous_write=False)
    env, errors, calls, gw = s3_run(gw)
    assert env["status"] == "skipped" and errors == []
    assert "credential" in env["reason"] and "never reads Secrets" in env["reason"]
    assert gw.buckets == {}
    assert [m[0] for m in gw.mutations] == [
        "put"
    ]  # the one refused request; no cleanup DELETE followed it


def test_s3_check_skips_when_even_listing_needs_credentials():
    env, errors, calls, gw = s3_run(Gateway(POD["ok"], anonymous_list=False))
    assert env["status"] == "skipped" and gw.mutations == []


def test_s3_check_refused_object_write_in_an_existing_bucket_skips_without_cleanup_delete():
    gw = Gateway(POD["ok"], buckets={"runwhen-s3-check": {}}, anonymous_write=False)
    env, _, _, gw = s3_run(gw)
    assert env["status"] == "skipped" and [m[0] for m in gw.mutations] == ["put"]


def test_s3_check_skips_when_the_release_has_no_s3_pod():
    pods = copy.deepcopy(PODS["ok"])
    pods["items"][0]["metadata"]["labels"]["app.kubernetes.io/component"] = "master"
    env, errors, calls, _ = s3_run(Gateway(POD["ok"]), extra_stubs={get_pods("ok"): ok(pods)})
    assert env["status"] == "skipped" and "no S3 gateway pod" in env["reason"]
    assert not [c for c in calls if "--raw" in c]


def test_s3_check_with_the_gateway_pod_not_running_is_unreachable_not_skipped():
    pods = copy.deepcopy(PODS["ok"])
    pods["items"][0]["status"]["phase"] = "Pending"
    env, errors, _, _ = s3_run(Gateway(POD["ok"]), extra_stubs={get_pods("ok"): ok(pods)})
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


def test_s3_check_a_failing_put_is_a_failed_probe_and_the_cleanup_still_runs():
    gw = Gateway(POD["ok"], fail={"put-object": UPSTREAM["500"]})
    env, errors, _, gw = s3_run(gw)
    p = env["outputs"]["probe"]
    assert env["status"] == "ok" and p["passed"] is False and p["anonymousWrite"] is None
    steps = {s["name"]: s for s in p["steps"]}
    assert steps["put-object"]["ok"] is False and steps["put-object"]["error"]["code"] == "internal"
    assert "get-object" not in steps
    assert "cleanup-delete-object" in steps and steps["delete-bucket"]["ok"] is True
    assert p["cleanedUp"] is True and gw.buckets == {}


def test_s3_check_content_mismatch_fails_the_probe():
    class Wrong(Gateway):
        def run(self, argv, cwd=None, check=None):
            if argv[1] == "get" and "runwhen-s3-check/2026" in argv[3]:
                return ok("something else")
            return super().run(argv, cwd)

    env, _, _, gw = s3_run(Wrong(POD["ok"]))
    p = env["outputs"]["probe"]
    assert p["passed"] is False and p["contentMatches"] is False
    assert [s["ok"] for s in p["steps"] if s["name"] == "get-object"] == [False]
    assert gw.buckets == {}


def test_s3_check_an_object_that_survives_delete_fails_the_probe():
    class Stubborn(Gateway):
        def run(self, argv, cwd=None, check=None):
            if argv[1] == "delete" and "runwhen-s3-check/2026" in argv[3]:
                return ok("")  # claims success, keeps the object
            return super().run(argv, cwd)

    env, errors, _, gw = s3_run(Stubborn(POD["ok"]))
    p = env["outputs"]["probe"]
    steps = {s["name"]: s for s in p["steps"]}
    assert p["passed"] is False and steps["confirm-deleted"]["ok"] is False
    assert "can still be read" in steps["confirm-deleted"]["error"]["message"]


def test_s3_check_cleanup_failure_is_reported_and_leaves_the_paths_named():
    gw = Gateway(POD["ok"], fail={"delete-bucket": UPSTREAM["500"]})
    env, errors, _, gw = s3_run(gw)
    p = env["outputs"]["probe"]
    assert env["status"] == "partial" and codes(errors) == [("internal", "s3-check/cleanup")]
    assert (
        p["cleanedUp"] is False and p["leftBehind"] == ["runwhen-s3-check"] and p["passed"] is False
    )


def test_s3_check_rbac_denial_of_the_write_verbs_is_reported_with_permission_denied():
    gw = Gateway(POD["ok"], fail={"put-bucket": rbac("pods/proxy", "update")})
    env, errors, _, gw = s3_run(gw)
    assert env["status"] == "partial" and codes(errors) == [
        ("permission_denied", "s3-check/create-bucket")
    ]
    p = env["outputs"]["probe"]
    assert (
        p["passed"] is False
        and p["bucketCreated"] is False
        and [s["name"] for s in p["steps"]] == ["create-bucket"]
    )


@pytest.mark.parametrize(
    "bucket", ["A", "ab", "has space", "a..b", "-x-", "x" * 64, "a/b", "../etc"]
)
def test_s3_check_refuses_bad_bucket_names_before_any_call(bucket):
    env, errors, calls, _ = s3_run(Gateway(POD["ok"]), bucket=bucket)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_s3_check_unreachable_gateway_fails_the_task():
    env, errors, _, _ = s3_run(Gateway(POD["ok"], fail={"list": UPSTREAM["refused"]}))
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


def test_the_probe_key_and_body_come_from_ctx_now():
    later = datetime(2027, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    with make_context(resource=sts_resource("ok"), settings=SETTINGS, now=later) as tc:
        calls = stub_commands(tc, release_stubs("ok", master=False))
        gw = Gateway(POD["ok"])
        base = tc.ctx.run

        def run_(argv, **kw):
            if "--raw" in argv:
                calls.append(tuple(argv))
                rc, out, err = gw.run(argv, kw.get("cwd"))
                return tc.ctx._finish_run(list(argv), rc, out, err, kw.get("check"))
            return base(argv, **kw)

        tc.ctx.run = run_
        env = run_main(script("s3_check"), tc)
    assert env["outputs"]["probe"]["key"] == "runwhen-s3-check/20270102T030405Z.txt"


# ---------------------------------------------------------------- RBAC / argv ----

RESOURCES = {
    "pods": ("", "pods"),
    "statefulsets": ("apps", "statefulsets"),
    "deployments": ("apps", "deployments"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-l", "-f", "--context"}
PROXY_VERBS = {"get": "get", "replace": "update", "delete": "delete"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs."""
    argv = list(argv)
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if "--raw" in argv:
        assert "/pods/" in positional[1] and "/proxy/" in positional[1], argv
        return {("", "pods/proxy", PROXY_VERBS[verb])}
    assert verb == "get", argv
    group, plural = RESOURCES[positional[1]]
    named = len(positional) > 2 and "-l" not in flags
    return {(group, plural, "get" if named else "list")}


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def needs_of(calls):
    needed = set()
    for argv in calls:
        needed |= rbac_needs(argv)
    return needed


READ_CASES = [
    ("cluster_status", sts_resource("ok"), release_stubs("ok")),
    ("cluster_status", ns_resource(), release_stubs("ok") | {get_pods(None): ok(PODS["ok"])}),
    ("volumes", sts_resource("broken"), release_stubs("broken")),
    ("filer_status", sts_resource("ok"), release_stubs("ok")),
    ("buckets", sts_resource("ok"), bucket_stubs("ok")),
    ("buckets", ns_resource(), bucket_stubs("ok") | {get_pods(None): ok(PODS["ok"])}),
]


@pytest.mark.parametrize("task,resource,stubs", READ_CASES)
def test_declared_permissions_cover_every_kubectl_call_a_read_task_makes(task, resource, stubs):
    env, errors, calls = run(task, resource, stubs)
    assert env["status"] == "ok", errors
    missing = needs_of(calls) - declared(TASKS[task.replace("_", "-")])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


@pytest.mark.parametrize("flow", ["real", "existing-bucket"])
def test_declared_permissions_cover_every_kubectl_call_s3_check_makes(flow):
    gw = Gateway(POD["ok"], buckets={"runwhen-s3-check": {}} if flow == "existing-bucket" else None)
    env, errors, calls, _ = s3_run(gw)
    assert env["status"] == "ok", errors
    needed = needs_of(calls)
    assert {v for _, r, v in needed if r == "pods/proxy"} == {"get", "update", "delete"}
    assert not needed - declared(TASKS["s3-check"])


def test_read_tasks_declare_no_write_verb_on_the_proxy_and_s3_check_the_only_one_that_does():
    for name, task in TASKS.items():
        verbs = {v for _, r, v in declared(task) if r == "pods/proxy"}
        assert verbs == ({"get", "update", "delete"} if name == "s3-check" else {"get"}), name
        assert task["readOnly"] is (name != "s3-check")


def test_no_read_task_issues_a_mutating_request():
    for task, resource, stubs in READ_CASES:
        _, _, calls = run(task, resource, stubs)
        bad = [
            c
            for c in calls
            if {"replace", "delete", "create", "patch", "apply", "exec", "annotate"} & set(c)
        ]
        assert not bad, (task, bad)


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "statefulsets", "s", "-o", "json"]) == {
        ("apps", "statefulsets", "get")
    }
    assert rbac_needs(list(raw("p", 9333, "dir/status"))) == {("", "pods/proxy", "get")}
    assert rbac_needs([*raw("p", 8333, "b/k", "replace"), "-f", "/tmp/x"]) == {
        ("", "pods/proxy", "update")
    }
    assert rbac_needs(list(raw("p", 8333, "b/k", "delete"))) == {("", "pods/proxy", "delete")}


# ------------------------------------------------------------------ secrets ----


def test_no_output_carries_secret_like_content():
    outputs = []
    for task, resource, stubs in READ_CASES[:5]:
        env, _, _ = run(task, resource, stubs)
        outputs.append(json.dumps(env["outputs"]))
    env, _, _, _ = s3_run(Gateway(POD["ok"]))
    outputs.append(json.dumps(env["outputs"]))
    dump = "\n".join(outputs).lower()
    for needle in ("secret", "password", "token", "accesskey", '"env"', "rwtestsecret"):
        assert needle not in dump, needle


def test_the_scripts_never_read_a_secret_or_configmap():
    for src in (SKILL / "lib" / "k8s_seaweedfs_health").glob("*.py"):
        body = src.read_text().lower()
        assert "secrets" not in body.replace("never reads secrets", "") or src.name in (
            "s3check.py",
        ), src.name
    for task, resource, stubs in READ_CASES:
        _, _, calls = run(task, resource, stubs)
        assert not [c for c in calls if "secrets" in c or "configmaps" in c]
