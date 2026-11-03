"""Unit tests for k8s-redis-health: lib/ and the thin scripts, against JSON and INFO output
captured from the kind-rw-plugins scenario rw-redis (a primary and a replica, a replica whose
primary does not exist, a Redis with no password, a Redis whose password the container lacks).
No test calls a cluster: scripts run against a fake ctx that answers kubectl from the captured
files, answers redis-cli like a server, and records every call."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_redis_health import k8s
from k8s_redis_health import redis as r
from k8s_redis_health import workload as w
from k8s_redis_health.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-redis-health"
DATA = Path(__file__).parent / "data" / "k8s_redis_health"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def text(name):
    return (DATA / f"{name}.txt").read_text()


STS = load("sts_redis")
ORPHAN = load("sts_orphan")
OPEN = load("deploy_open")
LOCKED = load("deploy_locked")
RS = load("replicasets")["items"]
PODS = load("pods")["items"]

TARGETS = {
    "redis": ("kubernetes/statefulset", "statefulsets.apps", STS),
    "redis-orphan": ("kubernetes/statefulset", "statefulsets.apps", ORPHAN),
    "redis-open": ("kubernetes/deployment", "deployments.apps", OPEN),
    "redis-locked": ("kubernetes/deployment", "deployments.apps", LOCKED),
}
# the INFO each pod answers
INFO = {
    "redis-0": text("info_primary"),
    "redis-1": text("info_replica"),
    "redis-orphan-0": text("info_orphan"),
}
INFO_REPLICATION = {
    "redis-0": text("info_replication_primary"),
    "redis-1": text("info_replication_replica"),
    "redis-orphan-0": text("info_replication_orphan"),
}
REFERENCE_TIME = datetime(2026, 11, 4, 9, 14, 2, 123456, tzinfo=timezone.utc)
KEY = "rw:healthcheck:20261104T091402123456"


class FakeCommandError(RwError):
    """What the v13 runner raises for a non-zero exit when check is true."""

    def __init__(self, argv, returncode, stderr):
        super().__init__("internal", f"{argv[0]} exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


def pod_named(prefix):
    return next(p for p in PODS if p["metadata"]["name"].startswith(prefix))


class FakeCtx:
    """What a script sees: resource, settings, dry_run, now(), error(), run().

    kubectl reads answer from the captured objects. `redis` is a callable
    (pod name, redis-cli args) -> (rc, stdout, stderr), or None for a server model: INFO from the
    captured output, PING -> PONG, SET/GET/DEL on a dict of keys, replicas answer READONLY.
    `answers` are (substring of the joined argv, (rc, stdout, stderr)) checked first.
    """

    def __init__(self, name="redis", answers=(), dry_run=False, pods=None, redis=None, doc=None):
        rtype, self.resource_plural, base = TARGETS[name]
        self.doc = copy.deepcopy(doc or base)
        self.resource = SimpleNamespace(namespace="rw-redis", name=name, type=rtype)
        self.settings = SimpleNamespace(max_items=200, memory_percent_threshold=90)
        self.dry_run = dry_run
        self.pods = copy.deepcopy(pods if pods is not None else PODS)
        self.answers = list(answers)
        self.redis = redis or self.server
        self.keys = {}
        self.calls = []
        self.errors = []

    def now(self):
        return REFERENCE_TIME

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message, "hint": hint})

    def lines(self):
        return [" ".join(c[1:]) for c in self.calls]

    def execs(self):
        """[(pod, container, redis-cli args)] of every kubectl exec."""
        out = []
        for argv in self.calls:
            if "exec" in argv:
                i = argv.index("exec")
                sh = argv.index("sh", argv.index("--") + 2)  # the `sh` after `sh -c <script>`
                out.append((argv[i + 1], argv[argv.index("-c") + 1], argv[sh + 1 :]))
        return out

    def run(self, argv, check=True, **kwargs):
        """Like the v13 runner: a non-zero exit raises unless the caller passes check=False."""
        self.calls.append(argv)
        line = " ".join(argv[1:])
        answer = next((a for needle, a in self.answers if needle in line), None)
        rc, out, err = answer if answer is not None else self.default(argv, line)
        if rc and check:
            raise FakeCommandError(argv, rc, err)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def default(self, argv, line):
        if f"get {self.resource_plural} {self.resource.name} " in line + " ":
            return 0, json.dumps(self.doc), ""
        if "get replicasets.apps" in line:
            return 0, json.dumps({"items": RS}), ""
        if "get pods" in line:
            return 0, json.dumps({"items": self.pods}), ""
        if " exec " in f" {line} ":
            pod = argv[argv.index("exec") + 1]
            args = argv[argv.index("sh", argv.index("--") + 2) + 1 :]
            return self.redis(pod, args)
        raise AssertionError(f"unexpected call {argv}")

    def server(self, pod, args):
        """A Redis server: redis-0 and the open and locked deployments' pods are primaries."""
        cmd = args[0]
        if pod.startswith("redis-locked"):
            return 0, "NOAUTH Authentication required.\n\n", ""
        if cmd == "PING":
            return 0, "PONG\n", ""
        if cmd == "INFO":
            if len(args) > 1:
                return 0, INFO_REPLICATION.get(pod, INFO_REPLICATION["redis-0"]), ""
            return 0, INFO.get(pod, INFO["redis-0"]), ""
        if pod in ("redis-1", "redis-orphan-0") and cmd in ("SET", "DEL"):
            return 0, "READONLY You can't write against a read only replica.\n\n", ""
        if cmd == "SET":
            assert args[3:] == ["EX", "60"]
            self.keys[args[1]] = args[2]
            return 0, "OK\n", ""
        if cmd == "GET":
            return 0, self.keys.get(args[1], "") + "\n", ""
        if cmd == "DEL":
            return 0, f"{int(self.keys.pop(args[1], None) is not None)}\n", ""
        raise AssertionError(f"unexpected redis-cli {args}")


def script(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def check_outputs(task, out):
    """Every output validates against the schema its tasks.yaml entry names."""
    assert set(out) == set(TASKS[task]["outputs"])
    for key, value in out.items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        errors = list(Draft202012Validator(schema).iter_errors(value))
        assert not errors, (task, key, errors[:2])


def ping(ctx, **inputs):
    out = script("ping")(ctx, **inputs)
    check_outputs("ping", out)
    return out


def rw(ctx, **inputs):
    out = script("read_write_check")(ctx, **inputs)
    check_outputs("read-write-check", out)
    return out


def by_pod(items):
    return {i["pod"]: i for i in items}


# ------------------------------------------------------------ INFO facts ----


def test_primary_facts_come_from_info():
    facts = r.instance_facts(r.parse_info(INFO["redis-0"]), 90)
    rep = facts["replication"]
    assert rep["role"] == "master"
    assert rep["connectedReplicas"] == 1
    assert rep["replicas"][0]["state"] == "online"
    assert rep["masterLinkStatus"] is None  # a primary has no link
    assert facts["server"]["redisVersion"].startswith("7.")
    assert facts["memory"]["maxBytes"] == 48 * 1024 * 1024
    assert facts["memory"]["maxmemoryPolicy"] == "allkeys-lru"
    assert facts["memory"]["overThreshold"] is False
    assert 0 < facts["memory"]["usedPercent"] < 90
    assert facts["stats"] == {"evictedKeys": 0, "rejectedConnections": 0}
    assert facts["persistence"]["rdb"]["lastBgsaveStatus"] == "ok"
    assert facts["persistence"]["rdb"]["lastSaveTime"].endswith("Z")
    assert facts["persistence"]["aof"]["enabled"] is False


def test_replica_link_up_and_down():
    up = r.instance_facts(r.parse_info(INFO["redis-1"]), 90)["replication"]
    assert (up["role"], up["masterLinkStatus"]) == ("slave", "up")
    assert up["masterHost"] == "redis-0.redis.rw-redis.svc"
    assert up["masterLastIoSecondsAgo"] is not None
    down = r.instance_facts(r.parse_info(INFO["redis-orphan-0"]), 90)["replication"]
    assert (down["role"], down["masterLinkStatus"]) == ("slave", "down")
    # Redis reports -1 for "never connected": null, not a number that reads like a time
    assert down["masterLinkDownSinceSeconds"] is None
    assert down["masterLastIoSecondsAgo"] is None


def test_no_maxmemory_means_no_percent_and_no_threshold_verdict():
    mem = r.instance_facts(r.parse_info(INFO["redis-orphan-0"]), 90)["memory"]
    assert (mem["maxBytes"], mem["usedPercent"], mem["overThreshold"]) == (0, None, None)


def test_memory_threshold_is_a_setting():
    fields = {"used_memory": "950", "maxmemory": "1000"}
    assert r.instance_facts(fields, 90)["memory"]["overThreshold"] is True
    assert r.instance_facts(fields, 96)["memory"]["overThreshold"] is False
    assert (
        r.instance_facts({**fields, "used_memory": "899"}, 90)["memory"]["overThreshold"] is False
    )


def test_a_field_the_server_does_not_report_is_null_not_zero():
    facts = r.instance_facts({"redis_version": "5.0.7", "role": "master"}, 90)
    assert facts["stats"] == {"evictedKeys": None, "rejectedConnections": None}
    assert facts["clients"]["connected"] is None
    assert facts["persistence"]["rdb"]["lastBgsaveStatus"] is None
    assert facts["replication"]["connectedReplicas"] is None


def test_replica_list_is_bounded_with_a_flag():
    fields = {"role": "master", "connected_slaves": "60"}
    for i in range(60):
        fields[f"slave{i}"] = f"ip=10.0.0.{i},port=6379,state=online,offset=1,lag=0"
    rep = r.instance_facts(fields, 90)["replication"]
    assert len(rep["replicas"]) == w.NESTED_MAX
    assert rep["replicasTruncated"] is True
    assert rep["connectedReplicas"] == 60


def test_replicas_are_read_in_slave_order():
    fields = {"slave10": "ip=b,port=1", "slave2": "ip=a,port=1", "slave0": "ip=z,port=1"}
    assert [x["ip"] for x in r._replicas(fields)] == ["z", "a", "b"]


def test_info_without_the_asked_section_is_an_internal_error():
    with pytest.raises(RwError) as exc:
        r.check_info(r.parse_info("WRONGTYPE nonsense\r\n"))
    assert exc.value.code == "internal"
    assert r.check_info(r.parse_info(INFO_REPLICATION["redis-0"]), "role")["role"] == "master"


# --------------------------------------------------------- credentials ----


def test_auth_failures_are_recognised_by_the_start_of_a_line():
    assert r.auth_failure("NOAUTH Authentication required.\n\n")
    assert r.auth_failure("AUTH failed: WRONGPASS invalid username-password pair\nPONG")
    assert r.auth_failure("WRONGPASS invalid username-password pair or user is disabled.")
    assert r.auth_failure("NOPERM this user has no permissions to run the 'info' command")
    assert not r.auth_failure("PONG")


def test_info_that_lists_earlier_auth_errors_is_not_an_auth_failure():
    """INFO's errorstats section carries errorstat_NOAUTH counters; a mid-line match must not
    make a healthy server look like it refused the credentials."""
    info = INFO["redis-0"] + "errorstat_NOAUTH:count=3\r\nerrorstat_WRONGPASS:count=1\r\n"
    assert not r.auth_failure(info)
    ctx = FakeCtx(answers=[("INFO", (0, info, ""))])
    out = ping(ctx)
    assert out["counts"]["failed"] == 0 and ctx.errors == []


def test_the_exec_script_takes_the_password_from_the_containers_own_env():
    assert "REDIS_PASSWORD" in r.EXEC_SCRIPT and "REDISCLI_AUTH" in r.EXEC_SCRIPT
    assert 'exec redis-cli "$@"' in r.EXEC_SCRIPT
    assert "-a " not in r.EXEC_SCRIPT and "--pass" not in r.EXEC_SCRIPT


def test_no_call_reads_a_secret_or_puts_a_password_on_a_command_line():
    ctx = FakeCtx()
    rw(ctx)
    ping(ctx)
    for argv in ctx.calls:
        joined = " ".join(argv)
        assert "secret" not in joined.lower()
        assert "printenv" not in joined and " env" not in joined
        assert "-a " not in joined and "REDISCLI_AUTH=" not in " ".join(
            argv[: argv.index("sh") if "sh" in argv else len(argv)]
        )
        if "exec" in argv:
            # the only shell text is the constant script; everything after `sh` is redis-cli args
            assert argv[argv.index("-c", argv.index("--")) + 1] == r.EXEC_SCRIPT


def test_outputs_never_contain_an_env_value_or_a_password():
    ctx = FakeCtx()
    blob = json.dumps({"p": ping(ctx), "w": rw(FakeCtx())})
    assert "rw-redis-test-password" not in blob
    assert "REDIS_PASSWORD" not in blob


# ------------------------------------------------------------- workload ----


def test_resolve_target_from_type_then_from_path():
    assert w.resolve_target("kubernetes/statefulset") == ("StatefulSet", "statefulsets.apps")
    assert w.resolve_target(None, "kubernetes/clusters/k/namespaces/n/deployments/r") == (
        "Deployment",
        "deployments.apps",
    )
    for bad in ("kubernetes/daemonset", "kubernetes/pod"):
        with pytest.raises(RwError) as exc:
            w.resolve_target(bad)
        assert exc.value.code == "invalid_input"
    with pytest.raises(RwError):
        w.resolve_target(None, "kubernetes/clusters/k/namespaces/n/daemonsets/x")


def test_a_daemonset_target_is_refused_before_any_kubectl_call():
    ctx = FakeCtx()
    ctx.resource.type = "kubernetes/daemonset"
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_owned_pods_follow_selector_and_owner_chain():
    sts = w.owned_pods("StatefulSet", STS, [], PODS)
    assert [p["metadata"]["name"] for p in sts] == ["redis-0", "redis-1"]
    dep = w.owned_pods("Deployment", OPEN, RS, PODS)
    assert [p["metadata"]["name"] for p in dep] == [pod_named("redis-open")["metadata"]["name"]]


def test_a_pod_with_the_right_labels_but_another_controller_is_not_owned():
    stray = copy.deepcopy(pod_named("redis-1"))
    stray["metadata"]["name"] = "redis-stray"
    stray["metadata"]["ownerReferences"] = [
        {"kind": "StatefulSet", "name": "other", "uid": "not-ours", "controller": True}
    ]
    names = [p["metadata"]["name"] for p in w.owned_pods("StatefulSet", STS, [], [*PODS, stray])]
    assert "redis-stray" not in names


def test_empty_selector_raises_rather_than_selecting_the_namespace():
    with pytest.raises(RwError) as exc:
        w.selector_string({})
    assert exc.value.code == "invalid_input"


def container_pod(*containers):
    return {
        "metadata": {"name": "p"},
        "spec": {"containers": [{"name": n, "image": i} for n, i in containers]},
    }


def test_container_choice():
    assert (
        w.choose_container(container_pod(("redis", "redis:7"), ("exporter", "x/redis_exporter")))
        == "redis"
    )
    assert (
        w.choose_container(
            container_pod(
                ("metrics", "oliver006/redis_exporter:v1"),
                ("cache", "docker.io/library/redis:7-alpine"),
            )
        )
        == "cache"
    )
    assert w.choose_container(container_pod(("a", "busybox"), ("b", "alpine"))) == "a"
    assert w.choose_container(container_pod(("redis", "redis:7"), ("side", "x")), "side") == "side"
    with pytest.raises(RwError) as exc:
        w.choose_container(container_pod(("redis", "redis:7")), "nope")
    assert exc.value.code == "invalid_input"


def test_a_requested_container_the_pods_lack_is_invalid_input_before_any_exec():
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        ping(ctx, container="nope")
    assert exc.value.code == "invalid_input" and ctx.execs() == []


# ----------------------------------------------------------------- ping ----


def test_ping_reports_each_pod_with_facts():
    ctx = FakeCtx()
    out = ping(ctx)
    pods = by_pod(out["instances"])
    assert set(pods) == {"redis-0", "redis-1"}
    assert pods["redis-0"]["ping"]["ok"] is True
    assert pods["redis-0"]["ping"]["response"] == "PONG"
    assert pods["redis-0"]["ping"]["latencyMs"] >= 0
    assert pods["redis-0"]["info"]["replication"]["role"] == "master"
    assert pods["redis-1"]["info"]["replication"]["masterLinkStatus"] == "up"
    assert out["counts"] == {"pods": 2, "ready": 2, "queried": 2, "failed": 0}
    assert ctx.errors == []


def test_ping_asks_every_ready_pod_in_the_redis_container():
    ctx = FakeCtx()
    ping(ctx)
    assert ctx.execs() == [
        ("redis-0", "redis", ["PING"]),
        ("redis-0", "redis", ["INFO"]),
        ("redis-1", "redis", ["PING"]),
        ("redis-1", "redis", ["INFO"]),
    ]


def test_the_broken_replica_link_is_reported():
    ctx = FakeCtx("redis-orphan")
    out = ping(ctx)
    entry = out["instances"][0]
    assert entry["ping"]["ok"] is True  # the process answers; its link is what is down
    assert entry["info"]["replication"]["role"] == "slave"
    assert entry["info"]["replication"]["masterLinkStatus"] == "down"


def test_deployment_pods_are_found_through_their_replicaset():
    ctx = FakeCtx("redis-open")
    out = ping(ctx)
    assert [i["pod"] for i in out["instances"]] == [pod_named("redis-open")["metadata"]["name"]]
    assert any("get replicasets.apps" in line for line in ctx.lines())


def test_a_pod_that_is_not_ready_is_listed_and_not_asked():
    pods = copy.deepcopy(PODS)
    for p in pods:
        if p["metadata"]["name"] == "redis-1":
            for c in p["status"]["conditions"]:
                if c["type"] == "Ready":
                    c["status"] = "False"
    ctx = FakeCtx(pods=pods)
    out = ping(ctx)
    pod = by_pod(out["instances"])["redis-1"]
    assert (pod["ready"], pod["queried"], pod["ping"], pod["info"], pod["error"]) == (
        False,
        False,
        None,
        None,
        None,
    )
    assert {e[0] for e in ctx.execs()} == {"redis-0"}
    assert out["counts"] == {"pods": 2, "ready": 1, "queried": 1, "failed": 0}


def test_a_workload_with_no_pods_lists_none():
    ctx = FakeCtx(pods=[])
    out = ping(ctx)
    assert out["instances"] == []
    assert out["counts"]["pods"] == 0 and ctx.execs() == []


def test_a_reply_that_is_not_pong_is_a_fact_not_an_error():
    ctx = FakeCtx(answers=[("PING", (0, "LOADING Redis is loading the dataset in memory\n", ""))])
    out = ping(ctx)
    entry = by_pod(out["instances"])["redis-0"]
    assert entry["ping"]["ok"] is False
    assert entry["ping"]["response"].startswith("LOADING")
    assert ctx.errors == []


def test_a_long_reply_is_truncated_with_a_flag():
    ctx = FakeCtx(answers=[("PING", (0, "ERR " + "x" * 3000 + "\n", ""))])
    entry = by_pod(ping(ctx)["instances"])["redis-0"]
    assert len(entry["ping"]["response"]) == 1024 and entry["ping"]["truncated"] is True


def test_instances_are_capped_by_max_items_and_counts_are_not():
    ctx = FakeCtx()
    ctx.settings.max_items = 1
    out = ping(ctx)
    assert len(out["instances"]) == 1 and out["counts"]["pods"] == 2


# ---------------------------------------------------------------- errors ----

NOT_FOUND = 'Error from server (NotFound): statefulsets.apps "redis" not found'
FORBIDDEN = (
    'Error from server (Forbidden): statefulsets.apps "redis" is forbidden: User "u" cannot get '
    'resource "statefulsets" in API group "apps" in the namespace "rw-redis"'
)
UNREACHABLE = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"


@pytest.mark.parametrize("script_name", ["ping", "read_write_check"])
@pytest.mark.parametrize(
    "stderr,code",
    [(NOT_FOUND, "not_found"), (FORBIDDEN, "permission_denied"), (UNREACHABLE, "unreachable")],
)
def test_a_failed_read_of_the_target_raises_with_its_code(script_name, stderr, code):
    ctx = FakeCtx(answers=[("get statefulsets.apps redis", (1, "", stderr))])
    with pytest.raises(RwError) as exc:
        script(script_name)(ctx)
    assert exc.value.code == code and ctx.execs() == []


@pytest.mark.parametrize("script_name", ["ping", "read_write_check"])
def test_a_forbidden_pod_list_raises_and_never_returns_an_empty_list(script_name):
    ctx = FakeCtx(
        answers=[("get pods", (1, "", 'pods is forbidden: User "u" cannot list resource "pods"'))]
    )
    with pytest.raises(RwError) as exc:
        script(script_name)(ctx)
    assert exc.value.code == "permission_denied"


def test_a_forbidden_replicaset_list_raises_for_a_deployment():
    ctx = FakeCtx(
        "redis-open",
        answers=[("get replicasets.apps", (1, "", "replicasets.apps is forbidden: cannot list"))],
    )
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "permission_denied"


EXEC_FORBIDDEN = (
    'Error from server (Forbidden): pods "redis-0" is forbidden: User "u" cannot create '
    'resource "pods/exec" in API group "" in the namespace "rw-redis"'
)


def test_forbidden_exec_on_every_pod_fails_the_task_with_permission_denied():
    ctx = FakeCtx(answers=[(" exec ", (1, "", EXEC_FORBIDDEN))])
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "permission_denied"
    assert ctx.errors == []  # raised, not also reported


def test_a_missing_redis_cli_is_a_dependency_error():
    ctx = FakeCtx(
        answers=[
            (
                " exec ",
                (
                    127,
                    "",
                    "sh: exec: line 0: redis-cli: not found\ncommand terminated with exit code 127",
                ),
            )
        ]
    )
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "dependency"


def test_a_password_the_container_lacks_is_permission_denied_with_a_hint():
    ctx = FakeCtx("redis-locked")
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "permission_denied"
    assert "REDIS_PASSWORD" in exc.value.message
    assert [e[2][0] for e in ctx.execs()] == ["PING"]  # stopped at the refusal


def test_one_pod_failing_makes_the_result_partial_with_the_others_kept():
    def redis(pod, args):
        if pod == "redis-1":
            return 1, "", "Could not connect to Redis at 127.0.0.1:6379: Connection refused"
        return FakeCtx().server(pod, args)

    ctx = FakeCtx(redis=redis)
    out = ping(ctx)
    pods = by_pod(out["instances"])
    assert pods["redis-0"]["info"] is not None and pods["redis-0"]["error"] is None
    assert pods["redis-1"]["error"]["code"] == "unreachable"
    assert pods["redis-1"]["queried"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "pod/redis-1")]
    assert out["counts"]["failed"] == 1


def test_info_failing_after_a_good_ping_keeps_the_ping():
    def redis(pod, args):
        if args[0] == "INFO" and pod == "redis-0":
            return 1, "", "Could not connect to Redis at 127.0.0.1:6379: Connection refused"
        return FakeCtx().server(pod, args)

    ctx = FakeCtx(redis=redis)
    out = ping(ctx)
    entry = by_pod(out["instances"])["redis-0"]
    assert entry["ping"]["ok"] is True and entry["info"] is None
    assert entry["error"]["code"] == "unreachable"
    assert by_pod(out["instances"])["redis-1"]["info"] is not None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "pod/redis-0")]


def test_exec_timeout_is_a_timeout_error():
    import subprocess

    ctx = FakeCtx()

    def run(argv, check=True, **kw):
        raise subprocess.TimeoutExpired(argv, 20)

    ctx.run = run
    with pytest.raises(RwError) as exc:
        from k8s_redis_health.fetch import redis_cli

        redis_cli(ctx, pod_named("redis-0"), "redis", "PING")
    assert exc.value.code == "timeout"


def test_the_error_message_never_names_the_exec_command_line():
    stderr = "boom"
    err = k8s.failure(
        ["kubectl", "exec", "p", "--", "sh", "-c", r.EXEC_SCRIPT],
        SimpleNamespace(returncode=1, stderr=stderr),
    )
    assert "REDIS_PASSWORD" not in err.message and "sh -c" not in err.message


@pytest.mark.parametrize(
    "rc,stderr,code",
    [
        (1, FORBIDDEN, "permission_denied"),
        (1, NOT_FOUND, "not_found"),
        (1, UNREACHABLE, "unreachable"),
        (124, "", "timeout"),
        (127, "", "dependency"),
        (1, "error: the server doesn't have a resource type", "dependency"),
        (1, "Error from server (TooManyRequests): 429", "rate_limited"),
        (1, "something odd", "internal"),
    ],
)
def test_kubectl_failures_are_classified(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


def test_non_json_output_is_an_internal_error():
    ctx = FakeCtx(answers=[("get statefulsets.apps redis", (0, "not json", ""))])
    with pytest.raises(RwError) as exc:
        ping(ctx)
    assert exc.value.code == "internal"


# ----------------------------------------------------- read-write-check ----


def test_the_check_writes_reads_and_deletes_on_the_primary_only():
    ctx = FakeCtx()
    out = rw(ctx)
    primary_steps = [e for e in ctx.execs() if e[2][0] in ("SET", "GET", "DEL")]
    assert primary_steps == [
        ("redis-0", "redis", ["SET", KEY, "20261104T091402123456", "EX", "60"]),
        ("redis-0", "redis", ["GET", KEY]),
        ("redis-0", "redis", ["DEL", KEY]),
    ]
    check = out["check"]
    assert check["key"] == KEY and check["ttlSeconds"] == 60
    assert (check["ran"], check["reason"], check["dryRun"]) == (True, None, False)
    [primary] = check["primaries"]
    assert primary["pod"] == "redis-0"
    assert [(s["step"], s["performed"], s["ok"]) for s in primary["steps"]] == [
        ("set", True, True),
        ("get", True, True),
        ("del", True, True),
    ]
    assert all(s["latencyMs"] >= 0 for s in primary["steps"])
    assert (primary["allOk"], primary["valueMatches"], primary["keyRemoved"]) == (True, True, True)
    assert ctx.keys == {}  # nothing left behind
    assert [c["role"] for c in out["candidates"]] == ["master", "slave"]


def test_the_key_is_unique_to_the_run_and_comes_from_ctx_now():
    first, second = FakeCtx(), FakeCtx()
    second.now = lambda: datetime(2026, 11, 4, 9, 14, 3, tzinfo=timezone.utc)
    assert rw(first)["check"]["key"] != rw(second)["check"]["key"]
    assert rw(second)["check"]["key"] == "rw:healthcheck:20261104T091403000000"


def test_a_dry_run_finds_the_primary_and_writes_nothing():
    ctx = FakeCtx(dry_run=True)
    out = rw(ctx)
    assert not [e for e in ctx.execs() if e[2][0] in ("SET", "GET", "DEL")]
    check = out["check"]
    assert (check["dryRun"], check["ran"], check["reason"]) == (True, False, "dry_run")
    [primary] = check["primaries"]
    assert primary["pod"] == "redis-0"
    assert all(s["performed"] is False and s["ok"] is None for s in primary["steps"])
    assert primary["allOk"] is None


def test_a_redis_with_no_primary_is_reported_and_nothing_is_written():
    ctx = FakeCtx("redis-orphan")
    out = rw(ctx)
    assert out["check"]["reason"] == "no_primary" and out["check"]["ran"] is False
    assert out["check"]["primaries"] == []
    assert [c["role"] for c in out["candidates"]] == ["slave"]
    assert not [e for e in ctx.execs() if e[2][0] in ("SET", "GET", "DEL")]


def test_no_ready_pod_is_reported_and_nothing_is_asked():
    pods = copy.deepcopy(PODS)
    for p in pods:
        for c in p["status"]["conditions"]:
            if c["type"] == "Ready":
                c["status"] = "False"
    ctx = FakeCtx(pods=pods)
    out = rw(ctx)
    assert out["check"]["reason"] == "no_ready_pods" and ctx.execs() == []
    assert all(c["role"] is None for c in out["candidates"])


def test_a_primary_that_refuses_the_set_is_reported_and_nothing_follows():
    ctx = FakeCtx(answers=[("SET", (0, "MISCONF Redis is configured to save RDB snapshots\n", ""))])
    out = rw(ctx)
    [primary] = out["check"]["primaries"]
    steps = {s["step"]: s for s in primary["steps"]}
    assert steps["set"]["performed"] and steps["set"]["ok"] is False
    assert steps["set"]["reply"].startswith("MISCONF")
    assert steps["get"]["performed"] is False and steps["del"]["performed"] is False
    assert (primary["allOk"], primary["valueMatches"], primary["keyRemoved"]) == (False, None, None)
    assert ctx.errors == []  # a refusal is a fact about the server


def test_a_get_that_returns_another_value_is_a_mismatch_and_the_key_is_still_deleted():
    ctx = FakeCtx(answers=[("GET", (0, "something else\n", ""))])
    out = rw(ctx)
    [primary] = out["check"]["primaries"]
    assert primary["valueMatches"] is False and primary["allOk"] is False
    assert primary["keyRemoved"] is True
    assert [e[2][0] for e in ctx.execs() if e[2][0] in ("SET", "GET", "DEL")] == [
        "SET",
        "GET",
        "DEL",
    ]


def test_a_failed_get_still_tries_the_delete_and_the_result_is_partial():
    ctx = FakeCtx(
        answers=[
            ("GET", (1, "", "Could not connect to Redis at 127.0.0.1:6379: Connection refused"))
        ]
    )
    out = rw(ctx)
    [primary] = out["check"]["primaries"]
    steps = {s["step"]: s for s in primary["steps"]}
    assert steps["get"]["ok"] is False and steps["get"]["errorCode"] == "unreachable"
    assert steps["del"]["ok"] is True
    assert primary["valueMatches"] is False and primary["allOk"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "pod/redis-0")]


def test_a_set_that_could_not_run_skips_the_get_and_still_tries_the_delete():
    ctx = FakeCtx(answers=[("SET", (1, "", "error: unable to upgrade connection: i/o timeout"))])
    out = rw(ctx)
    [primary] = out["check"]["primaries"]
    steps = {s["step"]: s for s in primary["steps"]}
    assert steps["set"]["errorCode"] == "unreachable"
    assert steps["get"]["performed"] is False
    assert steps["del"]["performed"] is True and primary["allOk"] is False


def test_the_check_on_a_redis_whose_password_the_container_lacks_is_permission_denied():
    ctx = FakeCtx("redis-locked")
    with pytest.raises(RwError) as exc:
        rw(ctx)
    assert exc.value.code == "permission_denied"
    assert not [e for e in ctx.execs() if e[2][0] in ("SET", "GET", "DEL")]


def test_forbidden_exec_fails_the_check_and_writes_nothing():
    ctx = FakeCtx(answers=[(" exec ", (1, "", EXEC_FORBIDDEN))])
    with pytest.raises(RwError) as exc:
        rw(ctx)
    assert exc.value.code == "permission_denied"


def test_a_deployment_target_uses_its_only_pod():
    ctx = FakeCtx("redis-open")
    out = rw(ctx)
    assert out["check"]["primaries"][0]["pod"] == pod_named("redis-open")["metadata"]["name"]
    assert out["check"]["primaries"][0]["allOk"] is True


def test_two_primaries_are_both_checked_and_a_replica_never():
    pods = copy.deepcopy(PODS)

    def redis(pod, args):
        if pod == "redis-1" and args[0] == "INFO":
            return 0, INFO_REPLICATION["redis-0"], ""  # claims to be a master too
        return FakeCtx().server(pod, args)

    ctx = FakeCtx(pods=pods, redis=redis)
    ctx.redis = lambda pod, args: (redis(pod, args) if args[0] != "SET" else (0, "OK\n", ""))
    out = rw(ctx)
    assert [p["pod"] for p in out["check"]["primaries"]] == ["redis-0", "redis-1"]


# ----------------------------------------------------------- permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "deployments.apps": ("apps", "deployments"),
    "replicasets.apps": ("apps", "replicasets"),
    "pods": ("", "pods"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-c", "--container", "-l", "--selector"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l) -> list;
    exec -> pods get + pods/exec create. Raises on anything else.
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
    if verb == "exec":
        return {("", "pods", "get"), ("", "pods/exec", "create")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, res, v)
        for rule in TASKS[task]["permissions"]
        for g in rule["apiGroups"]
        for res in rule["resources"]
        for v in rule["verbs"]
    }


@pytest.mark.parametrize(
    "task,module", [("ping", "ping"), ("read-write-check", "read_write_check")]
)
@pytest.mark.parametrize("target", ["redis", "redis-open"])
@pytest.mark.parametrize("dry", [False, True])
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, module, target, dry):
    ctx = FakeCtx(target, dry_run=dry)
    script(module)(ctx)
    needed = set()
    for argv in ctx.calls:
        needed |= rbac_needs(argv)
    assert needed <= declared(task), sorted(needed - declared(task))
    # and nothing declared beyond what the scripts can call (exec: create and get on pods/exec)
    assert declared(task) - needed <= {
        ("apps", "deployments", "get"),
        ("apps", "statefulsets", "get"),
        ("apps", "replicasets", "list"),
        ("", "pods/exec", "get"),
    }


def test_exec_declares_pod_get_and_exec_create_and_get():
    for task in TASKS:
        assert {
            ("", "pods", "get"),
            ("", "pods/exec", "create"),
            ("", "pods/exec", "get"),
        } <= declared(task)


def test_permissions_name_one_api_group_per_rule_and_no_secrets():
    for task in TASKS.values():
        for rule in task["permissions"]:
            assert len(rule["apiGroups"]) == 1
            assert "secrets" not in rule["resources"]


def test_the_write_task_declares_its_effects_and_a_dry_run():
    task = TASKS["read-write-check"]
    assert task["readOnly"] is False and task["destructive"] is False
    assert task["idempotent"] is True and task["dryRun"] is True and task["effects"]
    assert TASKS["ping"]["readOnly"] is True


def test_every_schema_is_valid_json_schema():
    for path in (SKILL / "schemas").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))
