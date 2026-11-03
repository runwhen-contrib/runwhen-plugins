"""Unit tests for k8s-postgres-operator-health: lib/ and the thin scripts, against JSON captured
from the kind-rw-plugins scenario rw-pgop (synthetic Crunchy/Zalando CRs, real postgres pods for
psql, a patronictl stub). Scripts run through runwhen_capability.testing: a real Context with the
V13 semantics (ctx.run raises on non-zero exit, ctx.error side channel, ctx.now fixed) and
stubbed commands; no test reaches a cluster."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_postgres_operator_health import k8s
from k8s_postgres_operator_health import operator as o
from k8s_postgres_operator_health.errors import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-postgres-operator-health"
DATA = Path(__file__).parent / "data" / "k8s_postgres_operator_health"
NOW = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
NS = "rw-pgop"
PC = "postgresclusters.postgres-operator.crunchydata.com"
PG = "postgresqls.acid.zalan.do"
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def load(name):
    return json.loads((DATA / name).read_text())


def text(name):
    return (DATA / name).read_text()


CR = {
    "hippo": load("cr_hippo.json"),
    "hippo-broken": load("cr_hippo_broken.json"),
    "acid-zalando": load("cr_acid_zalando.json"),
    "acid-sync-failed": load("cr_acid_sync_failed.json"),
}
PODS = {
    "hippo": load("pods_hippo.json"),
    "hippo-broken": load("pods_hippo_broken.json"),
    "acid-zalando": load("pods_acid_zalando.json"),
    "acid-sync-failed": {"items": []},
}
KIND_OF = {"hippo": "crunchy", "hippo-broken": "crunchy"}


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"pgop_health_{name}", SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def is_crunchy(name):
    return name in KIND_OF


def rtype(name):
    return "kubernetes/postgrescluster" if is_crunchy(name) else "kubernetes/postgresql"


def resource(name, ns=NS, type_=True):
    plural = "postgresclusters" if is_crunchy(name) else "postgresqls"
    res = {
        "path": f"kubernetes/clusters/kind/namespaces/{ns}/{plural}/{name}",
        "cluster": "kind",
        "namespace": ns,
        "name": name,
    }
    if type_:
        res["type"] = rtype(name)
    return res


def selector(name):
    if is_crunchy(name):
        return f"postgres-operator.crunchydata.com/cluster={name},postgres-operator.crunchydata.com/instance"
    return f"application=spilo,cluster-name={name}"


def get_cr(name):
    return ("kubectl", "-n", NS, "get", PC if is_crunchy(name) else PG, name, "-o", "json")


def get_pods(name):
    return ("kubectl", "-n", NS, "get", "pods", "-l", selector(name), "-o", "json")


def exec_argv(pod, container, *cmd):
    return ("kubectl", "-n", NS, "exec", pod, "-c", container, "--", *cmd)


def container_of(name):
    return "database" if is_crunchy(name) else "postgres"


def patroni_argv(pod, name):
    return exec_argv(pod, container_of(name), "patronictl", "list", "-f", "json")


def psql_argv(pod, name, sql):
    return exec_argv(
        pod,
        container_of(name),
        "psql",
        "-U",
        "postgres",
        "-X",
        "-q",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )


FORBIDDEN = (
    1,
    "",
    'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot %s',
)


def forbidden(what, verb="get"):
    rc, out, err = FORBIDDEN
    return rc, out, err % (what, f'{verb} resource "{what}" in the namespace "{NS}"')


def ok(doc):
    return 0, doc if isinstance(doc, str) else json.dumps(doc), ""


def run(task, name, stubs, settings=None, type_=True, **inputs):
    """Run a script as the executor does: (envelope, errors, calls)."""
    with make_context(
        resource=resource(name, type_=type_),
        settings={
            "maxItems": 200,
            "lagThresholdMb": 100,
            "connectionUtilizationPercent": 80,
            **(settings or {}),
        },
        now=NOW,
    ) as tc:
        calls = stub_commands(tc, stubs)
        env = run_main(script(task), tc, **inputs)
        return env, tc.errors, list(calls)


def standard(name, extra=None):
    stubs = {get_cr(name): ok(CR[name]), get_pods(name): ok(PODS[name])}
    stubs.update(extra or {})
    return stubs


def validate(schema_file, value):
    schema = json.loads((SKILL / "schemas" / schema_file).read_text())
    Draft202012Validator.check_schema(schema)
    return [e.message for e in Draft202012Validator(schema).iter_errors(value)]


def check_outputs(task, env):
    spec = TASKS[task.replace("_", "-")]["outputs"]
    assert set(env["outputs"]) == set(spec)
    for out, s in spec.items():
        assert validate(Path(s["schema"]).name, env["outputs"][out]) == [], (task, out)


# ------------------------------------------------------------ helpers ----


def test_resolve_kind_from_type_then_path():
    assert o.resolve_kind("kubernetes/postgrescluster") == o.CRUNCHY
    assert o.resolve_kind("kubernetes/postgresql") == o.ZALANDO
    assert o.resolve_kind(None, "kubernetes/clusters/k/namespaces/n/postgresqls/x") == o.ZALANDO
    for bad in (
        ("kubernetes/deployment", None),
        (None, "kubernetes/clusters/k/namespaces/n/pods/x"),
        (None, None),
    ):
        with pytest.raises(RwError) as exc:
            o.resolve_kind(*bad)
        assert exc.value.code == "invalid_input"


def test_selectors_are_the_operators_labels():
    assert o.pod_selector(o.CRUNCHY, "hippo") == selector("hippo")
    assert (
        o.pod_selector(o.ZALANDO, "acid-zalando") == "application=spilo,cluster-name=acid-zalando"
    )


def test_limit_text_cuts_at_1024():
    short, cut = o.limit_text("x" * 10)
    assert short == "x" * 10 and cut is False
    long, cut = o.limit_text("y" * 2000)
    assert len(long) == 1024 and cut is True
    assert o.limit_text(None) == (None, False)


@pytest.mark.parametrize(
    "rc,stderr,code",
    [
        (
            1,
            'Error from server (Forbidden): pods "x" is forbidden: cannot create resource "pods/exec"',
            "permission_denied",
        ),
        (
            1,
            'Error from server (NotFound): postgresclusters.postgres-operator.crunchydata.com "x" not found',
            "not_found",
        ),
        (1, "Unable to connect to the server: dial tcp: i/o timeout", "unreachable"),
        (124, "", "timeout"),
        (127, "", "dependency"),
        (
            1,
            'OCI runtime exec failed: exec: "patronictl": executable file not found in $PATH',
            "dependency",
        ),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (1, "something odd", "internal"),
    ],
)
def test_classify(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


# ------------------------------------------------------- cluster-status ----


def test_crunchy_healthy_instances_backups_and_members():
    env, errors, calls = run("cluster_status", "hippo", standard("hippo"))
    assert env["status"] == "ok" and errors == []
    out = env["outputs"]
    c = out["cluster"]
    assert (c["kind"], c["operator"], c["postgresVersion"]) == ("PostgresCluster", "crunchy", "16")
    assert c["instances"]["desired"] == 2 and c["instances"]["ready"] == 2
    assert c["generationObserved"] is True and c["clusterStatus"] is None
    assert [x["type"] for x in c["conditions"]] == [
        "PGBackRestReplicaRepoReady",
        "PGBackRestReplicaCreate",
    ]
    b = out["backups"]
    assert b["supported"] is True and b["configured"] is True
    # the newest completed backup of each type, not the first listed
    assert b["lastIncr"]["completionTime"] == "2026-11-03T18:01:15Z"
    assert b["lastFull"]["completionTime"] == "2026-11-01T02:04:10Z"
    assert b["lastIncr"]["ageSeconds"] == 10725  # 21:00:00 - 18:01:15 at the reference time
    assert b["last"]["type"] == "incr" and b["lastDiff"] is None
    assert b["neverCompleted"] == 0 and b["scheduledCount"] == 3
    assert b["repos"][0]["stanzaCreated"] is True
    m = out["members"]
    assert m["count"] == 2 and m["readyCount"] == 2 and m["selector"] == selector("hippo")
    by_name = {i["name"]: i for i in m["items"]}
    assert (
        by_name["hippo-instance1-abcd-0"]["primary"] is True
        and by_name["hippo-instance1-wxyz-0"]["role"] == "replica"
    )
    assert calls == [get_cr("hippo"), get_pods("hippo")]
    check_outputs("cluster_status", env)


def test_crunchy_broken_not_ready_no_completed_backup():
    env, errors, _ = run("cluster_status", "hippo-broken", standard("hippo-broken"))
    assert env["status"] == "ok" and errors == []
    out = env["outputs"]
    inst = out["cluster"]["instances"]
    assert inst["desired"] == 2 and inst["ready"] == 1 and inst["sets"][0]["ready"] == 1
    assert out["cluster"]["conditions"][0]["status"] == "False"
    b = out["backups"]
    assert b["lastFull"] is None and b["last"] is None  # a failed schedule is not a backup
    assert b["neverCompleted"] == 1 and b["repos"][0]["stanzaCreated"] is False
    assert out["members"]["count"] == 2 and out["members"]["readyCount"] == 1
    check_outputs("cluster_status", env)


def test_missing_scheduled_backups_is_null_never_a_recent_backup():
    # Classic: `date -d ''` is midnight today, so a cluster with no backup status looked fresh.
    obj = copy.deepcopy(CR["hippo"])
    del obj["status"]["pgbackrest"]["scheduledBackups"]
    view = o.backups_view(o.CRUNCHY, obj, NOW)
    assert view["last"] is None and view["lastFull"] is None and view["scheduledCount"] == 0
    del obj["status"]["pgbackrest"]
    view = o.backups_view(o.CRUNCHY, obj, NOW)
    assert view["last"] is None and view["repos"][0]["bound"] is None and view["manual"] is None


def test_backup_entries_without_a_completion_time_are_not_the_latest():
    obj = copy.deepcopy(CR["hippo"])
    obj["status"]["pgbackrest"]["scheduledBackups"].append(
        {"type": "full", "repo": "repo1", "startTime": "2026-11-03T20:59:00Z", "active": 1}
    )
    view = o.backups_view(o.CRUNCHY, obj, NOW)
    assert view["lastFull"]["completionTime"] == "2026-11-01T02:04:10Z"
    assert view["neverCompleted"] == 1


def test_manual_backup_and_restore_are_reported():
    obj = copy.deepcopy(CR["hippo"])
    obj["status"]["pgbackrest"]["manualBackup"] = {
        "id": "m1",
        "finished": False,
        "active": 1,
        "startTime": "2026-11-03T20:50:00Z",
    }
    obj["status"]["pgbackrest"]["restore"] = {"id": "r1", "finished": True, "succeeded": 1}
    view = o.backups_view(o.CRUNCHY, obj, NOW)
    assert view["manual"]["finished"] is False and view["manual"]["active"] == 1
    assert view["restore"]["succeeded"] == 1


def test_zalando_running_counts_ready_from_pods_and_has_no_backup_status():
    env, errors, _ = run("cluster_status", "acid-zalando", standard("acid-zalando"))
    assert env["status"] == "ok" and errors == []
    c = env["outputs"]["cluster"]
    assert (c["kind"], c["operator"], c["clusterStatus"]) == ("postgresql", "zalando", "Running")
    assert c["instances"]["desired"] == 2 and c["instances"]["ready"] == 2
    assert c["instances"]["readySource"] == "pods" and c["conditions"] == []
    assert c["generationObserved"] is None and c["postgresVersion"] == "16"
    assert env["outputs"]["backups"]["supported"] is False
    assert env["outputs"]["members"]["items"][0]["containers"][0]["name"] == "postgres"
    check_outputs("cluster_status", env)


def test_zalando_sync_failed_with_no_pods_is_zero_ready_not_unknown():
    env, errors, _ = run("cluster_status", "acid-sync-failed", standard("acid-sync-failed"))
    assert env["status"] == "ok" and errors == []
    out = env["outputs"]
    assert out["cluster"]["clusterStatus"] == "SyncFailed"
    assert out["cluster"]["instances"]["ready"] == 0 and out["cluster"]["instances"]["desired"] == 2
    assert out["members"]["count"] == 0 and out["members"]["items"] == []


def test_forbidden_pod_list_is_partial_with_null_members_and_unknown_ready():
    stubs = {
        get_cr("acid-zalando"): ok(CR["acid-zalando"]),
        get_pods("acid-zalando"): forbidden("pods", "list"),
    }
    env, errors, _ = run("cluster_status", "acid-zalando", stubs)
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("permission_denied", "members")]
    assert env["outputs"]["members"] is None
    assert env["outputs"]["cluster"]["instances"]["ready"] is None  # unknown, not 0
    assert env["outputs"]["cluster"]["clusterStatus"] == "Running"


def test_crunchy_forbidden_pods_keeps_status_instances():
    stubs = {get_cr("hippo"): ok(CR["hippo"]), get_pods("hippo"): forbidden("pods", "list")}
    env, errors, _ = run("cluster_status", "hippo", stubs)
    assert env["status"] == "partial" and env["outputs"]["members"] is None
    assert env["outputs"]["cluster"]["instances"]["ready"] == 2


def test_crunchy_instance_set_without_replicas_counts_one_and_no_status_is_null_ready():
    obj = copy.deepcopy(CR["hippo"])
    obj["spec"]["instances"] = [{"name": "instance1"}]
    del obj["status"]
    view = o.cluster_view(o.CRUNCHY, obj, None)
    assert view["instances"]["desired"] == 1 and view["instances"]["ready"] is None
    assert view["generationObserved"] is None and view["conditions"] == []


def test_generation_not_observed():
    obj = copy.deepcopy(CR["hippo"])
    obj["metadata"]["generation"] = obj["status"]["observedGeneration"] + 2
    assert o.cluster_view(o.CRUNCHY, obj, None)["generationObserved"] is False


def test_init_containers_are_reported_with_init_true():
    pod = copy.deepcopy(PODS["hippo"]["items"][0])
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "startup",
            "ready": True,
            "restartCount": 2,
            "state": {"terminated": {"reason": "Completed"}},
        }
    ]
    view = o.pod_view(o.CRUNCHY, pod)
    init = [c for c in view["containers"] if c["init"]]
    assert init[0]["name"] == "startup" and init[0]["reason"] == "Completed"
    assert view["restarts"] == 2  # init containers count


def test_conditions_and_containers_are_capped_at_fifty_with_messages_cut():
    obj = copy.deepcopy(CR["hippo"])
    obj["status"]["conditions"] = [
        {"type": f"c{i}", "status": "True", "message": "m" * 3000} for i in range(80)
    ]
    conds = o.cluster_view(o.CRUNCHY, obj, None)["conditions"]
    assert len(conds) == 50 and len(conds[0]["message"]) == 1024 and conds[0]["truncated"] is True


def test_members_list_is_capped_by_max_items_but_count_is_real():
    pods = [copy.deepcopy(PODS["hippo"]["items"][0]) for _ in range(5)]
    for i, p in enumerate(pods):
        p["metadata"]["name"] = f"p{i}"
    view = o.members_view(o.CRUNCHY, pods, "sel", 2)
    assert view["count"] == 5 and len(view["items"]) == 2 and view["truncated"] is True


# --------------------------------------------------------- patroni-status ----


def test_patroni_healthy_leader_and_streaming_replica():
    stubs = standard(
        "hippo", {patroni_argv("hippo-instance1-abcd-0", "hippo"): ok(text("patroni_hippo.json"))}
    )
    env, errors, calls = run("patroni_status", "hippo", stubs)
    assert env["status"] == "ok" and errors == []
    p = env["outputs"]["patroni"]
    assert (
        p["leaderPresent"] is True
        and p["leader"] == "hippo-instance1-abcd-0"
        and p["leaderRunning"] is True
    )
    assert p["cluster"] == "hippo-ha" and p["timelines"] == [3] and p["timelineMismatch"] is False
    assert p["notRunning"] == [] and p["aboveLagThreshold"] == [] and p["maxLagMb"] == 0
    assert p["queried"] == {"pod": "hippo-instance1-abcd-0", "container": "database"}
    replica = next(m for m in p["members"] if m["name"] == "hippo-instance1-wxyz-0")
    assert replica["state"] == "streaming" and replica["running"] is True and replica["lagMb"] == 0
    assert replica["lagUnknown"] is False
    assert calls[-1] == patroni_argv("hippo-instance1-abcd-0", "hippo")
    check_outputs("patroni_status", env)


def test_patroni_no_leader_lagging_and_failed_member_are_facts_not_errors():
    # pod hippo-broken-instance1-uvwx-0 is the Ready one, so it is the one asked
    stubs = standard(
        "hippo-broken",
        {
            patroni_argv("hippo-broken-instance1-uvwx-0", "hippo-broken"): ok(
                text("patroni_hippo_broken.json")
            )
        },
    )
    env, errors, _ = run("patroni_status", "hippo-broken", stubs)
    assert env["status"] == "ok" and errors == []
    p = env["outputs"]["patroni"]
    assert p["leaderPresent"] is False and p["leader"] is None and p["leaderRunning"] is False
    assert p["timelines"] == [4, 5] and p["timelineMismatch"] is True
    assert p["maxLagMb"] == 512 and p["aboveLagThreshold"] == ["hippo-broken-instance1-qrst-0"]
    assert p["notRunning"] == ["hippo-broken-instance1-uvwx-0"]
    failed = next(m for m in p["members"] if m["state"] == "start failed")
    assert failed["running"] is False and failed["lagMb"] is None and failed["lagUnknown"] is True
    check_outputs("patroni_status", env)


def test_lag_threshold_setting_decides_the_flag():
    stubs = standard(
        "hippo-broken",
        {
            patroni_argv("hippo-broken-instance1-uvwx-0", "hippo-broken"): ok(
                text("patroni_hippo_broken.json")
            )
        },
    )
    env, _, _ = run("patroni_status", "hippo-broken", stubs, settings={"lagThresholdMb": 1000})
    assert env["outputs"]["patroni"]["aboveLagThreshold"] == []
    assert env["outputs"]["patroni"]["lagThresholdMb"] == 1000


def test_patroni_standby_leader_counts_as_a_leader_and_pending_restart_is_reported():
    raw = [
        {
            "Cluster": "c",
            "Member": "a",
            "Role": "Standby Leader",
            "State": "running",
            "TL": 2,
            "Pending restart": True,
        },
        {
            "Cluster": "c",
            "Member": "b",
            "Role": "Sync Standby",
            "State": "streaming",
            "TL": 2,
            "Lag in MB": "3",
        },
    ]
    p = o.patroni_view(raw, None, 100, 200)
    assert p["leaderPresent"] is True and p["leader"] == "a"
    assert p["members"][0]["pendingRestart"] is True and p["members"][1]["lagMb"] == 3.0


def test_empty_array_unparsable_output_and_members_missing_facts_are_internal_errors():
    # Classic: empty or unparsable patronictl output produced zero lag and "healthy".
    for bad in ("", "Error: nope", "{}", '["x"]', "[]"):
        with pytest.raises(RwError) as exc:
            o.parse_patroni(bad)
        assert exc.value.code == "internal"
    for field in ("Member", "Role", "State"):
        member = {"Cluster": "c", "Member": "a", "Role": "Leader", "State": "running", "TL": 1}
        del member[field]
        with pytest.raises(RwError) as exc:
            o.parse_patroni(json.dumps([member]))
        assert exc.value.code == "internal" and f"no {field}" in exc.value.message
    ok_member = {"Cluster": "c", "Member": "a", "Role": "Leader", "State": "running"}
    with pytest.raises(RwError) as exc:  # the second member is the one without a State
        o.parse_patroni(json.dumps([ok_member, {**ok_member, "Member": "b", "State": ""}]))
    assert "member 1 has no State" in exc.value.message
    assert o.parse_patroni(json.dumps([ok_member]))[0]["Member"] == "a"


@pytest.mark.parametrize("stdout", ["[]", '[{"Cluster": "hippo-ha", "Member": "x"}]'])
def test_patroni_status_with_an_empty_array_or_members_missing_facts_fails_internal(stdout):
    stubs = standard("hippo", {patroni_argv("hippo-instance1-abcd-0", "hippo"): ok(stdout)})
    env, errors, _ = run("patroni_status", "hippo", stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "internal"
    assert env["outputs"] == {}  # never an all-clear built from nothing


def test_patroni_empty_stdout_fails_the_task_instead_of_reporting_no_lag():
    stubs = standard("hippo", {patroni_argv("hippo-instance1-abcd-0", "hippo"): ok("")})
    env, errors, _ = run("patroni_status", "hippo", stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "internal"


def test_patroni_without_a_running_pod_is_partial_and_everything_is_null():
    env, errors, _ = run("patroni_status", "acid-sync-failed", standard("acid-sync-failed"))
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("not_found", "members")]
    p = env["outputs"]["patroni"]
    assert p["queried"] is None and p["leaderPresent"] is None and p["members"] == []
    check_outputs("patroni_status", env)


def test_patroni_exec_failures_are_classified():
    argv = patroni_argv("hippo-instance1-abcd-0", "hippo")
    cases = [
        (forbidden("pods/exec", "create"), "permission_denied"),
        (
            (
                1,
                "",
                'OCI runtime exec failed: exec: "patronictl": executable file not found in $PATH',
            ),
            "dependency",
        ),
        (
            (
                1,
                "",
                "Error: Can not find suitable configuration of distributed configuration store",
            ),
            "internal",
        ),
        (
            (1, "", 'Error from server (NotFound): pods "hippo-instance1-abcd-0" not found'),
            "internal",
        ),  # pod went away
    ]
    for answer, code in cases:
        env, errors, _ = run("patroni_status", "hippo", standard("hippo", {argv: answer}))
        assert env["status"] == "failed" and errors[0]["code"] == code, (answer, errors)


def test_patroni_picks_a_ready_running_pod_and_skips_deleting_ones():
    pods = copy.deepcopy(PODS["hippo"]["items"])
    pods[0]["metadata"]["deletionTimestamp"] = "2026-11-03T20:59:00Z"
    chosen = o.exec_candidates(o.CRUNCHY, pods)
    assert [p["metadata"]["name"] for p in chosen] == ["hippo-instance1-wxyz-0"]
    pods[1]["status"]["phase"] = "Pending"
    assert o.exec_candidates(o.CRUNCHY, pods) == []


# ------------------------------------------------------------ connections ----


def conn_stubs(name, pod, doc=None, answer=None):
    sql = o.CONNECTIONS_SQL
    return standard(
        name,
        {
            psql_argv(pod, name, sql): answer
            or ok(doc if doc is not None else text("psql_connections.json"))
        },
    )


def test_connections_on_the_primary_from_one_psql_document():
    env, errors, calls = run("connections", "hippo", conn_stubs("hippo", "hippo-instance1-abcd-0"))
    assert env["status"] == "ok" and errors == []
    c = env["outputs"]["connections"]
    assert c["queried"] == {"pod": "hippo-instance1-abcd-0", "container": "database"}
    assert c["maxConnections"] == 100 and c["usableConnections"] == 97 and c["total"] == 0
    assert c["utilizationPercent"] == 0.0 and c["aboveThreshold"] is False
    assert c["waitingLocks"] == 0 and c["inRecovery"] is False and c["primaryCandidates"] == 1
    assert calls[-1] == psql_argv("hippo-instance1-abcd-0", "hippo", o.CONNECTIONS_SQL)
    check_outputs("connections", env)


def test_connections_saturation_and_ages_come_from_the_document():
    doc = {
        "inRecovery": False,
        "maxConnections": 100,
        "superuserReserved": 3,
        "total": 90,
        "byState": {"idle": 40, "active": 30, "idle in transaction": 20},
        "longestQuerySeconds": 1800,
        "longestIdleInTransactionSeconds": 600,
        "waitingLocks": 4,
        "sessionsWaitingOnLock": 3,
    }
    env, _, _ = run("connections", "hippo", conn_stubs("hippo", "hippo-instance1-abcd-0", doc))
    c = env["outputs"]["connections"]
    assert c["utilizationPercent"] == 92.8 and c["aboveThreshold"] is True
    assert c["byState"] == {"active": 30, "idle": 40, "idle in transaction": 20}
    assert (c["longestQuerySeconds"], c["longestIdleInTransactionSeconds"], c["waitingLocks"]) == (
        1800,
        600,
        4,
    )
    env, _, _ = run(
        "connections",
        "hippo",
        conn_stubs("hippo", "hippo-instance1-abcd-0", doc),
        settings={"connectionUtilizationPercent": 95},
    )
    assert env["outputs"]["connections"]["aboveThreshold"] is False


def test_unreadable_max_connections_fails_instead_of_passing():
    # Classic: an unreadable max_connections only added a report line and no issue.
    doc = {
        "maxConnections": None,
        "superuserReserved": 3,
        "total": 1,
        "byState": {},
        "waitingLocks": 0,
    }
    env, errors, _ = run("connections", "hippo", conn_stubs("hippo", "hippo-instance1-abcd-0", doc))
    assert env["status"] == "failed" and errors[0]["code"] == "internal"


@pytest.mark.parametrize("out", ["", "psql: error", "[]", "null"])
def test_psql_output_that_is_not_a_document_fails(out):
    env, errors, _ = run(
        "connections", "hippo", conn_stubs("hippo", "hippo-instance1-abcd-0", answer=(0, out, ""))
    )
    assert env["status"] == "failed" and errors[0]["code"] == "internal"


def test_no_primary_pod_is_not_found_and_nothing_is_exec_d():
    env, errors, calls = run("connections", "hippo-broken", standard("hippo-broken"))
    assert env["status"] == "failed" and errors[0]["code"] == "not_found"
    assert "primary role label" in errors[0]["message"]
    assert not any("exec" in c for c in calls)


def test_stale_label_during_failover_is_visible_as_in_recovery_and_two_primaries():
    pods = copy.deepcopy(PODS["hippo"])
    pods["items"][1]["metadata"]["labels"][o.KINDS[o.CRUNCHY]["role_label"]] = "master"
    stubs = {
        get_cr("hippo"): ok(CR["hippo"]),
        get_pods("hippo"): ok(pods),
        psql_argv("hippo-instance1-abcd-0", "hippo", o.CONNECTIONS_SQL): ok(
            {**json.loads(text("psql_connections.json")), "inRecovery": True}
        ),
    }
    env, _, _ = run("connections", "hippo", stubs)
    c = env["outputs"]["connections"]
    assert c["inRecovery"] is True and c["primaryCandidates"] == 2


def test_psql_failures_are_classified():
    argv = psql_argv("hippo-instance1-abcd-0", "hippo", o.CONNECTIONS_SQL)
    cases = [
        (forbidden("pods/exec", "create"), "permission_denied"),
        ((1, "", 'exec: "psql": executable file not found in $PATH'), "dependency"),
        (
            (
                2,
                "",
                'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed',
            ),
            "internal",
        ),
    ]
    for answer, code in cases:
        env, errors, _ = run("connections", "hippo", standard("hippo", {argv: answer}))
        assert env["status"] == "failed" and errors[0]["code"] == code


def test_connections_query_reads_no_query_text_and_leaves_its_own_session_out():
    sql = o.CONNECTIONS_SQL
    assert "query," not in sql and ".query" not in sql.replace("query_start", "")
    assert sql.count("pg_backend_pid()") >= 4


# --------------------------------------------------------------- settings ----


def test_settings_lists_non_default_rows_with_name_setting_unit_source():
    stubs = standard(
        "hippo",
        {
            psql_argv("hippo-instance1-abcd-0", "hippo", o.SETTINGS_SQL): ok(
                text("psql_settings.json")
            )
        },
    )
    env, errors, _ = run("settings", "hippo", stubs)
    assert env["status"] == "ok" and errors == []
    s = env["outputs"]["settings"]
    names = {i["name"] for i in s["items"]}
    assert {"listen_addresses", "data_directory"} <= names
    row = next(i for i in s["items"] if i["name"] == "listen_addresses")
    assert row == {
        "name": "listen_addresses",
        "setting": "*",
        "truncated": False,
        "unit": None,
        "source": "configuration file",
        "redacted": False,
    }
    assert (
        "application_name" not in names and "statement_timeout" not in names
    )  # this session's own
    check_outputs("settings", env)


def test_settings_never_returns_names_matching_the_exclusion_pattern():
    doc = {
        "items": [
            {
                "name": "ssl_key_file",
                "setting": "/etc/key.pem",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "password_encryption",
                "setting": "scram-sha-256",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "krb_server_keyfile",
                "setting": "FILE:/x",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "ssl_cert_file",
                "setting": "/etc/c.pem",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "my_secret_thing",
                "setting": "x",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "max_connections",
                "setting": "200",
                "unit": None,
                "source": "configuration file",
            },
        ],
        "excluded": 5,
    }
    view = o.settings_view(doc, None, 200)  # the query is trusted for nothing: filtered again here
    assert [i["name"] for i in view["items"]] == ["max_connections"]
    assert view["excludedCount"] == 5
    assert o.EXCLUDED_PATTERN in o.SETTINGS_SQL  # the query and the filter use one expression
    assert o.SETTINGS_SQL.count(o.EXCLUDED_PATTERN) == 2 and "@EXCLUDED@" not in o.SETTINGS_SQL


@pytest.mark.parametrize(
    "name",
    [
        "ssl_key_file", "ssl_cert_file", "ssl_ca_file", "ssl_crl_file", "krb_server_keyfile",
        "password_encryption", "ssl_passphrase_command", "ssl_passphrase_command_supports_reload",
        "my_secret_thing", "api_token", "auth_token", "db_credentials", "db_credential", "vault.token",
        "foo_dsn", "jdbc_uri", "auth", "pg_auth", "auth_method", "encryption_key", "keys", "Key",
        "ENCRYPTION_KEY", "some.password",
    ],
)  # fmt: skip
def test_settings_exclusion_covers_names_that_can_hold_a_secret(name):
    assert o.EXCLUDED_NAME.search(name), name
    doc = {"items": [{"name": name, "setting": "x", "unit": None, "source": "configuration file"}]}
    assert o.settings_view(doc, None, 10)["items"] == []


@pytest.mark.parametrize(
    "name",
    [
        "autovacuum", "autovacuum_max_workers", "autovacuum_vacuum_scale_factor",
        "log_autovacuum_min_duration", "authentication_timeout", "row_security",
        "tcp_keepalives_idle", "tcp_user_timeout", "default_text_search_config", "max_connections",
        "shared_buffers", "listen_addresses", "ssl_min_protocol_version", "ssl_ciphers",
        "config_file", "hba_file", "unix_socket_permissions", "scram_iterations", "monkey_patch",
        "bonjour_name", "wal_keep_size", "archive_timeout", "cluster_name", "synchronous_commit",
    ],
)  # fmt: skip
def test_settings_exclusion_does_not_swallow_ordinary_names(name):
    assert not o.EXCLUDED_NAME.search(name), name
    doc = {"items": [{"name": name, "setting": "v", "unit": None, "source": "configuration file"}]}
    assert [i["name"] for i in o.settings_view(doc, None, 10)["items"]] == [name]


def test_settings_withholds_the_values_of_dotted_extension_settings():
    doc = {
        "items": [
            {"name": "pgaudit.log", "setting": "write, ddl", "unit": None, "source": "configuration file"},
            {"name": "auto_explain.log_min_duration", "setting": "250", "unit": "ms", "source": "configuration file"},
            {"name": "pg_stat_statements.max", "setting": "10000", "unit": None, "source": "configuration file"},
            {"name": "cron.database_name", "setting": "appdb", "unit": None, "source": "configuration file"},
            {"name": "shared_buffers", "setting": "16384", "unit": "8kB", "source": "configuration file"},
        ],
        "excluded": 0,
    }  # fmt: skip
    view = o.settings_view(doc, None, 200)
    rows = {i["name"]: i for i in view["items"]}
    for name in (
        "pgaudit.log",
        "auto_explain.log_min_duration",
        "pg_stat_statements.max",
        "cron.database_name",
    ):
        assert rows[name]["setting"] is None and rows[name]["redacted"] is True, name
        assert rows[name]["source"] == "configuration file"  # the row stays: it is set
    assert (
        rows["shared_buffers"]["setting"] == "16384" and rows["shared_buffers"]["redacted"] is False
    )
    dump = json.dumps(view)
    assert "write, ddl" not in dump and "appdb" not in dump


def test_settings_withholds_values_that_can_hold_a_connection_string_or_command():
    doc = {
        "items": [
            {
                "name": "primary_conninfo",
                "setting": "host=10.0.0.1 user=repl password=hunter2",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "archive_command",
                "setting": "wal-g wal-push %p",
                "unit": None,
                "source": "configuration file",
            },
            {
                "name": "some_endpoint",
                "setting": "postgres://u:pw@host/db",
                "unit": None,
                "source": "configuration file",
            },
            {"name": "work_mem", "setting": "64", "unit": "kB", "source": "configuration file"},
        ],
        "excluded": 0,
    }
    view = o.settings_view(doc, None, 200)
    rows = {i["name"]: i for i in view["items"]}
    for name in ("primary_conninfo", "archive_command", "some_endpoint"):
        assert rows[name]["setting"] is None and rows[name]["redacted"] is True
    assert rows["work_mem"]["setting"] == "64" and rows["work_mem"]["unit"] == "kB"
    assert "hunter2" not in json.dumps(view) and "pw@" not in json.dumps(view)


def test_settings_cap_and_value_truncation():
    doc = {
        "items": [
            {"name": f"s{i:03d}", "setting": "v" * 2000, "unit": None, "source": "x"}
            for i in range(30)
        ],
        "excluded": 0,
    }
    view = o.settings_view(doc, None, 10)
    assert view["count"] == 30 and len(view["items"]) == 10 and view["truncated"] is True
    assert len(view["items"][0]["setting"]) == 1024 and view["items"][0]["truncated"] is True


def test_settings_without_items_fails():
    with pytest.raises(RwError):
        o.settings_view({}, None, 10)


# ----------------------------------------------- failure of the target ----


@pytest.mark.parametrize("task", ["cluster_status", "patroni_status", "connections", "settings"])
@pytest.mark.parametrize("name", ["hippo", "acid-zalando"])
def test_missing_target_is_not_found_in_every_task(task, name):
    err = f'Error from server (NotFound): {PC if is_crunchy(name) else PG} "{name}" not found'
    env, errors, calls = run(task, name, {get_cr(name): (1, "", err)})
    assert env["status"] == "failed" and errors[0]["code"] == "not_found"
    assert calls == [get_cr(name)]


@pytest.mark.parametrize("task", ["cluster_status", "patroni_status", "connections", "settings"])
def test_forbidden_target_and_unreachable_api_fail_every_task(task):
    env, errors, _ = run(task, "hippo", {get_cr("hippo"): forbidden(PC)})
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"
    env, errors, _ = run(
        task,
        "hippo",
        {
            get_cr("hippo"): (
                1,
                "",
                "Unable to connect to the server: dial tcp 127.0.0.1:6443: connection refused",
            )
        },
    )
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


@pytest.mark.parametrize("task", ["patroni_status", "connections", "settings"])
def test_forbidden_pod_list_fails_the_exec_tasks(task):
    stubs = {get_cr("hippo"): ok(CR["hippo"]), get_pods("hippo"): forbidden("pods", "list")}
    env, errors, _ = run(task, "hippo", stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"


def test_wrong_target_type_is_refused_before_any_kubectl_call():
    with make_context(
        resource={
            "path": "kubernetes/clusters/kind/namespaces/x/deployments/web",
            "cluster": "kind",
            "namespace": "x",
            "name": "web",
            "type": "kubernetes/deployment",
        },
        settings={"maxItems": 200},
        now=NOW,
    ) as tc:
        calls = stub_commands(tc, {})
        env = run_main(script("cluster_status"), tc)
        errors = tc.errors
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_target_kind_falls_back_to_the_path():
    env, errors, _ = run("cluster_status", "acid-zalando", standard("acid-zalando"), type_=False)
    assert env["status"] == "ok" and env["outputs"]["cluster"]["kind"] == "postgresql"


def test_kubectl_json_raises_when_ctx_run_does_not():
    class Done:
        returncode, stdout, stderr = (
            1,
            "",
            'Error from server (Forbidden): x is forbidden: cannot get resource "x"',
        )

    class Ctx:
        def run(self, argv, **kw):
            return Done()

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "permission_denied"


# ------------------------------------------------------------ RBAC / argv ----

RESOURCES = {
    "pods": ("", "pods"),
    PC: ("postgres-operator.crunchydata.com", "postgresclusters"),
    PG: ("acid.zalan.do", "postgresqls"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-c", "-l", "--selector", "--field-selector", "--context"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs."""
    argv = list(argv)
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
        named = len(positional) > 2 and "-l" not in flags
        return {(group, plural, "get" if named else "list")}
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
    "task,name,extra",
    [
        ("cluster_status", "hippo", {}),
        ("cluster_status", "acid-zalando", {}),
        (
            "patroni_status",
            "hippo",
            {patroni_argv("hippo-instance1-abcd-0", "hippo"): ok(text("patroni_hippo.json"))},
        ),
        (
            "patroni_status",
            "acid-zalando",
            {patroni_argv("acid-zalando-0", "acid-zalando"): ok(text("patroni_acid_zalando.json"))},
        ),
        (
            "connections",
            "hippo",
            {
                psql_argv("hippo-instance1-abcd-0", "hippo", o.CONNECTIONS_SQL): ok(
                    text("psql_connections.json")
                )
            },
        ),
        (
            "settings",
            "acid-zalando",
            {
                psql_argv("acid-zalando-0", "acid-zalando", o.SETTINGS_SQL): ok(
                    text("psql_settings.json")
                )
            },
        ),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, name, extra):
    env, errors, calls = run(task, name, standard(name, extra))
    assert env["status"] == "ok", errors
    needed = set()
    for argv in calls:
        needed |= rbac_needs(argv)
    missing = needed - declared(TASKS[task.replace("_", "-")])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_no_task_declares_a_verb_it_does_not_use():
    # cluster-status is the only task that must work without pods/exec
    perms = declared(TASKS["cluster-status"])
    assert not any(r == "pods/exec" for _, r, _ in perms)
    assert ("", "pods", "get") not in perms


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", PC, "n", "-o", "json"]) == {
        ("postgres-operator.crunchydata.com", "postgresclusters", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "exec", "p", "-c", "c", "--", "psql"]) == {
        ("", "pods", "get"),
        ("", "pods/exec", "create"),
    }


# ------------------------------------------------------------ no secrets ----


def test_no_script_output_contains_password_or_env_values():
    stubs = standard(
        "hippo",
        {
            patroni_argv("hippo-instance1-abcd-0", "hippo"): ok(text("patroni_hippo.json")),
            psql_argv("hippo-instance1-abcd-0", "hippo", o.SETTINGS_SQL): ok(
                text("psql_settings.json")
            ),
            psql_argv("hippo-instance1-abcd-0", "hippo", o.CONNECTIONS_SQL): ok(
                text("psql_connections.json")
            ),
        },
    )
    for task in ("cluster_status", "patroni_status", "connections", "settings"):
        env, _, _ = run(task, "hippo", stubs)
        dump = json.dumps(env["outputs"]).lower()
        assert (
            "password" not in dump
            and "hunter" not in dump
            and "postgres_host_auth_method" not in dump
        )
