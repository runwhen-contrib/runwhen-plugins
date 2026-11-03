"""Unit tests for k8s-postgres-operator-ops: lib/ and the thin scripts, against JSON captured from
the kind-rw-plugins scenario rw-pgop (synthetic Crunchy/Zalando CRs, member pods, a patronictl
stub). Scripts run through runwhen_capability.testing (a real Context, V13 semantics, stubbed
commands); no test reaches a cluster and none writes."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_postgres_operator_ops import fetch
from k8s_postgres_operator_ops import operator as o
from k8s_postgres_operator_ops.errors import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-postgres-operator-ops"
DATA = Path(__file__).parent / "data" / "k8s_postgres_operator_ops"
NOW = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-11-03T21:00:00Z"
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
PATRONI = {
    "hippo": text("patroni_hippo.json"),
    "hippo-broken": text("patroni_hippo_broken.json"),
    "acid-zalando": text("patroni_acid_zalando.json"),
}
CRUNCHY = ("hippo", "hippo-broken")


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"pgop_ops_{name}", SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def is_crunchy(name):
    return name in CRUNCHY


def resource(name, ns=NS):
    plural = "postgresclusters" if is_crunchy(name) else "postgresqls"
    return {
        "path": f"kubernetes/clusters/kind/namespaces/{ns}/{plural}/{name}",
        "cluster": "kind",
        "namespace": ns,
        "name": name,
        "type": "kubernetes/postgrescluster" if is_crunchy(name) else "kubernetes/postgresql",
    }


def selector(name):
    if is_crunchy(name):
        return f"postgres-operator.crunchydata.com/cluster={name},postgres-operator.crunchydata.com/instance"
    return f"application=spilo,cluster-name={name}"


def container_of(name):
    return "database" if is_crunchy(name) else "postgres"


def get_cr(name):
    return ("kubectl", "-n", NS, "get", PC if is_crunchy(name) else PG, name, "-o", "json")


def get_pods(name):
    return ("kubectl", "-n", NS, "get", "pods", "-l", selector(name), "-o", "json")


def exec_argv(pod, name, *cmd):
    return ("kubectl", "-n", NS, "exec", pod, "-c", container_of(name), "--", *cmd)


def list_argv(pod, name):
    return exec_argv(pod, name, "patronictl", "list", "-f", "json")


def patch_argv(name, at=NOW_ISO, dry=False):
    patch = {"spec": {"metadata": {"annotations": {o.RESTART_ANNOTATION: at}}}}
    return (
        "kubectl", "-n", NS, "patch", PC, name, "--type=merge", "-p", json.dumps(patch),
        *(("--dry-run=server",) if dry else ()),
    )  # fmt: skip


def ok(doc="", rc=0):
    return rc, doc if isinstance(doc, str) else json.dumps(doc), ""


def forbidden(what, verb="get"):
    return (
        1,
        "",
        f'Error from server (Forbidden): {what} is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot {verb} resource "{what}" in the namespace "{NS}"',
    )


def standard(name, extra=None, pods=None, cr=None):
    stubs = {get_cr(name): ok(cr or CR[name]), get_pods(name): ok(pods or PODS[name])}
    stubs.update(extra or {})
    return stubs


def run(task, name, stubs, dry=False, hook=None, **inputs):
    """Run a script as the executor does: (envelope, errors, calls)."""
    with make_context(
        resource=resource(name), settings={"maxItems": 200}, dry_run=dry, now=NOW
    ) as tc:
        calls = stub_commands(tc, stubs)
        if hook:
            inner = tc.ctx.run

            def run_and_hook(argv, *a, **kw):
                result = inner(argv, *a, **kw)
                hook(tuple(argv), tc)
                return result

            tc.ctx.run = run_and_hook
        env = run_main(script(task), tc, **inputs)
        return env, tc.errors, list(calls)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Polling sleeps advance a fake clock instead of waiting."""
    clock = SimpleNamespace(t=0.0)
    clock.monotonic = lambda: clock.t
    clock.sleep = lambda s: setattr(clock, "t", clock.t + s)
    monkeypatch.setattr(fetch, "time", clock)
    return clock


def validate(schema_file, value):
    schema = json.loads((SKILL / "schemas" / schema_file).read_text())
    Draft202012Validator.check_schema(schema)
    return [e.message for e in Draft202012Validator(schema).iter_errors(value)]


def check_outputs(task, env):
    spec = TASKS[task.replace("_", "-")]["outputs"]
    assert set(env["outputs"]) == set(spec)
    for out, s in spec.items():
        assert validate(Path(s["schema"]).name, env["outputs"][out]) == [], (task, out)


def writes(calls):
    """The mutating calls among the recorded argv (patronictl write sub-commands, kubectl patch)."""
    out = []
    for c in calls:
        if "patch" in c and "--dry-run=server" not in c:
            out.append(c)
        if "exec" in c and "patronictl" in c and c[c.index("patronictl") + 1] != "list":
            out.append(c)
    return out


# ------------------------------------------------------------- helpers ----


def test_clean_member_accepts_names_and_refuses_anything_an_option_could_mean():
    assert o.clean_member(" hippo-instance1-0 ") == "hippo-instance1-0"
    for bad in (None, "", "  ", "--force", "-x", "a b", "a;b", "a$(x)", "../x", "x" * 300):
        with pytest.raises(RwError) as exc:
            o.clean_member(bad)
        assert exc.value.code == "invalid_input"


def test_timeout_bounds():
    assert (
        o.check_timeout("0s") == 0
        and o.check_timeout("2m") == 120
        and o.check_timeout("240s") == 240
    )
    for bad in ("241s", "5x", "", "-1s", "soon"):
        with pytest.raises(RwError) as exc:
            o.check_timeout(bad)
        assert exc.value.code == "invalid_input"


def test_patroni_cluster_name_is_read_not_derived():
    assert o.patroni_cluster_name(o.parse_patroni(PATRONI["hippo"])) == "hippo-ha"
    with pytest.raises(RwError):
        o.patroni_cluster_name([{"Member": "a"}])
    for bad in ("", "oops", "{}"):
        with pytest.raises(RwError) as exc:
            o.parse_patroni(bad)
        assert exc.value.code == "internal"


def views(name):
    return o.members(o.parse_patroni(PATRONI[name]))


def test_plan_switchover_with_a_healthy_leader_and_no_candidate():
    mode, leader, chosen, by = o.plan_switchover(views("hippo"), None)
    assert (mode, leader["name"], chosen, by) == ("switchover", "hippo-instance1-0", None, None)


def test_plan_switchover_with_a_named_candidate():
    mode, leader, chosen, by = o.plan_switchover(views("hippo"), "hippo-instance2-0")
    assert (mode, chosen["name"], by) == ("switchover", "hippo-instance2-0", "input")


@pytest.mark.parametrize(
    "candidate,why",
    [("hippo-instance1-0", "already the leader"), ("nope", "not a member")],
)
def test_plan_switchover_refuses_a_candidate_that_cannot_be_promoted(candidate, why):
    with pytest.raises(RwError) as exc:
        o.plan_switchover(views("hippo"), candidate)
    assert exc.value.code == "invalid_input" and why in exc.value.message


def test_plan_failover_picks_the_running_replica_with_least_lag_when_no_leader():
    raw = o.parse_patroni(PATRONI["hippo-broken"])
    raw.append(
        {
            "Cluster": "hippo-broken-ha",
            "Member": "c",
            "Role": "Replica",
            "State": "streaming",
            "TL": 5,
            "Lag in MB": 3,
        }
    )
    raw.append(
        {
            "Cluster": "hippo-broken-ha",
            "Member": "d",
            "Role": "Replica",
            "State": "streaming",
            "TL": 5,
            "Lag in MB": "unknown",
        }
    )
    mode, leader, chosen, by = o.plan_switchover(o.members(raw), None)
    assert (mode, leader, chosen["name"], by) == ("failover", None, "c", "auto")


def test_plan_failover_when_the_leader_is_not_running():
    raw = o.parse_patroni(PATRONI["hippo"])
    raw[0]["State"] = "stopped"
    mode, leader, chosen, by = o.plan_switchover(o.members(raw), None)
    assert mode == "failover" and leader["name"] == "hippo-instance1-0"
    assert chosen["name"] == "hippo-instance2-0" and by == "auto"


def test_plan_failover_refuses_a_candidate_that_is_not_up_and_with_no_one_to_promote():
    with pytest.raises(RwError) as exc:
        o.plan_switchover(views("hippo-broken"), "hippo-broken-instance2-0")  # start failed
    assert exc.value.code == "invalid_input" and "not running or streaming" in exc.value.message
    raw = o.parse_patroni(PATRONI["hippo-broken"])
    raw[0]["State"] = "stopped"
    with pytest.raises(RwError) as exc:
        o.plan_switchover(o.members(raw), None)
    assert exc.value.code == "invalid_input" and "nothing was changed" in exc.value.message


def test_check_reinit_refuses_the_leader_and_unknown_members():
    v = views("hippo")
    assert o.check_reinit(v, "hippo-instance2-0")["name"] == "hippo-instance2-0"
    with pytest.raises(RwError) as exc:
        o.check_reinit(v, "hippo-instance1-0")
    assert exc.value.code == "invalid_input" and "leader" in exc.value.message
    with pytest.raises(RwError) as exc:
        o.check_reinit(v, "ghost")
    assert "hippo-instance1-0" in exc.value.message  # lists the members


def test_standby_leader_is_a_leader_for_reinit():
    raw = [{"Cluster": "c", "Member": "a", "Role": "Standby Leader", "State": "running"}]
    with pytest.raises(RwError):
        o.check_reinit(o.members(raw), "a")


def test_exec_pod_prefers_ready_then_not_avoided_then_not_primary():
    pods = PODS["hippo"]["items"]
    assert (
        o.exec_pod(o.CRUNCHY, pods)["metadata"]["name"] == "hippo-instance2-0"
    )  # replica before primary
    assert (
        o.exec_pod(o.CRUNCHY, pods, avoid=("hippo-instance2-0",))["metadata"]["name"]
        == "hippo-instance1-0"
    )
    assert o.exec_pod(o.CRUNCHY, []) is None
    deleting = copy.deepcopy(pods)
    for p in deleting:
        p["metadata"]["deletionTimestamp"] = "2026-11-03T20:00:00Z"
    assert o.exec_pod(o.CRUNCHY, deleting) is None


def test_gitops_signals_report_keys_only():
    obj = copy.deepcopy(CR["hippo"])
    obj["metadata"]["labels"] = {"kustomize.toolkit.fluxcd.io/name": "apps", "x": "y"}
    view = o.target_view(o.CRUNCHY, obj, "hippo-ha")
    assert view["gitopsManaged"] is True and view["gitops"]["tool"] == "flux"
    assert view["gitops"]["signals"] == [
        {"kind": "label", "key": "kustomize.toolkit.fluxcd.io/name", "tool": "flux"}
    ]
    assert o.target_view(o.CRUNCHY, CR["hippo"])["gitopsManaged"] is False


def test_resolve_kind():
    assert o.resolve_kind("kubernetes/postgresql") == o.ZALANDO
    assert (
        o.resolve_kind(None, "kubernetes/clusters/k/namespaces/n/postgresclusters/x") == o.CRUNCHY
    )
    with pytest.raises(RwError) as exc:
        o.resolve_kind("kubernetes/deployment")
    assert exc.value.code == "invalid_input"


# --------------------------------------------------------- reinit-member ----


def test_reinit_dry_run_checks_everything_and_runs_no_write():
    stubs = standard("hippo", {list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"])})
    env, errors, calls = run("reinit_member", "hippo", stubs, dry=True, member="hippo-instance2-0")
    assert env["status"] == "ok" and errors == [] and env["dryRun"] is True
    r = env["outputs"]["reinit"]
    assert (r["applied"], r["wouldRun"]) == (False, True)
    assert r["command"] == {
        "tool": "patronictl",
        "subcommand": "reinit",
        "cluster": "hippo-ha",
        "member": "hippo-instance2-0",
    }
    assert (
        r["stateBefore"] == "streaming"
        and r["role"] == "Replica"
        and r["leader"] == "hippo-instance1-0"
    )
    assert r["output"] is None and r["stateAfter"] is None
    assert env["outputs"]["target"]["patroniCluster"] == "hippo-ha"
    assert writes(calls) == []
    # the pod used is not the member being wiped
    assert r["ranIn"] == {"pod": "hippo-instance1-0", "container": "database"}
    check_outputs("reinit_member", env)


def test_reinit_runs_patronictl_reinit_force_with_the_patroni_cluster_name():
    write = exec_argv(
        "hippo-instance1-0",
        "hippo",
        "patronictl",
        "reinit",
        "hippo-ha",
        "hippo-instance2-0",
        "--force",
    )
    after = json.loads(PATRONI["hippo"])
    after[1]["State"] = "creating replica"
    stubs = standard(
        "hippo",
        {
            list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]),
            write: ok("Success: reinitialize for member hippo-instance2-0\n"),
        },
    )

    def hook(argv, tc):
        if argv == write:
            stub_commands(tc, {list_argv("hippo-instance1-0", "hippo"): ok(after)})

    env, errors, calls = run("reinit_member", "hippo", stubs, hook=hook, member="hippo-instance2-0")
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["reinit"]
    assert r["applied"] is True and r["wouldRun"] is False
    assert r["output"] == "Success: reinitialize for member hippo-instance2-0"
    assert r["stateBefore"] == "streaming" and r["stateAfter"] == "creating replica"
    assert writes(calls) == [write]
    check_outputs("reinit_member", env)


def test_reinit_refuses_the_leader_before_any_write():
    stubs = standard(
        "hippo",
        {
            list_argv("hippo-instance2-0", "hippo"): ok(PATRONI["hippo"]),
            list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]),
        },
    )
    env, errors, calls = run("reinit_member", "hippo", stubs, member="hippo-instance1-0")
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "invalid_input"
        and "leader" in errors[0]["message"]
    )
    assert writes(calls) == []


def test_reinit_of_a_name_that_is_not_a_member_lists_the_members():
    stubs = standard("hippo", {list_argv("hippo-instance2-0", "hippo"): ok(PATRONI["hippo"])})
    env, errors, calls = run("reinit_member", "hippo", stubs, member="ghost")
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert "hippo-instance2-0" in errors[0]["message"] and writes(calls) == []


@pytest.mark.parametrize("bad", ["", "--force", "a b"])
def test_reinit_bad_member_input_fails_before_any_kubectl_call(bad):
    env, errors, calls = run("reinit_member", "hippo", {}, member=bad)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_reinit_when_patronictl_fails_the_task_fails_with_its_message():
    # Classic: a failed reinit was reported as "Members Were Reinitialized".
    write = exec_argv(
        "hippo-instance1-0",
        "hippo",
        "patronictl",
        "reinit",
        "hippo-ha",
        "hippo-instance2-0",
        "--force",
    )
    stubs = standard(
        "hippo",
        {
            list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]),
            write: (1, "", "Error: Failed to reinitialize: member is not a replica"),
        },
    )
    env, errors, _ = run("reinit_member", "hippo", stubs, member="hippo-instance2-0")
    assert env["status"] == "failed" and errors[0]["code"] == "internal"
    assert "Failed to reinitialize" in errors[0]["message"]


def test_reinit_read_back_failure_is_reported_but_keeps_the_result():
    write = exec_argv(
        "hippo-instance1-0",
        "hippo",
        "patronictl",
        "reinit",
        "hippo-ha",
        "hippo-instance2-0",
        "--force",
    )
    stubs = standard(
        "hippo",
        {list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]), write: ok("Success")},
    )

    def hook(argv, tc):
        if argv == write:
            stub_commands(
                tc,
                {
                    list_argv("hippo-instance1-0", "hippo"): (
                        1,
                        "",
                        "Unable to connect to the server: connection refused",
                    )
                },
            )

    env, errors, _ = run("reinit_member", "hippo", stubs, hook=hook, member="hippo-instance2-0")
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("unreachable", "verify")
    ]
    assert (
        env["outputs"]["reinit"]["applied"] is True
        and env["outputs"]["reinit"]["stateAfter"] is None
    )


def test_reinit_with_no_running_pod_changes_nothing():
    env, errors, calls = run(
        "reinit_member", "acid-sync-failed", standard("acid-sync-failed"), member="x"
    )
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "not_found"
        and "nothing was changed" in errors[0]["message"]
    )
    assert writes(calls) == []


# ------------------------------------------------------------- switchover ----


def sw_stubs(name, pod, before=None):
    return standard(name, {list_argv(pod, name): ok(before or PATRONI[name])})


def test_switchover_dry_run_with_a_healthy_leader_runs_nothing():
    env, errors, calls = run(
        "switchover", "hippo", sw_stubs("hippo", "hippo-instance2-0"), dry=True
    )
    assert env["status"] == "ok" and errors == []
    s = env["outputs"]["switchover"]
    assert (s["applied"], s["wouldRun"], s["mode"]) == (False, True, "switchover")
    assert (
        s["leaderBefore"] == "hippo-instance1-0"
        and s["candidate"] is None
        and s["candidateChosenBy"] is None
    )
    assert s["command"] == {
        "tool": "patronictl",
        "subcommand": "switchover",
        "cluster": "hippo-ha",
        "candidate": None,
    }
    assert s["ranIn"]["pod"] == "hippo-instance2-0"  # not the leader's pod
    assert s["leaderAfter"] is None and s["completed"] is None and s["leaderChanged"] is None
    assert writes(calls) == []
    check_outputs("switchover", env)


def test_switchover_runs_patronictl_switchover_force_then_reads_the_new_leader():
    write = exec_argv(
        "hippo-instance2-0", "hippo", "patronictl", "switchover", "hippo-ha", "--force"
    )
    switched = json.loads(PATRONI["hippo"])
    switched[0].update(Role="Replica", State="streaming", TL=4)
    switched[0]["Lag in MB"] = 0
    switched[1].update(Role="Leader", State="running", TL=4)
    switched[1].pop("Lag in MB")
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[write] = ok("Successfully switched over to hippo-instance2-0\n")

    def hook(argv, tc):
        if argv == write:
            stub_commands(tc, {list_argv("hippo-instance2-0", "hippo"): ok(switched)})

    env, errors, calls = run("switchover", "hippo", stubs, hook=hook)
    assert env["status"] == "ok" and errors == []
    s = env["outputs"]["switchover"]
    assert s["applied"] is True and s["mode"] == "switchover"
    assert (s["leaderBefore"], s["leaderAfter"], s["leaderChanged"]) == (
        "hippo-instance1-0",
        "hippo-instance2-0",
        True,
    )
    assert (s["timelineBefore"], s["timelineAfter"]) == (3, 4)
    assert s["completed"] is True and s["completionReason"] == "leader_changed"
    assert [m["name"] for m in s["membersAfter"]] == ["hippo-instance1-0", "hippo-instance2-0"]
    assert writes(calls) == [write]
    check_outputs("switchover", env)


def test_switchover_with_a_candidate_passes_it_and_completes_only_on_that_leader():
    write = exec_argv(
        "hippo-instance2-0",
        "hippo",
        "patronictl",
        "switchover",
        "hippo-ha",
        "--candidate",
        "hippo-instance2-0",
        "--force",
    )
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[write] = ok("done")
    # patronictl exits 0 but the leader never changes: not completed
    env, _, calls = run("switchover", "hippo", stubs, candidate="hippo-instance2-0", timeout="6s")
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and s["applied"] is True
    assert s["candidate"] == "hippo-instance2-0" and s["candidateChosenBy"] == "input"
    assert (
        s["completed"] is False
        and s["completionReason"] == "timeout"
        and s["leaderChanged"] is False
    )
    assert s["leaderAfter"] == "hippo-instance1-0"
    assert writes(calls) == [write]


def test_switchover_without_a_leader_fails_over_to_the_least_lagging_running_replica():
    pod = "hippo-broken-instance2-0"
    raw = json.loads(PATRONI["hippo-broken"])
    cand = "hippo-broken-instance1-0"
    write = exec_argv(
        pod,
        "hippo-broken",
        "patronictl",
        "failover",
        "hippo-broken-ha",
        "--candidate",
        cand,
        "--force",
    )
    stubs = sw_stubs("hippo-broken", pod)
    stubs[write] = ok("done")
    env, errors, calls = run("switchover", "hippo-broken", stubs, dry=True)
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and errors == []
    assert (s["mode"], s["leaderBefore"], s["candidate"], s["candidateChosenBy"]) == (
        "failover",
        None,
        cand,
        "auto",
    )
    assert s["command"]["subcommand"] == "failover" and s["command"]["candidate"] == cand
    assert raw and writes(calls) == []
    check_outputs("switchover", env)


def test_failover_real_run_completes_when_the_candidate_is_leader():
    pod = "hippo-broken-instance2-0"
    cand = "hippo-broken-instance1-0"
    write = exec_argv(
        pod,
        "hippo-broken",
        "patronictl",
        "failover",
        "hippo-broken-ha",
        "--candidate",
        cand,
        "--force",
    )
    after = json.loads(PATRONI["hippo-broken"])
    after[0].update(Role="Leader", State="running", TL=6)
    stubs = sw_stubs("hippo-broken", pod)
    stubs[write] = ok("done")

    def hook(argv, tc):
        if argv == write:
            stub_commands(tc, {list_argv(pod, "hippo-broken"): ok(after)})

    env, _, _ = run("switchover", "hippo-broken", stubs, hook=hook)
    s = env["outputs"]["switchover"]
    assert s["completed"] is True and s["leaderAfter"] == cand and s["leaderChanged"] is True


@pytest.mark.parametrize("candidate", ["hippo-instance1-0", "ghost", "--force"])
def test_switchover_refuses_a_bad_candidate_and_writes_nothing(candidate):
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[list_argv("hippo-instance1-0", "hippo")] = ok(PATRONI["hippo"])
    env, errors, calls = run("switchover", "hippo", stubs, candidate=candidate)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert writes(calls) == []


def test_switchover_timeout_zero_does_not_wait():
    write = exec_argv(
        "hippo-instance2-0", "hippo", "patronictl", "switchover", "hippo-ha", "--force"
    )
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[write] = ok("done")
    env, _, calls = run("switchover", "hippo", stubs, timeout="0s")
    s = env["outputs"]["switchover"]
    assert s["applied"] is True and s["completed"] is None and s["completionReason"] == "not_waited"
    assert calls.count(list_argv("hippo-instance2-0", "hippo")) == 1  # only the read before


def test_switchover_failure_of_patronictl_fails_the_task():
    write = exec_argv(
        "hippo-instance2-0", "hippo", "patronictl", "switchover", "hippo-ha", "--force"
    )
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[write] = (1, "", "Error: Switchover failed, details: 503, Switchover failed")
    env, errors, _ = run("switchover", "hippo", stubs)
    assert env["status"] == "failed" and "Switchover failed" in errors[0]["message"]


def test_switchover_read_failure_while_waiting_is_reported_not_lost():
    write = exec_argv(
        "hippo-instance2-0", "hippo", "patronictl", "switchover", "hippo-ha", "--force"
    )
    stubs = sw_stubs("hippo", "hippo-instance2-0")
    stubs[write] = ok("done")

    def hook(argv, tc):
        if argv == write:
            stub_commands(
                tc,
                {
                    list_argv("hippo-instance2-0", "hippo"): (
                        1,
                        "",
                        "Unable to connect to the server: i/o timeout",
                    )
                },
            )

    env, errors, _ = run("switchover", "hippo", stubs, hook=hook)
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("unreachable", "wait")
    ]
    s = env["outputs"]["switchover"]
    assert (
        s["applied"] is True and s["completed"] is None and s["completionReason"] == "not_observed"
    )


def test_switchover_bad_timeout_fails_before_any_kubectl_call():
    env, errors, calls = run("switchover", "hippo", {}, timeout="10m")
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


# ---------------------------------------------------------- rolling-restart ----


def pods_with(name, annotation=None, start="2026-11-03T21:00:30Z", ready=True):
    pods = copy.deepcopy(PODS[name])
    for p in pods["items"]:
        if annotation:
            p["metadata"].setdefault("annotations", {})[o.RESTART_ANNOTATION] = annotation
        p["status"]["startTime"] = start
        if not ready:
            for c in p["status"]["conditions"]:
                if c["type"] == "Ready":
                    c["status"] = "False"
    return pods


def test_crunchy_dry_run_sends_the_identical_patch_as_a_server_dry_run():
    stubs = standard(
        "hippo",
        {
            patch_argv("hippo", dry=True): ok(
                "postgrescluster.postgres-operator.crunchydata.com/hippo patched (server dry run)\n"
            )
        },
    )
    env, errors, calls = run("rolling_restart", "hippo", stubs, dry=True)
    assert env["status"] == "ok" and errors == [] and env["dryRun"] is True
    r = env["outputs"]["restart"]
    assert (r["applied"], r["wouldRun"], r["method"]) == (False, True, "annotation")
    assert r["annotation"] == o.RESTART_ANNOTATION and r["requestedAt"] == NOW_ISO
    assert r["membersExpected"] == 2 and r["completed"] is None and r["members"] is None
    assert calls == [get_cr("hippo"), patch_argv("hippo", dry=True)]
    check_outputs("rolling_restart", env)


def test_crunchy_restart_completes_when_every_pod_is_ready_and_restarted():
    stubs = standard(
        "hippo", {patch_argv("hippo"): ok("patched\n")}, pods=pods_with("hippo", NOW_ISO)
    )
    env, errors, calls = run("rolling_restart", "hippo", stubs)
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["restart"]
    assert r["applied"] is True and r["completed"] is True and r["completionReason"] == "restarted"
    assert (r["membersFound"], r["membersReady"], r["membersRestarted"]) == (2, 2, 2)
    assert patch_argv("hippo") in calls
    check_outputs("rolling_restart", env)


def test_crunchy_restart_is_not_complete_merely_because_pods_are_running():
    # Classic: reported "Rolling Restart Completed" whatever happened. Here the operator has not
    # restarted anything yet (old start time, no annotation): all Running but not restarted.
    stubs = standard(
        "hippo",
        {patch_argv("hippo"): ok("patched\n")},
        pods=pods_with("hippo", start="2026-11-03T20:00:00Z"),
    )
    env, _, _ = run("rolling_restart", "hippo", stubs, timeout="6s")
    r = env["outputs"]["restart"]
    assert env["status"] == "ok" and r["applied"] is True
    assert r["completed"] is False and r["completionReason"] == "timeout"
    assert (r["membersReady"], r["membersRestarted"]) == (2, 0)


def test_crunchy_restart_with_a_pod_not_ready_is_not_complete():
    stubs = standard(
        "hippo",
        {patch_argv("hippo"): ok("patched\n")},
        pods=pods_with("hippo", NOW_ISO, ready=False),
    )
    env, _, _ = run("rolling_restart", "hippo", stubs, timeout="3s")
    r = env["outputs"]["restart"]
    assert r["completed"] is False and r["membersReady"] == 0 and r["membersRestarted"] == 2


def test_crunchy_restart_counts_a_replaced_pod_by_its_start_time_too():
    summaries = [
        {
            "ready": True,
            "phase": "Running",
            "restartAnnotation": None,
            "startTime": "2026-11-03T21:00:05Z",
        },
        {
            "ready": True,
            "phase": "Running",
            "restartAnnotation": NOW_ISO,
            "startTime": "2026-11-03T19:00:00Z",
        },
    ]
    assert o.crunchy_restart_done(summaries, 2, NOW_ISO) == (True, 2, 2)
    assert o.crunchy_restart_done(summaries, 3, NOW_ISO)[0] is False  # a pod is missing
    assert o.crunchy_restart_done(summaries, 0, NOW_ISO)[0] is False


def test_restart_timeout_zero_does_not_wait():
    stubs = standard("hippo", {patch_argv("hippo"): ok("patched\n")})
    env, _, calls = run("rolling_restart", "hippo", stubs, timeout="0s")
    r = env["outputs"]["restart"]
    assert r["applied"] is True and r["completed"] is None and r["completionReason"] == "not_waited"
    assert get_pods("hippo") not in calls


def test_crunchy_restart_forbidden_patch_fails_without_changes():
    stubs = standard("hippo", {patch_argv("hippo"): forbidden(PC, "patch")})
    env, errors, _ = run("rolling_restart", "hippo", stubs)
    assert env["status"] == "failed" and errors[0]["code"] == "permission_denied"


def test_crunchy_restart_records_the_previous_annotation():
    cr = copy.deepcopy(CR["hippo"])
    cr["spec"]["metadata"] = {"annotations": {o.RESTART_ANNOTATION: "2026-10-01T00:00:00Z"}}
    stubs = standard(
        "hippo", {patch_argv("hippo", dry=True): ok("patched (server dry run)")}, cr=cr
    )
    env, _, _ = run("rolling_restart", "hippo", stubs, dry=True)
    assert env["outputs"]["restart"]["previousAnnotation"] == "2026-10-01T00:00:00Z"


def test_zalando_dry_run_reads_but_does_not_restart():
    stubs = standard(
        "acid-zalando", {list_argv("acid-zalando-1", "acid-zalando"): ok(PATRONI["acid-zalando"])}
    )
    env, errors, calls = run("rolling_restart", "acid-zalando", stubs, dry=True)
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["restart"]
    assert (r["applied"], r["wouldRun"], r["method"]) == (False, True, "patronictl")
    assert r["command"] == {
        "tool": "patronictl",
        "subcommand": "restart",
        "cluster": "acid-zalando",
        "member": None,
    }
    assert r["membersExpected"] == 2 and r["annotation"] is None
    assert writes(calls) == []
    check_outputs("rolling_restart", env)


def test_zalando_real_restart_runs_patronictl_restart_force_and_checks_members():
    write = exec_argv(
        "acid-zalando-1", "acid-zalando", "patronictl", "restart", "acid-zalando", "--force"
    )
    stubs = standard(
        "acid-zalando",
        {
            list_argv("acid-zalando-1", "acid-zalando"): ok(PATRONI["acid-zalando"]),
            write: ok("Success: restart on instance acid-zalando-0\n"),
        },
    )
    env, errors, calls = run("rolling_restart", "acid-zalando", stubs)
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["restart"]
    assert r["applied"] is True and r["completed"] is True and r["completionReason"] == "running"
    assert (r["membersFound"], r["membersReady"]) == (2, 2)
    assert r["output"].startswith("Success: restart")
    assert writes(calls) == [write]
    check_outputs("rolling_restart", env)


def test_zalando_restart_with_a_member_still_down_is_not_complete():
    write = exec_argv(
        "acid-zalando-1", "acid-zalando", "patronictl", "restart", "acid-zalando", "--force"
    )
    down = json.loads(PATRONI["acid-zalando"])
    down[1]["State"] = "stopped"
    stubs = standard(
        "acid-zalando",
        {
            list_argv("acid-zalando-1", "acid-zalando"): ok(PATRONI["acid-zalando"]),
            write: ok("Success"),
        },
    )

    def hook(argv, tc):
        if argv == write:
            stub_commands(tc, {list_argv("acid-zalando-1", "acid-zalando"): ok(down)})

    env, _, _ = run("rolling_restart", "acid-zalando", stubs, hook=hook, timeout="3s")
    r = env["outputs"]["restart"]
    assert r["completed"] is False and r["completionReason"] == "timeout" and r["membersReady"] == 1


def test_zalando_failed_patronictl_restart_fails_the_task():
    write = exec_argv(
        "acid-zalando-1", "acid-zalando", "patronictl", "restart", "acid-zalando", "--force"
    )
    stubs = standard(
        "acid-zalando",
        {
            list_argv("acid-zalando-1", "acid-zalando"): ok(PATRONI["acid-zalando"]),
            write: (1, "", "Error: restart failed"),
        },
    )
    env, errors, _ = run("rolling_restart", "acid-zalando", stubs)
    assert env["status"] == "failed" and "restart failed" in errors[0]["message"]


# --------------------------------------------- target failures, every task ----

TASK_ARGS = {"reinit_member": {"member": "x"}, "switchover": {}, "rolling_restart": {}}


@pytest.mark.parametrize("task", list(TASK_ARGS))
@pytest.mark.parametrize("name", ["hippo", "acid-zalando"])
def test_missing_target_is_not_found_and_nothing_is_written(task, name):
    err = f'Error from server (NotFound): {PC if is_crunchy(name) else PG} "{name}" not found'
    env, errors, calls = run(task, name, {get_cr(name): (1, "", err)}, **TASK_ARGS[task])
    assert env["status"] == "failed" and errors[0]["code"] == "not_found"
    assert calls == [get_cr(name)]


@pytest.mark.parametrize("task", list(TASK_ARGS))
def test_forbidden_and_unreachable_fail_every_task_without_writing(task):
    env, errors, calls = run(task, "hippo", {get_cr("hippo"): forbidden(PC)}, **TASK_ARGS[task])
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "permission_denied"
        and writes(calls) == []
    )
    err = "Unable to connect to the server: dial tcp 127.0.0.1:6443: connection refused"
    env, errors, _ = run(task, "hippo", {get_cr("hippo"): (1, "", err)}, **TASK_ARGS[task])
    assert env["status"] == "failed" and errors[0]["code"] == "unreachable"


@pytest.mark.parametrize("task", ["reinit_member", "switchover"])
def test_forbidden_exec_fails_the_task_before_any_write(task):
    stubs = standard(
        "hippo",
        {
            list_argv("hippo-instance1-0", "hippo"): forbidden("pods/exec", "create"),
            list_argv("hippo-instance2-0", "hippo"): forbidden("pods/exec", "create"),
        },
    )
    env, errors, calls = run(task, "hippo", stubs, **TASK_ARGS[task])
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "permission_denied"
        and writes(calls) == []
    )


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
        env = run_main(script("rolling_restart"), tc)
        errors = tc.errors
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


# --------------------------------------------------------------- tasks.yaml ----


def test_every_task_is_a_declared_write_with_effects_and_dry_run():
    assert set(TASKS) == {"reinit-member", "switchover", "rolling-restart"}
    for t in TASKS.values():
        assert t["readOnly"] is False and t["dryRun"] is True and t["effects"]
    assert (
        TASKS["reinit-member"]["destructive"] is True and TASKS["switchover"]["destructive"] is True
    )
    assert (
        TASKS["rolling-restart"]["destructive"] is False
        and TASKS["rolling-restart"]["idempotent"] is False
    )


# ------------------------------------------------------------ RBAC / argv ----

RESOURCES = {
    "pods": ("", "pods"),
    PC: ("postgres-operator.crunchydata.com", "postgresclusters"),
    PG: ("acid.zalan.do", "postgresqls"),
}
VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-o",
    "-c",
    "-l",
    "--selector",
    "--field-selector",
    "--context",
    "-p",
    "--type",
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs: get <kind> <name> -> get;
    get/-l -> list; patch -> patch; exec -> pods get + pods/exec create."""
    flags, positional, it = {}, [], iter(list(argv)[1:])
    for a in it:
        if a == "--":
            break
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif a.startswith("--type=") or a.startswith("--dry-run"):
            continue
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named = len(positional) > 2 and "-l" not in flags
        return {(group, plural, "get" if named else "list")}
    if verb == "patch":
        group, plural = RESOURCES[positional[1]]
        return {(group, plural, "patch")}
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


def scenarios():
    reinit_write = exec_argv(
        "hippo-instance1-0",
        "hippo",
        "patronictl",
        "reinit",
        "hippo-ha",
        "hippo-instance2-0",
        "--force",
    )
    sw_write = exec_argv(
        "hippo-instance2-0", "hippo", "patronictl", "switchover", "hippo-ha", "--force"
    )
    zr_write = exec_argv(
        "acid-zalando-1", "acid-zalando", "patronictl", "restart", "acid-zalando", "--force"
    )
    return [
        (
            "reinit_member",
            "hippo",
            {list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]), reinit_write: ok("ok")},
            {"member": "hippo-instance2-0"},
            None,
        ),
        (
            "switchover",
            "hippo",
            {list_argv("hippo-instance2-0", "hippo"): ok(PATRONI["hippo"]), sw_write: ok("ok")},
            {"timeout": "0s"},
            None,
        ),
        (
            "rolling_restart",
            "hippo",
            {patch_argv("hippo"): ok("patched")},
            {},
            pods_with("hippo", NOW_ISO),
        ),
        (
            "rolling_restart",
            "acid-zalando",
            {
                list_argv("acid-zalando-1", "acid-zalando"): ok(PATRONI["acid-zalando"]),
                zr_write: ok("ok"),
            },
            {},
            None,
        ),
    ]


@pytest.mark.parametrize(
    "task,name,extra,inputs,pods", scenarios(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(
    task, name, extra, inputs, pods
):
    env, errors, calls = run(task, name, standard(name, extra, pods=pods), **inputs)
    assert env["status"] == "ok", errors
    needed = set()
    for argv in calls:
        needed |= rbac_needs(argv)
    missing = needed - declared(TASKS[task.replace("_", "-")])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_only_the_crunchy_restart_may_patch():
    for name, t in TASKS.items():
        patches = [(g, r) for g, r, v in declared(t) if v == "patch"]
        assert patches == (
            [("postgres-operator.crunchydata.com", "postgresclusters")]
            if name == "rolling-restart"
            else []
        )


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(patch_argv("hippo")) == {
        ("postgres-operator.crunchydata.com", "postgresclusters", "patch")
    }
    assert rbac_needs(get_pods("hippo")) == {("", "pods", "list")}
    assert rbac_needs(get_cr("acid-zalando")) == {("acid.zalan.do", "postgresqls", "get")}


# ------------------------------------------------------------ no secrets ----


def test_outputs_carry_no_secret_or_env_values_and_bound_the_command_text():
    big = "x" * 5000
    write = exec_argv(
        "hippo-instance1-0",
        "hippo",
        "patronictl",
        "reinit",
        "hippo-ha",
        "hippo-instance2-0",
        "--force",
    )
    stubs = standard(
        "hippo", {list_argv("hippo-instance1-0", "hippo"): ok(PATRONI["hippo"]), write: ok(big)}
    )
    env, _, _ = run("reinit_member", "hippo", stubs, member="hippo-instance2-0")
    r = env["outputs"]["reinit"]
    assert len(r["output"]) == 1024 and r["outputTruncated"] is True
    dump = json.dumps(env["outputs"]).lower()
    assert "password" not in dump and "postgres_host_auth_method" not in dump
