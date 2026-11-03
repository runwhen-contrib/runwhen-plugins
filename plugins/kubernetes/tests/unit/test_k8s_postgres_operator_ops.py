"""Unit tests for k8s-postgres-operator-ops: lib/ and the thin scripts, against JSON captured from
the kind-rw-plugins scenario rw-pgop (synthetic Crunchy/Zalando CRs, member pods, a patronictl
stub) and against realistic `patronictl` stdout (a cluster table followed by Patroni's `Success:` /
`Failed:` lines). Scripts run through runwhen_capability.testing (a real Context, V13 semantics,
stubbed commands); the writes go to `Sim`, a fake patronictl whose members change as writes arrive.
No test reaches a cluster."""

import copy
import importlib.util
import json
import subprocess
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

LEADER = "hippo-instance1-abcd-0"
REPLICA = "hippo-instance1-wxyz-0"
ZLEADER, ZREPLICA = "acid-zalando-0", "acid-zalando-1"


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


# ------------------------------------------- realistic patronictl output ----


def table(raw):
    """What patronictl prints before its result lines: Patroni's pretty table of the members."""
    extra = ["Pending restart"] if any(m.get("Pending restart") for m in raw) else []
    cols = ["Member", "Host", "Role", "State", "TL", "Lag in MB", *extra]
    rows = [[str(m.get(c, "")) for c in cols] for m in raw]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    line = lambda cells: "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    head = f"+ Cluster: {raw[0]['Cluster']} (7301122334455667788) "
    head += "-" * max(3, len(rule) - len(head) - 1) + "+"
    return "\n".join([head, line(cols), rule, *(line(r) for r in rows), rule]) + "\n"


def restart_out(raw, member, good=True, why="restart failed"):
    if good:
        return table(raw) + f"Success: restart on member {member}\n"
    return table(raw) + f"Failed: restart for member {member}, status code=503, ({why})\n"


def reinit_out(raw, member, good=True):
    if good:
        return table(raw) + f"Success: reinitialize for member {member}\n"
    return (
        table(raw)
        + f"Failed: reinitialize for member {member}, status code=503, (Failed to reinitialize: member is not a replica)\n"
    )


def switch_out(raw, mode, candidate):
    verb = "switched over" if mode == "switchover" else "failed over"
    return table(raw) + f'Successfully {verb} to "{candidate}"\n'


class Sim:
    """A fake patronictl for one cluster. `members` is the true state; writes change it, and the
    time a member stays `restarting` / `creating replica` runs on the fake clock. `behaviour`
    maps (action, member) to how a write goes wrong: failed (a Failed line, exit 0), nosuccess
    (no Success line, exit 0), rc1 (non-zero exit), timeout (still running at the limit),
    keep_pending (restarts but Pending restart stays)."""

    def __init__(self, name, clock, members=None):
        self.name, self.clock = name, clock
        self.members = copy.deepcopy(members or json.loads(PATRONI[name]))
        self.behaviour, self.down_for, self.writes, self.exec_timeouts = {}, {}, [], []
        self._until = {}

    def view(self):
        out = copy.deepcopy(self.members)
        for m in out:
            until, state = self._until.get(m["Member"], (None, None))
            if until is not None and self.clock.t < until:
                m["State"] = state
        return out

    def handle(self, pod, args, timeout):
        self.exec_timeouts.append(timeout)
        action = args[0]
        if action == "list":
            return 0, json.dumps(self.view()), ""
        self.writes.append((pod, tuple(args)))
        member = args[2] if action in ("restart", "reinit") else None
        how = self.behaviour.get((action, member))
        raw = self.view()
        if how == "timeout":
            raise subprocess.TimeoutExpired(["kubectl"], timeout)
        if how == "rc1":
            return 1, "", f"Error: {action} for {member} failed: 500 Internal Server Error"
        if how == "failed":
            fail = restart_out if action == "restart" else reinit_out
            if action in ("restart", "reinit"):
                return 0, fail(raw, member, good=False), ""
            return (
                0,
                table(raw) + "Failed: switchover to x, status code=412, (no good candidates)\n",
                "",
            )
        if how == "nosuccess":
            return 0, table(raw), ""
        if action == "restart":
            m = next(m for m in self.members if m["Member"] == member)
            if how != "keep_pending":
                m.pop("Pending restart", None)
            if member in self.down_for:
                self._until[member] = (self.clock.t + self.down_for[member], "restarting")
            return 0, restart_out(raw, member), ""
        if action == "reinit":
            self._until[member] = (self.clock.t + 20, "creating replica")
            return 0, reinit_out(raw, member), ""
        candidate = args[args.index("--candidate") + 1] if "--candidate" in args else None
        old = next((m for m in self.members if m["Role"].lower() == "leader"), None)
        new = next(m for m in self.members if m["Member"] == candidate)
        if old:
            old.update(Role="Replica", State="streaming")
            old["Lag in MB"] = 0
        new.update(Role="Leader", State="running", TL=new["TL"] + 1)
        new.pop("Lag in MB", None)
        return 0, switch_out(raw, action, candidate), ""


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Polling sleeps advance a fake clock instead of waiting."""
    clock = SimpleNamespace(t=0.0)
    clock.monotonic = lambda: clock.t
    clock.sleep = lambda s: setattr(clock, "t", clock.t + s)
    monkeypatch.setattr(fetch, "time", clock)
    return clock


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


def run_sim(task, name, sim, dry=False, extra=None, pods=None, cr=None, **inputs):
    """`run`, with every patronictl command answered by `sim`: (envelope, errors, calls)."""
    with make_context(
        resource=resource(name), settings={"maxItems": 200}, dry_run=dry, now=NOW
    ) as tc:
        calls = stub_commands(tc, standard(name, extra, pods=pods, cr=cr))
        inner = tc.ctx.run

        def run_command(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
            key = tuple(argv)
            if "patronictl" not in key:
                return inner(argv, cwd, timeout, env, inherit_env=inherit_env, check=check)
            calls.append(key)
            pod = key[key.index("exec") + 1]
            rc, out, err = sim.handle(pod, list(key[key.index("patronictl") + 1 :]), timeout)
            return tc.ctx._finish_run(list(argv), rc, out, err, check)

        tc.ctx.run = run_command
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


def writes(calls):
    """The mutating calls among the recorded argv (patronictl write sub-commands, kubectl patch)."""
    out = []
    for c in calls:
        if "patch" in c and "--dry-run=server" not in c:
            out.append(c)
        if "exec" in c and "patronictl" in c and c[c.index("patronictl") + 1] != "list":
            out.append(c)
    return out


# ---------------------------------------------- patronictl output, parsing ----

ZRAW = json.loads(PATRONI["acid-zalando"])
HRAW = json.loads(PATRONI["hippo"])


@pytest.mark.parametrize(
    "stdout",
    [
        restart_out(ZRAW, ZREPLICA),  # Patroni 3.x: a table, then the Success line
        f"Success: restart on member {ZREPLICA}\n",
        f"SUCCESS: RESTART ON MEMBER {ZREPLICA.upper()}\n",  # case-insensitive
        f"Success: restart on node {ZREPLICA}\n",  # tolerated wording
        f"Success: restart on instance '{ZREPLICA}'\n",
        f"Success: restarted for member {ZREPLICA}.\n",
        f"2026-11-03 21:00:00 something unrelated\nSuccess: restart on member {ZREPLICA}\nbye\n",
        f"Success: restart on member acid-zalando-0\nSuccess: restart on member {ZREPLICA}\n",
    ],
)
def test_restart_success_line_for_the_member_is_accepted(stdout):
    assert o.verify_write("restart", stdout, ZREPLICA) == (True, None)


@pytest.mark.parametrize(
    "stdout,why",
    [
        (restart_out(ZRAW, ZREPLICA, good=False), "Failed: restart for member"),
        (restart_out(ZRAW, ZREPLICA, good=False, why="Postgres is stopped"), "Postgres is stopped"),
        (table(ZRAW), "no 'Success: restart"),  # exit 0, a table, nothing else
        ("", "no 'Success: restart"),
        (f"Success: restart on member {ZLEADER}\n", "no 'Success: restart"),  # another member
        (f"Success: restart on member {ZREPLICA}-x\n", "no 'Success: restart"),  # name prefix
        (
            f"Success: restart on member {ZREPLICA}\nFailed: restart for member {ZLEADER}\n",
            "Failed:",
        ),
        ("Failed: restart for node, connection refused\n", "Failed:"),
        ("failed: restart for member x\n", "failed:"),  # case-insensitive
        (f"Restart of {ZREPLICA} was successful\n", "no 'Success: restart"),
    ],
)
def test_restart_without_its_success_line_or_with_a_failed_line_is_not_done(stdout, why):
    done, reason = o.verify_write("restart", stdout, ZREPLICA)
    assert done is False and why in reason


@pytest.mark.parametrize(
    "stdout",
    [
        reinit_out(HRAW, REPLICA),
        f"Success: reinitialize for member {REPLICA}\n",
        f"Success: reinitialise for node {REPLICA}\n",
        f"Success: reinitialized on member {REPLICA}\n",
    ],
)
def test_reinit_success_line_for_the_member_is_accepted(stdout):
    assert o.verify_write("reinit", stdout, REPLICA) == (True, None)


def test_reinit_is_not_done_on_a_restart_success_line_or_a_failed_line():
    assert not o.verify_write("reinit", f"Success: restart on member {REPLICA}\n", REPLICA)[0]
    assert not o.verify_write("reinit", reinit_out(HRAW, REPLICA, good=False), REPLICA)[0]
    assert not o.verify_write("reinit", table(HRAW), REPLICA)[0]


def test_switch_failed_reads_failure_lines_only():
    assert o.switch_failed(switch_out(HRAW, "switchover", REPLICA)) is None
    assert o.switch_failed(switch_out(HRAW, "failover", REPLICA)) is None
    assert "Failed: switchover" in o.switch_failed("Failed: switchover to x, status code=412\n")
    assert "Switchover failed" in o.switch_failed(
        "Switchover failed, details: 503, Switchover failed\n"
    )
    assert o.switch_failed("Failover failed, details: 412\n")


def test_result_lines_drop_the_table_only():
    shown = o.write_result_lines(restart_out(ZRAW, ZREPLICA))
    assert shown == f"Success: restart on member {ZREPLICA}"


# ------------------------------------------------------------- helpers ----


def test_clean_member_accepts_names_and_refuses_anything_an_option_could_mean():
    assert o.clean_member(f" {LEADER} ") == LEADER
    for bad in (None, "", "  ", "--force", "-x", "a b", "a;b", "a$(x)", "../x", "x" * 300):
        with pytest.raises(RwError) as exc:
            o.clean_member(bad)
        assert exc.value.code == "invalid_input"


def test_timeout_bounds():
    assert (
        o.check_timeout("0s") == 0
        and o.check_timeout("2m") == 120
        and o.check_timeout("300s") == 300
        and o.check_timeout("5m") == 300
    )
    for bad in ("301s", "6m", "5x", "", "-1s", "soon"):
        with pytest.raises(RwError) as exc:
            o.check_timeout(bad)
        assert exc.value.code == "invalid_input"


def test_the_time_constants_match_tasks_yaml():
    for t in TASKS.values():
        assert o.parse_duration(t["timeout"]) == o.TASK_TIMEOUT_SECONDS == 420
    assert o.MAX_WAIT_SECONDS == 300
    # a wait at its maximum still fits in the task's time with the margin to spare
    assert o.MAX_WAIT_SECONDS + o.BUDGET_MARGIN_SECONDS < o.TASK_TIMEOUT_SECONDS


def test_patroni_cluster_name_is_read_not_derived():
    assert o.patroni_cluster_name(o.parse_patroni(PATRONI["hippo"])) == "hippo-ha"
    with pytest.raises(RwError):
        o.patroni_cluster_name([{"Member": "a"}])
    for bad in ("", "oops", "{}"):
        with pytest.raises(RwError) as exc:
            o.parse_patroni(bad)
        assert exc.value.code == "internal"


@pytest.mark.parametrize(
    "bad,why",
    [
        ("[]", "empty array"),
        ('[{"Cluster": "c", "Role": "Leader", "State": "running"}]', "no Member"),
        ('[{"Cluster": "c", "Member": "a", "State": "running"}]', "no Role"),
        ('[{"Cluster": "c", "Member": "a", "Role": "Leader"}]', "no State"),
        ('[{"Cluster": "c", "Member": "a", "Role": "Leader", "State": ""}]', "no State"),
        ('[{"Member": "a", "Role": "Leader", "State": "running"}, {"Cluster": "c"}]', "member 1"),
    ],
)
def test_parse_patroni_refuses_an_empty_list_and_members_missing_fields(bad, why):
    with pytest.raises(RwError) as exc:
        o.parse_patroni(bad)
    assert exc.value.code == "internal" and why in exc.value.message


def views(name):
    return o.members(o.parse_patroni(PATRONI[name]))


def with_leader_state(state, role="Leader"):
    raw = json.loads(PATRONI["hippo"])
    raw[0].update(State=state, Role=role)
    return o.members(raw)


def test_plan_switchover_with_a_healthy_leader_and_no_candidate():
    mode, leader, chosen = o.plan_switchover(views("hippo"), None)
    assert (mode, leader["name"], chosen) == ("switchover", LEADER, None)


def test_plan_switchover_with_a_named_candidate():
    mode, leader, chosen = o.plan_switchover(views("hippo"), REPLICA)
    assert (mode, chosen["name"]) == ("switchover", REPLICA)


@pytest.mark.parametrize(
    "candidate,why",
    [(LEADER, "already the leader"), ("nope", "not a member")],
)
def test_plan_switchover_refuses_a_candidate_that_cannot_be_promoted(candidate, why):
    with pytest.raises(RwError) as exc:
        o.plan_switchover(views("hippo"), candidate)
    assert exc.value.code == "invalid_input" and why in exc.value.message


@pytest.mark.parametrize(
    "state", ["restarting", "starting", "stopping", "Restarting", "promoting", "???"]
)
@pytest.mark.parametrize("candidate", [None, REPLICA])
def test_a_leader_in_a_transient_state_is_refused_not_failed_over(state, candidate):
    with pytest.raises(RwError) as exc:
        o.plan_switchover(with_leader_state(state), candidate)
    assert exc.value.code == "invalid_input"
    assert f"leader is {state}; retry later" in exc.value.message
    assert LEADER in exc.value.message and "Nothing was changed" in exc.value.message


@pytest.mark.parametrize(
    "state", ["stopped", "crashed", "start failed", "restart failed", "Stopped"]
)
def test_a_leader_in_a_terminal_state_is_failed_over_to_the_named_candidate(state):
    mode, leader, chosen = o.plan_switchover(with_leader_state(state), REPLICA)
    assert (mode, leader["name"], chosen["name"]) == ("failover", LEADER, REPLICA)


def test_a_failover_with_no_leader_needs_the_named_candidate():
    broken = views("hippo-broken")
    mode, leader, chosen = o.plan_switchover(broken, "hippo-broken-instance1-qrst-0")
    assert (mode, leader, chosen["name"]) == ("failover", None, "hippo-broken-instance1-qrst-0")


@pytest.mark.parametrize("state", ["stopped", "start failed"])
def test_a_failover_without_a_candidate_is_refused_and_chooses_nothing(state):
    with pytest.raises(RwError) as exc:
        o.plan_switchover(with_leader_state(state), None)
    assert exc.value.code == "invalid_input" and "explicit candidate" in exc.value.message
    assert f"{REPLICA} (lag 0 MB)" in exc.value.message  # it lists them, it does not pick
    with pytest.raises(RwError) as exc:
        o.plan_switchover(views("hippo-broken"), None)  # no leader at all
    assert "there is no leader" in exc.value.message and "explicit candidate" in exc.value.message


def test_a_failover_refuses_a_candidate_whose_lag_is_unknown():
    raw = json.loads(PATRONI["hippo-broken"])
    raw[1]["State"] = "running"  # up, but its lag is 'unknown'
    with pytest.raises(RwError) as exc:
        o.plan_switchover(o.members(raw), "hippo-broken-instance1-uvwx-0")
    assert exc.value.code == "invalid_input" and "unknown replication lag" in exc.value.message
    raw[1].pop("Lag in MB")  # Patroni printed nothing: unknown as well
    with pytest.raises(RwError):
        o.plan_switchover(o.members(raw), "hippo-broken-instance1-uvwx-0")


def test_a_failover_refuses_a_candidate_that_is_not_up():
    with pytest.raises(RwError) as exc:
        o.plan_switchover(views("hippo-broken"), "hippo-broken-instance1-uvwx-0")  # start failed
    assert exc.value.code == "invalid_input" and "not running or streaming" in exc.value.message


def test_a_switchover_does_not_need_a_known_lag():
    raw = json.loads(PATRONI["hippo"])
    raw[1]["Lag in MB"] = "unknown"
    assert o.plan_switchover(o.members(raw), REPLICA)[0] == "switchover"


def test_check_reinit_refuses_the_leader_unknown_members_and_no_healthy_leader():
    v = views("hippo")
    assert o.check_reinit(v, REPLICA)["name"] == REPLICA
    with pytest.raises(RwError) as exc:
        o.check_reinit(v, LEADER)
    assert exc.value.code == "invalid_input" and "leader" in exc.value.message
    with pytest.raises(RwError) as exc:
        o.check_reinit(v, "ghost")
    assert LEADER in exc.value.message  # lists the members
    for state in ("restarting", "stopped", "start failed"):
        with pytest.raises(RwError) as exc:
            o.check_reinit(with_leader_state(state), REPLICA)
        assert exc.value.code == "invalid_input"
        assert f"leader {LEADER!r} is {state}" in exc.value.message
        assert "switchover" in exc.value.message and "nothing was changed" in exc.value.message
    with pytest.raises(RwError) as exc:
        o.check_reinit(views("hippo-broken"), "hippo-broken-instance1-qrst-0")
    assert "has no leader" in exc.value.message and "switchover" in exc.value.message


def test_standby_leader_is_a_leader_for_reinit():
    raw = [{"Cluster": "c", "Member": "a", "Role": "Standby Leader", "State": "running"}]
    with pytest.raises(RwError):
        o.check_reinit(o.members(raw), "a")


def test_restart_order_is_replicas_first_by_name_and_the_leader_last():
    raw = [
        {"Member": "c", "Role": "Replica", "State": "streaming"},
        {"Member": "a", "Role": "Leader", "State": "running"},
        {"Member": "b", "Role": "Sync Standby", "State": "streaming"},
    ]
    assert o.restart_order(o.members(raw)) == ["b", "c", "a"]
    raw[1]["Role"] = "Replica"  # no leader: by name
    assert o.restart_order(o.members(raw)) == ["a", "b", "c"]


def test_member_back_needs_up_and_the_pending_restart_cleared():
    raw = [
        {"Member": "a", "Role": "Replica", "State": "streaming", "Pending restart": "*"},
        {"Member": "b", "Role": "Replica", "State": "restarting"},
        {"Member": "c", "Role": "Replica", "State": "streaming"},
        {"Member": "d", "Role": "Leader", "State": "running"},
    ]
    v = o.members(raw)
    assert [o.member_back(v, n) for n in "abcd"] == [False, False, True, True]
    assert o.member_back(v, "zzz") is False


def test_reinit_watch_is_not_fooled_by_a_stale_streaming_reading():
    watch = o.ReinitWatch(REPLICA)
    up, down = views("hippo"), with_leader_state("running")
    down[1]["state"], down[1]["running"] = "creating replica", False
    assert watch(up, 3) is False  # still streaming 3s after the command: could be stale
    assert watch(down, 6) is False and watch.seen_down is True
    assert watch(up, 9) is True  # seen down, now up again
    assert o.ReinitWatch(REPLICA)(up, o.REINIT_SETTLE_SECONDS) is True  # a quick clone: settled
    gone = [m for m in up if m["name"] != REPLICA]
    assert o.ReinitWatch(REPLICA)(gone, 99) is False  # not in the list: not up


def test_exec_pod_prefers_ready_then_not_avoided_then_not_primary():
    pods = PODS["hippo"]["items"]
    assert o.exec_pod(o.CRUNCHY, pods)["metadata"]["name"] == REPLICA  # replica before primary
    assert o.exec_pod(o.CRUNCHY, pods, avoid=(REPLICA,))["metadata"]["name"] == LEADER
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


def test_budget_is_the_task_timeout_less_the_margin_and_honours_the_runners_deadline(fast_clock):
    ctx = SimpleNamespace(deadline_at=None)
    b = fetch.Budget(ctx)
    assert b.remaining() == 390 and b.wait_for(300) == (300, False)
    fast_clock.t = 200
    assert (
        b.remaining() == 190 and b.wait_for(300) == (190, True) and b.wait_for(120) == (120, False)
    )
    assert b.can_start_step() is True
    fast_clock.t = 365
    assert b.can_start_step() is False and b.exec_seconds() == 25
    fast_clock.t = 500
    assert b.wait_for(60) == (0.0, True) and b.exec_seconds() == 1.0
    fast_clock.t = 1000
    sooner = fetch.Budget(SimpleNamespace(deadline_at=1100))  # the runner's own deadline is sooner
    assert sooner.remaining() == 70
    later = fetch.Budget(SimpleNamespace(deadline_at=99999))
    assert later.remaining() == 390


# --------------------------------------------------------- reinit-member ----


def test_reinit_dry_run_checks_everything_and_runs_no_write():
    stubs = standard("hippo", {list_argv(LEADER, "hippo"): ok(PATRONI["hippo"])})
    env, errors, calls = run("reinit_member", "hippo", stubs, dry=True, member=REPLICA)
    assert env["status"] == "ok" and errors == [] and env["dryRun"] is True
    r = env["outputs"]["reinit"]
    assert (r["applied"], r["wouldRun"]) == (False, True)
    assert r["command"] == {
        "tool": "patronictl",
        "subcommand": "reinit",
        "cluster": "hippo-ha",
        "member": REPLICA,
    }
    assert r["stateBefore"] == "streaming" and r["role"] == "Replica" and r["leader"] == LEADER
    assert r["output"] is None and r["stateAfter"] is None
    assert (r["completed"], r["completionReason"]) == (None, None)
    assert env["outputs"]["target"]["patroniCluster"] == "hippo-ha"
    assert writes(calls) == []
    # the pod used is not the member being wiped
    assert r["ranIn"] == {"pod": LEADER, "container": "database"}
    check_outputs("reinit_member", env)


def test_reinit_is_applied_after_the_success_line_and_completed_when_the_member_streams_again(
    fast_clock,
):
    sim = Sim("hippo", fast_clock)
    env, errors, calls = run_sim("reinit_member", "hippo", sim, member=REPLICA)
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["reinit"]
    assert r["applied"] is True and r["wouldRun"] is False
    assert r["output"] == f"Success: reinitialize for member {REPLICA}"  # no table
    assert sim.writes == [(LEADER, ("reinit", "hippo-ha", REPLICA, "--force"))]
    # the member was seen `creating replica` for 20s, then streaming: only then is it completed
    assert r["completed"] is True and r["completionReason"] == "running"
    assert r["stateBefore"] == "streaming" and r["stateAfter"] == "streaming"
    assert r["waitedSeconds"] >= 20
    check_outputs("reinit_member", env)


def test_reinit_is_not_completed_while_the_member_is_still_cloning(fast_clock):
    sim = Sim("hippo", fast_clock)
    env, errors, _ = run_sim("reinit_member", "hippo", sim, member=REPLICA, timeout="9s")
    r = env["outputs"]["reinit"]
    assert env["status"] == "ok" and errors == [] and r["applied"] is True
    assert r["completed"] is False and r["completionReason"] == "timeout"
    assert r["stateAfter"] == "creating replica"


def test_reinit_timeout_zero_applies_but_does_not_wait(fast_clock):
    sim = Sim("hippo", fast_clock)
    env, _, calls = run_sim("reinit_member", "hippo", sim, member=REPLICA, timeout="0s")
    r = env["outputs"]["reinit"]
    assert r["applied"] is True and r["completed"] is None and r["completionReason"] == "not_waited"
    assert calls.count(list_argv(LEADER, "hippo")) == 1  # only the read before


@pytest.mark.parametrize("how", ["failed", "nosuccess"])
def test_reinit_with_a_failed_line_or_no_success_line_is_an_error_even_when_patronictl_exits_0(
    fast_clock, how
):
    sim = Sim("hippo", fast_clock)
    sim.behaviour[("reinit", REPLICA)] = how
    env, errors, calls = run_sim("reinit_member", "hippo", sim, member=REPLICA)
    r = env["outputs"]["reinit"]
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("internal", "reinit")]
    assert "exited 0 but did not confirm" in errors[0]["message"]
    assert r["applied"] is False and r["completed"] is False and r["completionReason"] == "failed"
    assert r["stateAfter"] is None and r["waitedSeconds"] is None
    assert calls.count(list_argv(LEADER, "hippo")) == 1  # nothing was waited for
    check_outputs("reinit_member", env)


def test_reinit_failed_line_text_reaches_the_error_and_the_output():
    # Classic: a failed reinit was reported as "Members Were Reinitialized".
    stdout = reinit_out(HRAW, REPLICA, good=False)
    write = exec_argv(LEADER, "hippo", "patronictl", "reinit", "hippo-ha", REPLICA, "--force")
    stubs = standard("hippo", {list_argv(LEADER, "hippo"): ok(PATRONI["hippo"]), write: ok(stdout)})
    env, errors, _ = run("reinit_member", "hippo", stubs, member=REPLICA)
    assert "member is not a replica" in errors[0]["message"]
    r = env["outputs"]["reinit"]
    assert r["output"].startswith("Failed: reinitialize for member") and r["applied"] is False


def test_reinit_failed_line_on_stderr_is_read_too():
    write = exec_argv(LEADER, "hippo", "patronictl", "reinit", "hippo-ha", REPLICA, "--force")
    stubs = standard(
        "hippo",
        {
            list_argv(LEADER, "hippo"): ok(PATRONI["hippo"]),
            write: (
                0,
                table(HRAW),
                f"Failed: reinitialize for member {REPLICA}, status code=503\n",
            ),
        },
    )
    env, errors, _ = run("reinit_member", "hippo", stubs, member=REPLICA)
    assert env["outputs"]["reinit"]["applied"] is False and errors[0]["code"] == "internal"


def test_reinit_when_patronictl_exits_non_zero_the_task_fails_with_its_message(fast_clock):
    sim = Sim("hippo", fast_clock)
    sim.behaviour[("reinit", REPLICA)] = "rc1"
    env, errors, _ = run_sim("reinit_member", "hippo", sim, member=REPLICA)
    assert env["status"] == "failed" and errors[0]["code"] == "internal"
    assert "500 Internal Server Error" in errors[0]["message"]


def test_reinit_still_running_at_the_limit_is_unconfirmed_not_applied(fast_clock):
    sim = Sim("hippo", fast_clock)
    sim.behaviour[("reinit", REPLICA)] = "timeout"
    env, errors, _ = run_sim("reinit_member", "hippo", sim, member=REPLICA)
    r = env["outputs"]["reinit"]
    assert env["status"] == "partial" and [e["code"] for e in errors] == ["timeout"]
    assert (r["applied"], r["completed"], r["completionReason"]) == (False, False, "unconfirmed")


def test_reinit_wait_is_capped_by_the_time_left(fast_clock):
    sim = Sim("hippo", fast_clock)
    # the write itself takes 340s of the 390s budget: 50s of wait is left
    real_handle = sim.handle

    def slow(pod, args, timeout):
        result = real_handle(pod, args, timeout)
        if args[0] == "reinit":
            fast_clock.t += 340
            sim._until[REPLICA] = (
                fast_clock.t + 500,
                "creating replica",
            )  # clone outlasts the task
        return result

    sim.handle = slow
    env, errors, _ = run_sim("reinit_member", "hippo", sim, member=REPLICA, timeout="300s")
    r = env["outputs"]["reinit"]
    assert errors == [] and r["applied"] is True
    assert r["completed"] is False and r["completionReason"] == "budget"
    assert r["waitedSeconds"] <= 50 and fast_clock.t <= 390 + 1


def test_reinit_write_exec_limit_is_cut_to_the_time_left(fast_clock):
    sim = Sim("hippo", fast_clock)
    run_sim("reinit_member", "hippo", sim, member=REPLICA, timeout="0s")
    assert sim.exec_timeouts[-1] == 120  # a write may run 120s, less when less is left


@pytest.mark.parametrize("state", ["restarting", "stopped"])
def test_reinit_with_the_leader_not_running_is_refused_and_writes_nothing(fast_clock, state):
    sim = Sim("hippo", fast_clock)
    sim.members[0]["State"] = state
    env, errors, calls = run_sim("reinit_member", "hippo", sim, member=REPLICA)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert f"leader {LEADER!r} is {state}" in errors[0]["message"] and writes(calls) == []


def test_reinit_with_no_leader_is_refused_and_points_at_failover(fast_clock):
    sim = Sim("hippo-broken", fast_clock)
    env, errors, calls = run_sim(
        "reinit_member", "hippo-broken", sim, member="hippo-broken-instance1-qrst-0"
    )
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert "has no leader" in errors[0]["message"] and "switchover" in errors[0]["message"]
    assert writes(calls) == []


def test_reinit_refuses_the_leader_before_any_write():
    stubs = standard(
        "hippo",
        {
            list_argv(REPLICA, "hippo"): ok(PATRONI["hippo"]),
            list_argv(LEADER, "hippo"): ok(PATRONI["hippo"]),
        },
    )
    env, errors, calls = run("reinit_member", "hippo", stubs, member=LEADER)
    assert (
        env["status"] == "failed"
        and errors[0]["code"] == "invalid_input"
        and "leader" in errors[0]["message"]
    )
    assert writes(calls) == []


def test_reinit_of_a_name_that_is_not_a_member_lists_the_members():
    stubs = standard("hippo", {list_argv(REPLICA, "hippo"): ok(PATRONI["hippo"])})
    env, errors, calls = run("reinit_member", "hippo", stubs, member="ghost")
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert REPLICA in errors[0]["message"] and writes(calls) == []


@pytest.mark.parametrize("bad", ["", "--force", "a b"])
def test_reinit_bad_member_input_fails_before_any_kubectl_call(bad):
    env, errors, calls = run("reinit_member", "hippo", {}, member=bad)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_reinit_read_back_failure_is_reported_but_keeps_the_result():
    write = exec_argv(LEADER, "hippo", "patronictl", "reinit", "hippo-ha", REPLICA, "--force")
    stubs = standard(
        "hippo",
        {
            list_argv(LEADER, "hippo"): ok(PATRONI["hippo"]),
            write: ok(reinit_out(HRAW, REPLICA)),
        },
    )

    def hook(argv, tc):
        if argv == write:
            stub_commands(
                tc,
                {
                    list_argv(LEADER, "hippo"): (
                        1,
                        "",
                        "Unable to connect to the server: connection refused",
                    )
                },
            )

    env, errors, _ = run("reinit_member", "hippo", stubs, hook=hook, member=REPLICA)
    r = env["outputs"]["reinit"]
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("unreachable", "wait")
    ]
    assert r["applied"] is True and r["stateAfter"] is None
    assert r["completed"] is None and r["completionReason"] == "not_observed"


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


def test_reinit_of_an_empty_member_list_is_an_internal_error_and_writes_nothing():
    stubs = standard(
        "hippo", {list_argv(LEADER, "hippo"): ok("[]"), list_argv(REPLICA, "hippo"): ok("[]")}
    )
    env, errors, calls = run("reinit_member", "hippo", stubs, member=REPLICA)
    assert env["status"] == "failed" and errors[0]["code"] == "internal"
    assert "empty array" in errors[0]["message"] and writes(calls) == []


# ------------------------------------------------------------- switchover ----


def sw_stubs(name, pod, before=None):
    return standard(name, {list_argv(pod, name): ok(before or PATRONI[name])})


def test_switchover_dry_run_with_a_healthy_leader_runs_nothing():
    env, errors, calls = run("switchover", "hippo", sw_stubs("hippo", REPLICA), dry=True)
    assert env["status"] == "ok" and errors == []
    s = env["outputs"]["switchover"]
    assert (s["applied"], s["wouldRun"], s["mode"]) == (False, True, "switchover")
    assert s["leaderBefore"] == LEADER and s["candidate"] is None
    assert s["command"] == {
        "tool": "patronictl",
        "subcommand": "switchover",
        "cluster": "hippo-ha",
        "candidate": None,
    }
    assert s["ranIn"]["pod"] == REPLICA  # not the leader's pod
    assert s["leaderAfter"] is None and s["completed"] is None and s["leaderChanged"] is None
    assert writes(calls) == []
    check_outputs("switchover", env)


def test_switchover_runs_patronictl_switchover_force_then_reads_the_new_leader(fast_clock):
    sim = Sim("hippo", fast_clock)
    # patronictl picks the replica: the sim promotes the candidate it is given, so name it
    sim_write = sim.handle

    def pick(pod, args, timeout):
        if args[0] == "switchover":
            args = [*args[:-1], "--candidate", REPLICA, args[-1]]
        return sim_write(pod, args, timeout)

    sim.handle = pick
    env, errors, calls = run_sim("switchover", "hippo", sim)
    assert env["status"] == "ok" and errors == []
    s = env["outputs"]["switchover"]
    assert s["applied"] is True and s["mode"] == "switchover"
    assert s["output"] == f'Successfully switched over to "{REPLICA}"'
    assert (s["leaderBefore"], s["leaderAfter"], s["leaderChanged"]) == (LEADER, REPLICA, True)
    assert (s["timelineBefore"], s["timelineAfter"]) == (3, 4)
    assert s["completed"] is True and s["completionReason"] == "leader_changed"
    assert [m["name"] for m in s["membersAfter"]] == [LEADER, REPLICA]
    assert [w[1][0] for w in sim.writes] == ["switchover"]
    assert "--candidate" not in writes(calls)[0]
    check_outputs("switchover", env)


def test_switchover_with_a_candidate_passes_it_and_completes_only_on_that_leader(fast_clock):
    sim = Sim("hippo", fast_clock)
    env, errors, calls = run_sim("switchover", "hippo", sim, candidate=REPLICA)
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and errors == [] and s["mode"] == "switchover"
    assert sim.writes == [(REPLICA, ("switchover", "hippo-ha", "--candidate", REPLICA, "--force"))]
    assert s["candidate"] == REPLICA and s["completed"] is True and s["leaderAfter"] == REPLICA
    assert "candidateChosenBy" not in s


def test_switchover_that_exits_0_but_never_changes_the_leader_is_not_completed():
    write = exec_argv(
        REPLICA, "hippo", "patronictl", "switchover", "hippo-ha", "--candidate", REPLICA, "--force"
    )
    stubs = sw_stubs("hippo", REPLICA)
    stubs[write] = ok(switch_out(HRAW, "switchover", REPLICA))
    env, _, calls = run("switchover", "hippo", stubs, candidate=REPLICA, timeout="6s")
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and s["applied"] is True
    assert (
        s["completed"] is False
        and s["completionReason"] == "timeout"
        and s["leaderChanged"] is False
    )
    assert s["leaderAfter"] == LEADER
    assert writes(calls) == [write]


@pytest.mark.parametrize("state", ["stopped", "crashed", "start failed", "restart failed"])
def test_switchover_with_a_terminal_leader_fails_over_to_the_named_candidate(fast_clock, state):
    sim = Sim("hippo", fast_clock)
    sim.members[0]["State"] = state
    env, errors, calls = run_sim("switchover", "hippo", sim, candidate=REPLICA)
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and errors == []
    assert (s["mode"], s["leaderBeforeState"], s["candidate"]) == ("failover", state, REPLICA)
    assert sim.writes == [(REPLICA, ("failover", "hippo-ha", "--candidate", REPLICA, "--force"))]
    assert s["completed"] is True and s["leaderAfter"] == REPLICA
    assert s["output"] == f'Successfully failed over to "{REPLICA}"'


def test_failover_dry_run_with_no_leader_and_a_named_candidate_runs_nothing():
    pod = "hippo-broken-instance1-uvwx-0"
    cand = "hippo-broken-instance1-qrst-0"
    env, errors, calls = run(
        "switchover", "hippo-broken", sw_stubs("hippo-broken", pod), dry=True, candidate=cand
    )
    s = env["outputs"]["switchover"]
    assert env["status"] == "ok" and errors == []
    assert (s["mode"], s["leaderBefore"], s["candidate"]) == ("failover", None, cand)
    assert s["command"]["subcommand"] == "failover" and s["command"]["candidate"] == cand
    assert writes(calls) == []
    check_outputs("switchover", env)


def test_failover_without_a_candidate_is_refused_and_writes_nothing():
    pod = "hippo-broken-instance1-uvwx-0"
    env, errors, calls = run("switchover", "hippo-broken", sw_stubs("hippo-broken", pod))
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert "explicit candidate" in errors[0]["message"] and writes(calls) == []


@pytest.mark.parametrize("state", ["restarting", "starting", "stopping"])
def test_switchover_with_a_transient_leader_is_refused_and_writes_nothing(fast_clock, state):
    sim = Sim("hippo", fast_clock)
    sim.members[0]["State"] = state
    for candidate in (None, REPLICA):
        inputs = {"candidate": candidate} if candidate else {}
        env, errors, calls = run_sim("switchover", "hippo", sim, **inputs)
        assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
        assert f"leader is {state}; retry later" in errors[0]["message"] and writes(calls) == []


def test_failover_refuses_a_candidate_with_unknown_lag_and_writes_nothing():
    raw = json.loads(PATRONI["hippo-broken"])
    raw[1]["State"] = "running"
    stubs = sw_stubs("hippo-broken", "hippo-broken-instance1-uvwx-0", before=json.dumps(raw))
    stubs[list_argv("hippo-broken-instance1-qrst-0", "hippo-broken")] = ok(json.dumps(raw))
    env, errors, calls = run(
        "switchover", "hippo-broken", stubs, candidate="hippo-broken-instance1-uvwx-0"
    )
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert "unknown replication lag" in errors[0]["message"] and writes(calls) == []


@pytest.mark.parametrize("candidate", [LEADER, "ghost", "--force"])
def test_switchover_refuses_a_bad_candidate_and_writes_nothing(candidate):
    stubs = sw_stubs("hippo", REPLICA)
    stubs[list_argv(LEADER, "hippo")] = ok(PATRONI["hippo"])
    env, errors, calls = run("switchover", "hippo", stubs, candidate=candidate)
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    assert writes(calls) == []


def test_switchover_timeout_zero_does_not_wait(fast_clock):
    sim = Sim("hippo", fast_clock)
    env, _, calls = run_sim("switchover", "hippo", sim, candidate=REPLICA, timeout="0s")
    s = env["outputs"]["switchover"]
    assert s["applied"] is True and s["completed"] is None and s["completionReason"] == "not_waited"
    assert calls.count(list_argv(REPLICA, "hippo")) == 1  # only the read before


def test_switchover_with_a_failed_line_and_exit_0_is_an_error_and_not_completed(fast_clock):
    sim = Sim("hippo", fast_clock)
    sim.behaviour[("switchover", None)] = "failed"
    env, errors, calls = run_sim("switchover", "hippo", sim, candidate=REPLICA)
    s = env["outputs"]["switchover"]
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("internal", "switchover")
    ]
    assert s["applied"] is False and s["completed"] is False and s["completionReason"] == "failed"
    assert s["leaderAfter"] is None and s["output"].startswith("Failed:")
    check_outputs("switchover", env)


def test_switchover_failure_of_patronictl_fails_the_task():
    write = exec_argv(REPLICA, "hippo", "patronictl", "switchover", "hippo-ha", "--force")
    stubs = sw_stubs("hippo", REPLICA)
    stubs[write] = (1, "", "Error: Switchover failed, details: 503, Switchover failed")
    env, errors, _ = run("switchover", "hippo", stubs)
    assert env["status"] == "failed" and "Switchover failed" in errors[0]["message"]


def test_switchover_wait_is_capped_by_the_time_left(fast_clock):
    write = exec_argv(REPLICA, "hippo", "patronictl", "switchover", "hippo-ha", "--force")
    stubs = sw_stubs("hippo", REPLICA)
    stubs[write] = ok(switch_out(HRAW, "switchover", REPLICA))

    def hook(argv, tc):
        if argv == write:
            fast_clock.t += 350  # the write itself took 350s of the 390s

    env, _, _ = run("switchover", "hippo", stubs, hook=hook, timeout="300s")
    s = env["outputs"]["switchover"]
    assert s["completed"] is False and s["completionReason"] == "budget"
    assert s["waitedSeconds"] <= 40 and fast_clock.t <= 391


def test_switchover_read_failure_while_waiting_is_reported_not_lost():
    write = exec_argv(REPLICA, "hippo", "patronictl", "switchover", "hippo-ha", "--force")
    stubs = sw_stubs("hippo", REPLICA)
    stubs[write] = ok(switch_out(HRAW, "switchover", REPLICA))

    def hook(argv, tc):
        if argv == write:
            stub_commands(
                tc,
                {
                    list_argv(REPLICA, "hippo"): (
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


def test_crunchy_restart_annotation_is_the_documented_bare_restarted_key():
    assert o.RESTART_ANNOTATION == "restarted"
    assert o.restart_patch(NOW_ISO) == {
        "spec": {"metadata": {"annotations": {"restarted": NOW_ISO}}}
    }


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
    assert r["annotation"] == "restarted" and r["requestedAt"] == NOW_ISO
    assert r["membersExpected"] == 2 and r["completed"] is None and r["members"] is None
    assert r["memberResults"] is None
    assert calls == [get_cr("hippo"), patch_argv("hippo", dry=True)]
    assert '"annotations": {"restarted": "2026-11-03T21:00:00Z"}' in calls[1][-2]
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


def test_crunchy_restart_wait_is_capped_by_the_time_left(fast_clock):
    stubs = standard(
        "hippo",
        {patch_argv("hippo"): ok("patched\n")},
        pods=pods_with("hippo", start="2026-11-03T20:00:00Z"),
    )
    env, _, _ = run("rolling_restart", "hippo", stubs, hook=lambda a, tc: None, timeout="300s")
    r = env["outputs"]["restart"]
    assert r["completed"] is False and r["completionReason"] == "timeout"  # 300s fits: not capped
    fast_clock.t = 0

    def hook(argv, tc):
        if argv == patch_argv("hippo"):
            fast_clock.t += 330  # 60s of budget left

    env, _, _ = run("rolling_restart", "hippo", stubs, hook=hook, timeout="300s")
    r = env["outputs"]["restart"]
    assert r["completionReason"] == "budget" and r["waitedSeconds"] <= 60


def test_restart_timeout_zero_does_not_wait_for_crunchy():
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


def test_the_annotation_values_that_are_echoed_are_bounded():
    big = "x" * 5000
    cr = copy.deepcopy(CR["hippo"])
    cr["spec"]["metadata"] = {"annotations": {o.RESTART_ANNOTATION: big}}
    pods = pods_with("hippo", big)
    stubs = standard("hippo", {patch_argv("hippo"): ok("patched\n")}, pods=pods, cr=cr)
    env, errors, _ = run("rolling_restart", "hippo", stubs, timeout="3s")
    r = env["outputs"]["restart"]
    assert errors == [] and len(r["previousAnnotation"]) == 1024
    assert all(len(m["restartAnnotation"]) == 1024 for m in r["members"])
    check_outputs("rolling_restart", env)  # the schema says maxLength 1024 for both
    schema = json.loads((SKILL / "schemas" / "restart.v1.json").read_text())["properties"]
    assert schema["previousAnnotation"]["maxLength"] == 1024
    items = schema["members"]["oneOf"][1]["items"]["properties"]
    assert items["restartAnnotation"]["maxLength"] == 1024
    over = copy.deepcopy(r)
    over["previousAnnotation"] = big
    assert validate("restart.v1.json", over)  # an unbounded value would not validate


def test_zalando_dry_run_reads_and_plans_the_order_but_does_not_restart():
    stubs = standard(
        "acid-zalando", {list_argv(ZREPLICA, "acid-zalando"): ok(PATRONI["acid-zalando"])}
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
    assert [(m["order"], m["member"], m["status"]) for m in r["memberResults"]] == [
        (1, ZREPLICA, "planned"),
        (2, ZLEADER, "planned"),
    ]
    assert writes(calls) == []
    check_outputs("rolling_restart", env)


def test_zalando_restart_goes_member_by_member_replicas_first_and_the_leader_last(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.down_for = {ZREPLICA: 8, ZLEADER: 8}
    env, errors, calls = run_sim("rolling_restart", "acid-zalando", sim)
    assert env["status"] == "ok" and errors == []
    r = env["outputs"]["restart"]
    assert sim.writes == [
        (ZREPLICA, ("restart", "acid-zalando", ZREPLICA, "--force")),
        (ZREPLICA, ("restart", "acid-zalando", ZLEADER, "--force")),
    ]
    assert r["applied"] is True and r["completed"] is True and r["completionReason"] == "running"
    assert (r["membersExpected"], r["membersFound"], r["membersReady"], r["membersRestarted"]) == (
        2,
        2,
        2,
        2,
    )
    first, last = r["memberResults"]
    assert (first["member"], first["role"], first["status"]) == (ZREPLICA, "Replica", "restarted")
    assert (last["member"], last["role"], last["status"]) == (ZLEADER, "Leader", "restarted")
    assert first["applied"] and first["pendingRestartBefore"] is True
    assert first["pendingRestartAfter"] is False and first["stateAfter"] == "streaming"
    assert first["output"] == f"Success: restart on member {ZREPLICA}"
    assert first["waitedSeconds"] >= 8 and last["waitedSeconds"] >= 8
    assert all(not m["pendingRestart"] for m in r["members"])
    assert r["method"] == "patronictl"
    check_outputs("rolling_restart", env)


def test_zalando_the_next_member_waits_for_the_previous_one_to_be_back(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.down_for = {ZREPLICA: 30, ZLEADER: 5}
    real = sim.handle
    seen = []

    def spy(pod, args, timeout):
        if args[0] == "restart" and args[2] == ZLEADER:
            seen.append(fast_clock.t)  # the leader is restarted only after the replica is back
        return real(pod, args, timeout)

    sim.handle = spy
    env, _, _ = run_sim("rolling_restart", "acid-zalando", sim)
    assert env["outputs"]["restart"]["completed"] is True
    assert seen and seen[0] >= 30


def test_zalando_a_member_that_does_not_come_back_stops_the_loop_and_the_leader_is_not_touched(
    fast_clock,
):
    sim = Sim("acid-zalando", fast_clock)
    sim.down_for = {ZREPLICA: 10_000}
    env, errors, calls = run_sim("rolling_restart", "acid-zalando", sim, timeout="20s")
    r = env["outputs"]["restart"]
    assert env["status"] == "ok" and errors == []
    assert [w[1][2] for w in sim.writes] == [ZREPLICA]  # the leader was never restarted
    assert r["applied"] is True and r["completed"] is False and r["completionReason"] == "timeout"
    assert [m["status"] for m in r["memberResults"]] == ["not_back", "not_attempted"]
    assert r["memberResults"][0]["stateAfter"] == "restarting"
    assert "not restarted" in r["memberResults"][1]["detail"]
    assert r["membersRestarted"] == 0 and r["membersReady"] == 1
    check_outputs("rolling_restart", env)


def test_zalando_a_member_whose_pending_restart_stays_is_not_back(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.behaviour[("restart", ZREPLICA)] = "keep_pending"
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim, timeout="9s")
    r = env["outputs"]["restart"]
    assert r["completed"] is False and r["memberResults"][0]["status"] == "not_back"
    assert r["memberResults"][0]["pendingRestartAfter"] is True
    assert r["memberResults"][1]["status"] == "not_attempted"


@pytest.mark.parametrize("how", ["failed", "nosuccess"])
def test_zalando_restart_with_a_failed_line_or_no_success_line_is_an_error_even_when_patronictl_exits_0(
    fast_clock, how
):
    sim = Sim("acid-zalando", fast_clock)
    sim.behaviour[("restart", ZREPLICA)] = how
    env, errors, calls = run_sim("rolling_restart", "acid-zalando", sim)
    r = env["outputs"]["restart"]
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("internal", f"restart:{ZREPLICA}")]
    assert "exited 0 but did not confirm" in errors[0]["message"]
    assert r["applied"] is False and r["completed"] is False
    assert r["completionReason"] == "member_failed" and r["membersRestarted"] == 0
    assert [m["status"] for m in r["memberResults"]] == ["failed", "not_attempted"]
    assert [w[1][2] for w in sim.writes] == [ZREPLICA]  # the leader was not restarted
    check_outputs("rolling_restart", env)


def test_zalando_failed_line_text_is_in_the_member_result():
    write = exec_argv(
        ZREPLICA, "acid-zalando", "patronictl", "restart", "acid-zalando", ZREPLICA, "--force"
    )
    stubs = standard(
        "acid-zalando",
        {
            list_argv(ZREPLICA, "acid-zalando"): ok(PATRONI["acid-zalando"]),
            write: ok(restart_out(ZRAW, ZREPLICA, good=False, why="Postgres is stopped")),
        },
    )
    env, errors, _ = run("rolling_restart", "acid-zalando", stubs)
    first = env["outputs"]["restart"]["memberResults"][0]
    assert first["status"] == "failed" and "Postgres is stopped" in first["detail"]
    assert first["output"].startswith("Failed: restart for member")


def test_zalando_second_member_failing_keeps_the_first_members_result(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.behaviour[("restart", ZLEADER)] = "failed"
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim)
    r = env["outputs"]["restart"]
    assert env["status"] == "partial" and r["applied"] is True and r["completed"] is False
    assert [m["status"] for m in r["memberResults"]] == ["restarted", "failed"]
    assert r["membersRestarted"] == 1


def test_zalando_patronictl_exit_non_zero_is_a_member_failure_not_a_lost_result(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.behaviour[("restart", ZLEADER)] = "rc1"
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim)
    r = env["outputs"]["restart"]
    assert env["status"] == "partial" and errors[0]["code"] == "internal"
    assert (
        errors[0]["scope"] == f"restart:{ZLEADER}"
        and "500 Internal Server Error" in errors[0]["message"]
    )
    assert [m["status"] for m in r["memberResults"]] == ["restarted", "failed"]


def test_zalando_a_write_still_running_at_the_limit_is_unconfirmed_and_ends_the_loop(fast_clock):
    sim = Sim("acid-zalando", fast_clock)
    sim.behaviour[("restart", ZREPLICA)] = "timeout"
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim)
    r = env["outputs"]["restart"]
    assert env["status"] == "partial" and [e["code"] for e in errors] == ["timeout"]
    assert [m["status"] for m in r["memberResults"]] == ["unconfirmed", "not_attempted"]
    assert r["applied"] is False and r["completed"] is False


def test_zalando_budget_caps_the_wait_of_a_late_member(fast_clock):
    # three members that each take 150s to come back: the third has 90s left, not the 300s asked
    raw = json.loads(PATRONI["acid-zalando"])
    raw.insert(1, {**raw[1], "Member": "acid-zalando-2", "Host": "10.244.0.56"})
    sim = Sim("acid-zalando", fast_clock, members=raw)
    sim.down_for = {m["Member"]: 150 for m in raw}
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim, timeout="300s")
    r = env["outputs"]["restart"]
    assert errors == [] and r["completed"] is False and r["completionReason"] == "budget"
    statuses = [m["status"] for m in r["memberResults"]]
    assert statuses == ["restarted", "restarted", "not_back"]
    assert [m["member"] for m in r["memberResults"]][-1] == ZLEADER
    assert fast_clock.t <= 390 + 1  # never past the task's time less the margin


def test_zalando_no_time_left_for_another_member_is_not_attempted_not_started_blind(fast_clock):
    raw = json.loads(PATRONI["acid-zalando"])
    raw.insert(1, {**raw[1], "Member": "acid-zalando-2", "Host": "10.244.0.56"})
    sim = Sim("acid-zalando", fast_clock, members=raw)
    sim.down_for = {m["Member"]: 190 for m in raw}
    env, errors, _ = run_sim("rolling_restart", "acid-zalando", sim, timeout="300s")
    r = env["outputs"]["restart"]
    statuses = [m["status"] for m in r["memberResults"]]
    assert statuses == ["restarted", "restarted", "not_attempted"] and errors == []
    assert r["completed"] is False and r["completionReason"] == "budget"
    assert len(sim.writes) == 2 and "time was left" in r["memberResults"][2]["detail"]


def test_zalando_restart_needs_a_wait_of_at_least_a_second():
    env, errors, calls = run("rolling_restart", "acid-zalando", {}, timeout="0s")
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input" and calls == []


def test_zalando_read_failure_while_waiting_is_reported_and_stops_the_loop():
    write = exec_argv(
        ZREPLICA, "acid-zalando", "patronictl", "restart", "acid-zalando", ZREPLICA, "--force"
    )
    stubs = standard(
        "acid-zalando",
        {
            list_argv(ZREPLICA, "acid-zalando"): ok(PATRONI["acid-zalando"]),
            write: ok(restart_out(ZRAW, ZREPLICA)),
        },
    )

    def hook(argv, tc):
        if argv == write:
            stub_commands(
                tc,
                {
                    list_argv(ZREPLICA, "acid-zalando"): (
                        1,
                        "",
                        "Unable to connect to the server: connection refused",
                    )
                },
            )

    env, errors, calls = run("rolling_restart", "acid-zalando", stubs, hook=hook)
    r = env["outputs"]["restart"]
    assert env["status"] == "partial" and [(e["code"], e["scope"]) for e in errors] == [
        ("unreachable", "wait")
    ]
    assert (
        r["applied"] is True and r["completed"] is None and r["completionReason"] == "not_observed"
    )
    assert [m["status"] for m in r["memberResults"]] == ["not_back", "not_attempted"]
    assert writes(calls) == [write]


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
            list_argv(LEADER, "hippo"): forbidden("pods/exec", "create"),
            list_argv(REPLICA, "hippo"): forbidden("pods/exec", "create"),
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


def test_every_task_is_a_declared_write_with_effects_dry_run_and_a_420s_budget():
    assert set(TASKS) == {"reinit-member", "switchover", "rolling-restart"}
    for t in TASKS.values():
        assert t["readOnly"] is False and t["dryRun"] is True and t["effects"]
        assert t["timeout"] == "420s"
        assert "timeout" in t["inputs"]
    assert (
        TASKS["reinit-member"]["destructive"] is True and TASKS["switchover"]["destructive"] is True
    )
    assert (
        TASKS["rolling-restart"]["destructive"] is False
        and TASKS["rolling-restart"]["idempotent"] is False
    )


def test_rolling_restart_effects_say_replicas_first_leader_last_and_what_else_rolls():
    effects = " ".join(TASKS["rolling-restart"]["effects"])
    assert "replicas first and the leader last" in effects
    assert "waiting for each member to be back before the next" in effects
    assert "pgBouncer" in effects and "pgBackRest" in effects and "restarted" in effects
    assert "postgres-operator.crunchydata.com/restarted" not in effects


def test_rolling_restart_keeps_one_task_with_exec_documented_as_zalando_only():
    raw = (SKILL / "tasks.yaml").read_text()
    assert "pods/exec is used ONLY for Zalando" in raw
    assert "`pods/exec` is used only for Zalando" in (SKILL / "SKILL.md").read_text()
    assert ("", "pods/exec", "create") in declared(TASKS["rolling-restart"])


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


@pytest.mark.parametrize(
    "task,name,inputs",
    [
        ("reinit_member", "hippo", {"member": REPLICA, "timeout": "0s"}),
        ("switchover", "hippo", {"timeout": "0s", "candidate": REPLICA}),
        ("rolling_restart", "hippo", {}),
        ("rolling_restart", "acid-zalando", {}),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(
    fast_clock, task, name, inputs
):
    sim = Sim(name, fast_clock) if name != "hippo" or task != "rolling_restart" else None
    if sim is None:
        env, errors, calls = run(
            task,
            name,
            standard(name, {patch_argv(name): ok("patched")}, pods=pods_with(name, NOW_ISO)),
            **inputs,
        )
    else:
        env, errors, calls = run_sim(task, name, sim, **inputs)
    assert env["status"] == "ok", errors
    needed = set()
    for argv in calls:
        needed |= rbac_needs(argv)
    missing = needed - declared(TASKS[task.replace("_", "-")])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"
    if name == "acid-zalando":
        assert ("", "pods/exec", "create") in needed  # exec is the Zalando path
    if name == "hippo" and task == "rolling_restart":
        assert all("exec" not in c for c in calls)  # the Crunchy restart never execs


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
    write = exec_argv(LEADER, "hippo", "patronictl", "reinit", "hippo-ha", REPLICA, "--force")
    stubs = standard(
        "hippo",
        {
            list_argv(LEADER, "hippo"): ok(PATRONI["hippo"]),
            write: ok(f"Success: reinitialize for member {REPLICA}\n{big}\n"),
        },
    )
    env, _, _ = run("reinit_member", "hippo", stubs, member=REPLICA, timeout="0s")
    r = env["outputs"]["reinit"]
    assert len(r["output"]) == 1024 and r["outputTruncated"] is True
    dump = json.dumps(env["outputs"]).lower()
    assert "password" not in dump and "postgres_host_auth_method" not in dump
    check_outputs("reinit_member", env)
