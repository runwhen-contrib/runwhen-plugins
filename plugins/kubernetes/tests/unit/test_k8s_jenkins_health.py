"""Unit tests for k8s-jenkins-health's lib/ and scripts. The captured data comes from the
`rw-jenkins` namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-jenkins-health.yaml): `jenkins` (healthy), `jenkins-stuck` (a queue item waiting for days, a
blocked one, an offline node) and `jenkins-locked` (anonymous read off: the JSON API answers 403).
All three are small python stubs serving the documented remote API, not a real Jenkins. The
workload and pod data are the narrow `-o jsonpath` projections the scripts ask for (no env, no pod
template). The scripts run through the SDK's test context (`ctx.run` raises on a non-zero exit like
the real runner; `stub_commands` matches the exact kubectl argv, so an unexpected call fails)."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_jenkins_health import build_queue, fetch, http_status, project, proxy
from k8s_jenkins_health.errors import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-jenkins-health"
DATA = Path(__file__).parent / "data" / "k8s_jenkins_health"
UTC = timezone.utc
NS = "rw-jenkins"
CAPTURED = datetime(2026, 11, 3, 23, 47, 41, tzinfo=UTC)  # just after the queue data was captured
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
SCRIPTS = {"http-status": "http_status", "queue": "queue_status"}
OUTPUT = {"http-status": "login", "queue": "queue"}
SETTINGS = {"maxItems": 200}
FORBIDDEN = (
    'Error from server (Forbidden): pods "x" is forbidden: User "u" cannot get resource '
    '"pods/proxy" in API group "" in the namespace "rw-jenkins"'
)
UNREACHABLE = (
    "Error from server (ServiceUnavailable): error trying to reach service: dial tcp "
    "10.244.0.5:8080: connect: connection refused"
)
UNAVAILABLE = (
    "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
)
NO_ROUTE = "Error from server (NotFound): the server could not find the requested resource"
BACKEND_403 = "Error from server (Forbidden): <html><body>Authentication required</body></html>"
BACKEND_401 = (
    "error: You must be logged in to the server "
    "(the server has asked for the client to provide credentials)"
)
CLIENT_TIMEOUT = (
    "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)"
)
NO_HEADERS = "I1104 05:17:40.733006 1 loader.go:407] Config loaded from file: /x\n"


def load(name):
    return (DATA / name).read_text()


def script(task):
    spec = importlib.util.spec_from_file_location(
        f"jenkins_{task}", SKILL / "scripts" / f"{SCRIPTS[task]}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def raw(pod, path, scheme="http", port=8080, verbose=False):
    argv = (
        "kubectl",
        "get",
        "--raw",
        f"/api/v1/namespaces/{NS}/pods/{scheme}:{pod}:{port}/proxy{path}",
        "--request-timeout=30s",
    )
    return argv + ("-v=8",) if verbose else argv


QUEUE = build_queue.QUEUE + build_queue.QUEUE_TREE
COMPUTERS = build_queue.COMPUTERS + build_queue.COMPUTER_TREE
REPLIES = {
    "jenkins": {
        "/login": (0, load("login.html"), load("login.stderr.txt")),
        QUEUE: (0, load("queue-empty.json"), ""),
        COMPUTERS: (0, load("computers-healthy.json"), ""),
    },
    "jenkins-stuck": {
        "/login": (0, load("login.html"), load("login.stderr.txt")),
        QUEUE: (0, load("queue-stuck.json"), ""),
        COMPUTERS: (0, load("computers-stuck.json"), ""),
    },
    "jenkins-locked": {
        "/login": (0, load("login.html"), load("login.stderr.txt")),
        QUEUE: (1, "", load("queue-locked.stderr.txt")),
        COMPUTERS: (1, "", load("queue-locked.stderr.txt")),
    },
}


def projected(argv_tail, template, text, fail, key):
    argv = ("kubectl", "-n", NS, "get", *argv_tail, "-o", "jsonpath=" + template)
    return argv, ((1, "", fail[key]) if key in fail else (0, text, ""))


def stubs(name="jenkins", fail=None, replies=None, pods_text=None, kind="statefulset", **inputs):
    """kubectl answers for a run on workload `name`. `fail` maps 'workload', 'pods' or 'proxy' to
    stderr; `replies` maps a path (all pods) or (pod, path) to (rc, stdout, stderr)."""
    fail, replies = fail or {}, replies or {}
    scheme, port = inputs.get("scheme", "http"), inputs.get("port", 8080)
    prefix = inputs.get("url_prefix", "")
    pods_text = load(f"pods_{name}.txt") if pods_text is None else pods_text
    selector = f"app.kubernetes.io/instance={name},app.kubernetes.io/name=jenkins"
    out = dict(
        [
            projected(
                (f"{kind}s.apps", name),
                project.WORKLOAD_FIELDS.template(False),
                load(f"workload_{name}.txt").replace("StatefulSet", kind.capitalize())
                if kind != "statefulset"
                else load(f"workload_{name}.txt"),
                fail,
                "workload",
            ),
            projected(
                ("pods", "-l", selector),
                project.POD_FIELDS.template(True),
                pods_text,
                fail,
                "pods",
            ),
        ]
    )
    for pod in sorted(
        {p.split("=", 1)[1] for p in pods_text.splitlines() if p.startswith("s:metadata.name=")}
    ):
        for path, default in REPLIES[name].items():
            reply = replies.get((pod, path), replies.get(path, default))
            argv = raw(pod, prefix + path, scheme, port, verbose=path == "/login")
            out[argv] = (1, "", fail["proxy"]) if "proxy" in fail else reply
    return out


def run(
    task,
    name="jenkins",
    inputs=None,
    res=None,
    settings=None,
    now=CAPTURED,
    kind="statefulset",
    **kw,
):
    """(envelope, calls) of one task on workload `name`."""
    resource = res or {
        "cluster": "kind",
        "namespace": NS,
        "name": name,
        "type": f"kubernetes/{kind}",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/{kind}s/{name}",
    }
    inputs = inputs or {}
    with make_context(resource=resource, settings=settings or SETTINGS, now=now) as tc:
        calls = stub_commands(tc, stubs(name, kind=kind, **inputs, **kw))
        env = run_main(script(task), tc, **inputs)
    return env, calls


def check_schema(task, env):
    schema = json.loads((SKILL / TASKS[task]["outputs"][OUTPUT[task]]["schema"]).read_text())
    jsonschema.validate(env["outputs"][OUTPUT[task]], schema, cls=jsonschema.Draft202012Validator)


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def pod_of(env, task):
    return env["outputs"][OUTPUT[task]]["pods"][0]


# ------------------------------------------------------------ http-status ----


def test_login_healthy_is_ok_with_the_version_header():
    env, calls = run("http-status")
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["login"]
    assert out["workload"] == {
        "kind": "StatefulSet",
        "name": "jenkins",
        "replicas": 1,
        "readyReplicas": 1,
    }
    assert (out["podCount"], out["okCount"], out["notOkCount"]) == (1, 1, 0)
    pod = pod_of(env, "http-status")
    assert (pod["httpStatus"], pod["ok"], pod["version"], pod["headersExposed"]) == (
        200,
        True,
        "2.462.3",
        True,
    )
    assert pod["port"] == 8080 and pod["podReady"] is True
    assert raw("jenkins-0", "/login", verbose=True) in calls
    check_schema("http-status", env)


def test_login_on_the_stuck_and_locked_controllers_is_still_ok():
    for name in ("jenkins-stuck", "jenkins-locked"):
        env, _ = run("http-status", name)
        assert env["status"] == "ok" and pod_of(env, "http-status")["ok"] is True


def test_version_is_null_when_headers_are_shown_without_it_and_unknown_when_not_shown():
    shown = load("login.stderr.txt").replace("X-Jenkins: 2.462.3", "X-Other: 1")
    env, _ = run("http-status", replies={"/login": (0, load("login.html"), shown)})
    pod = pod_of(env, "http-status")
    assert (pod["ok"], pod["version"], pod["headersExposed"]) == (True, None, True)
    env, _ = run("http-status", replies={"/login": (0, load("login.html"), NO_HEADERS)})
    pod = pod_of(env, "http-status")
    assert (pod["ok"], pod["version"], pod["headersExposed"]) == (True, None, False)
    check_schema("http-status", env)


def test_a_200_that_is_not_jenkins_is_an_error_never_ok():
    page = "<html><body>Welcome to nginx!</body></html>"
    env, _ = run("http-status", replies={"/login": (0, page, NO_HEADERS)})
    assert env["status"] == "failed"  # the only pod gave nothing usable
    assert codes(env) == [("internal", "pod/jenkins-0")]
    assert env["outputs"] == {}
    # one good pod keeps the result partial
    two = load("pods_jenkins.txt") + load("pods_jenkins.txt").replace("jenkins-0", "jenkins-1")
    env, _ = run(
        "http-status", pods_text=two, replies={("jenkins-1", "/login"): (0, page, NO_HEADERS)}
    )
    assert env["status"] == "partial" and codes(env) == [("internal", "pod/jenkins-1")]
    pods = env["outputs"]["login"]["pods"]
    assert [p["ok"] for p in pods] == [True, None]
    check_schema("http-status", env)


def test_a_503_and_a_404_are_ok_false_facts():
    for stderr, status in ((UNAVAILABLE, 503), (NO_ROUTE, 404)):
        env, _ = run("http-status", replies={"/login": (1, "", stderr)})
        assert env["status"] == "ok" and env["errors"] == []
        pod = pod_of(env, "http-status")
        assert (pod["httpStatus"], pod["ok"]) == (status, False)
        assert env["outputs"]["login"]["notOkCount"] == 1


def test_login_wanting_credentials_skips_the_task():
    env, _ = run("http-status", replies={"/login": (1, "", BACKEND_403)})
    assert env["status"] == "skipped" and "misc plugin, M4" in env["reason"]
    assert env["errors"] == [] and env["outputs"] == {}


def test_login_rbac_refusal_fails_with_permission_denied():
    env, _ = run("http-status", fail={"proxy": FORBIDDEN})
    assert env["status"] == "failed" and env["errors"][0]["code"] == "permission_denied"
    assert "I1104" not in env["errors"][0]["message"]


def test_login_unreachable_pod_fails_when_nothing_answered():
    env, _ = run("http-status", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env) == [("unreachable", "pod/jenkins-0")]


def test_url_prefix_and_port_and_scheme_change_the_url():
    env, calls = run(
        "http-status", inputs={"url_prefix": "/jenkins", "port": 8443, "scheme": "https"}
    )
    assert env["status"] == "ok"
    assert raw("jenkins-0", "/jenkins/login", "https", 8443, verbose=True) in calls


# ------------------------------------------------------------ queue ----


def test_queue_healthy_has_an_empty_queue_and_every_executor_online():
    env, calls = run("queue")
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["queue"]
    assert (out["stuckAfterSeconds"], out["stuckAfterCount"], out["offlineCount"]) == (1800, 0, 0)
    pod = pod_of(env, "queue")
    assert pod["queue"]["itemCount"] == 0 and pod["queue"]["stuckAfterItems"] == []
    assert (pod["computers"]["computerCount"], pod["computers"]["offlineCount"]) == (1, 0)
    assert raw("jenkins-0", QUEUE) in calls and raw("jenkins-0", COMPUTERS) in calls
    check_schema("queue", env)


def test_queue_broken_lists_the_item_waiting_longer_than_stuck_after_and_the_offline_node():
    env, _ = run("queue", "jenkins-stuck")
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["queue"]
    assert (out["stuckAfterCount"], out["offlineCount"]) == (1, 1)
    queue = pod_of(env, "queue")["queue"]
    assert (queue["itemCount"], queue["flaggedStuckCount"], queue["flaggedBlockedCount"]) == (
        2,
        1,
        1,
    )
    (item,) = queue["stuckAfterItems"]
    assert (item["id"], item["task"], item["stuck"], item["blocked"]) == (
        41,
        "nightly-build",
        True,
        False,
    )
    assert item["why"] == "There are no nodes with the label 'arm64'"
    assert 3 * 86400 - 60 < item["ageSeconds"] < 3 * 86400 + 60
    (node,) = pod_of(env, "queue")["computers"]["offline"]
    assert (node["name"], node["temporarilyOffline"], node["cause"]) == (
        "agent-arm64-1",
        True,
        "Disconnected by admin: node maintenance",
    )
    check_schema("queue", env)


def test_stuck_after_is_measured_against_the_run_time():
    # the blocked item has waited 60s at capture: listed once the threshold is below its age
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "30s"})
    assert env["outputs"]["queue"]["stuckAfterCount"] == 2
    assert [i["id"] for i in pod_of(env, "queue")["queue"]["stuckAfterItems"]] == [
        41,
        42,
    ]  # oldest first
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "5d"})
    assert env["outputs"]["queue"]["stuckAfterCount"] == 0
    # a later run time ages every item
    later = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    env, _ = run("queue", "jenkins-stuck", now=later)
    assert env["outputs"]["queue"]["stuckAfterCount"] == 2


def test_stuck_after_accepts_compound_durations_and_rejects_bad_ones():
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "1h30m"})
    assert env["outputs"]["queue"]["stuckAfterSeconds"] == 5400
    for bad in ("soon", "10", "-5m", ""):
        env, calls = run("queue", "jenkins-stuck", inputs={"stuck_after": bad})
        assert env["status"] == "failed" and env["errors"][0]["code"] == "invalid_input", bad
        assert calls == []


def test_the_why_text_is_cut_at_1024():
    doc = json.loads(load("queue-stuck.json"))
    doc["items"][0]["why"] = "w" * 3000
    env, _ = run("queue", "jenkins-stuck", replies={QUEUE: (0, json.dumps(doc), "")})
    item = pod_of(env, "queue")["queue"]["stuckAfterItems"][0]
    assert len(item["why"]) == 1024 and item["whyTruncated"] is True
    check_schema("queue", env)


def test_anonymous_read_off_skips_with_the_hint():
    env, _ = run("queue", "jenkins-locked")
    assert env["status"] == "skipped"
    assert env["reason"].endswith(
        "anonymous read is off; a Jenkins API token needs a generic connection (misc plugin, M4)"
    )
    assert env["errors"] == [] and env["outputs"] == {}


def test_one_endpoint_wanting_credentials_is_partial_and_the_other_stands():
    env, _ = run("queue", "jenkins-stuck", replies={COMPUTERS: (1, "", BACKEND_401)})
    assert env["status"] == "partial"
    assert codes(env) == [("permission_denied", "pod/jenkins-stuck-0/computers")]
    assert "misc plugin, M4" in env["errors"][0]["hint"]
    pod = pod_of(env, "queue")
    assert pod["computers"] is None and pod["queue"]["stuckAfterCount"] == 1
    check_schema("queue", env)


@pytest.mark.parametrize(
    "path, reply",
    [
        (QUEUE, (0, "<html>Please wait while Jenkins is getting ready</html>", "")),
        (QUEUE, (0, "[]", "")),
        (QUEUE, (0, '{"items": {"a": 1}}', "")),
        (QUEUE, (0, '{"items": [1]}', "")),
        (COMPUTERS, (0, "<html></html>", "")),
        (COMPUTERS, (0, '{"computer": "x"}', "")),
        (COMPUTERS, (0, '{"displayName": "Nodes"}', "")),
    ],
)
def test_an_unexpected_payload_is_an_error_never_an_empty_result(path, reply):
    env, _ = run("queue", replies={path: reply})
    section = "queue" if path == QUEUE else "computers"
    assert env["status"] == "partial"
    assert codes(env) == [("internal", f"pod/jenkins-0/{section}")]
    assert pod_of(env, "queue")[section] is None
    check_schema("queue", env)


def test_a_non_numeric_in_queue_since_is_an_error():
    doc = json.loads(load("queue-stuck.json"))
    doc["items"][0]["inQueueSince"] = "yesterday"
    env, _ = run("queue", "jenkins-stuck", replies={QUEUE: (0, json.dumps(doc), "")})
    assert codes(env) == [("internal", "pod/jenkins-stuck-0/queue")]
    assert pod_of(env, "queue")["queue"] is None


def test_an_item_without_in_queue_since_is_counted_not_aged():
    doc = {"items": [{"id": 1, "stuck": True, "why": "x"}]}
    env, _ = run("queue", replies={QUEUE: (0, json.dumps(doc), "")})
    queue = pod_of(env, "queue")["queue"]
    assert (queue["ageUnknownCount"], queue["flaggedStuckCount"], queue["stuckAfterItems"]) == (
        1,
        1,
        [],
    )


def test_a_503_from_the_json_api_is_a_dependency_error():
    env, _ = run("queue", replies={QUEUE: (1, "", UNAVAILABLE)})
    assert env["status"] == "partial"
    assert codes(env) == [("dependency", "pod/jenkins-0/queue")]
    assert pod_of(env, "queue")["computers"]["computerCount"] == 1


def test_queue_fails_when_nothing_answers_and_on_rbac_refusal():
    env, _ = run("queue", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and env["errors"][0]["code"] == "unreachable"
    assert env["errors"][0]["scope"] == "pod/jenkins-0/queue" and env["outputs"] == {}
    env, _ = run("queue", fail={"proxy": FORBIDDEN})
    assert env["status"] == "failed" and env["errors"][0]["code"] == "permission_denied"


# ------------------------------------------------------------ both tasks ----


@pytest.mark.parametrize("task", ["http-status", "queue"])
@pytest.mark.parametrize(
    "fail, code",
    [
        ({"workload": FORBIDDEN.replace("pods/proxy", "statefulsets")}, "permission_denied"),
        (
            {"workload": 'Error from server (NotFound): statefulsets.apps "x" not found'},
            "not_found",
        ),
        ({"workload": "Unable to connect to the server: dial tcp: i/o timeout"}, "unreachable"),
        ({"pods": FORBIDDEN.replace("pods/proxy", "pods")}, "permission_denied"),
    ],
)
def test_a_failed_primary_read_fails_the_task_with_its_code(task, fail, code):
    env, _ = run(task, fail=fail)
    assert (env["status"], env["errors"][0]["code"], env["outputs"]) == ("failed", code, {})


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_pods_that_are_not_running_are_listed_and_not_asked(task):
    text = load("pods_jenkins.txt").replace("status.phase=Running", "status.phase=Pending")
    env, calls = run(task, pods_text=text)
    assert env["status"] == "ok"
    assert pod_of(env, task)["queried"] is False and pod_of(env, task)["phase"] == "Pending"
    assert not [c for c in calls if "--raw" in c]


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_a_workload_without_pods_is_a_fact(task):
    env, _ = run(task, pods_text="")
    assert env["status"] == "ok" and env["outputs"][OUTPUT[task]]["pods"] == []


@pytest.mark.parametrize("task", ["http-status", "queue"])
@pytest.mark.parametrize(
    "inputs",
    [
        {"scheme": "ftp"},
        {"port": 0},
        {"port": 70000},
        {"url_prefix": "jenkins"},
        {"url_prefix": "/a/../b"},
        {"url_prefix": "/a?b=c"},
    ],
)
def test_bad_input_is_invalid_input_before_any_kubectl_call(task, inputs):
    env, calls = run(task, inputs=inputs)
    assert env["status"] == "failed" and env["errors"][0]["code"] == "invalid_input"
    assert calls == []


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_a_target_that_is_not_a_statefulset_or_deployment_is_invalid_input(task):
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "x",
        "type": "kubernetes/daemonset",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/daemonsets/x",
    }
    env, calls = run(task, res=res)
    assert env["errors"][0]["code"] == "invalid_input" and calls == []


def test_the_type_falls_back_to_the_plural_of_the_path():
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "jenkins",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/jenkins",
    }
    env, _ = run("http-status", res=res)
    assert env["status"] == "ok"


def test_max_items_cuts_the_pods_and_the_stuck_items():
    one = load("pods_jenkins-stuck.txt")
    two = one + one.replace("jenkins-stuck-0", "jenkins-stuck-1")
    env, _ = run("queue", "jenkins-stuck", pods_text=two, settings={"maxItems": 1})
    out = env["outputs"]["queue"]
    assert (out["podCount"], len(out["pods"]), out["truncated"]) == (2, 1, True)
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "30s"}, settings={"maxItems": 1})
    queue = pod_of(env, "queue")["queue"]
    assert (
        queue["stuckAfterCount"],
        len(queue["stuckAfterItems"]),
        queue["stuckAfterTruncated"],
    ) == (
        2,
        1,
        True,
    )


# ------------------------------------------------------------ lib ----


class Canned:
    def __init__(self, rc, out, err):
        self.result = type("R", (), {"returncode": rc, "stdout": out, "stderr": err})
        self.argv = None

    def run(self, argv, check=True, **_):
        assert check is False
        self.argv = argv
        return self.result


def test_proxy_get_tells_the_statuses_apart_with_and_without_header_logging():
    assert proxy.proxy_get(Canned(0, "x", ""), "/x") == proxy.Reply(200, "x")
    verbose = proxy.proxy_get(Canned(0, "x", load("login.stderr.txt")), "/x", headers=True)
    assert (verbose.status, verbose.version, verbose.headers_exposed) == (200, "2.462.3", True)
    ctx = Canned(0, "x", load("login.stderr.txt"))
    proxy.proxy_get(ctx, "/x", headers=True)
    assert ctx.argv[-1] == "-v=8"
    ctx = Canned(0, "x", "")
    proxy.proxy_get(ctx, "/x")
    assert "-v=8" not in ctx.argv
    # the real -v=8 dump of a backend 403 is a 403, not an internal error
    assert (
        proxy.proxy_get(Canned(1, "", load("queue-locked.stderr.txt")), "/x", headers=True).status
        == 403
    )
    assert proxy.proxy_get(Canned(1, "", NO_ROUTE), "/x").status == 404
    assert proxy.proxy_get(Canned(1, "", UNAVAILABLE), "/x").status == 503
    assert proxy.proxy_get(Canned(1, "", BACKEND_401), "/x").status == 401
    for stderr, code in (
        (FORBIDDEN, "permission_denied"),
        (UNREACHABLE, "unreachable"),
        ('Error from server (NotFound): pods "p" not found', "not_found"),
        (CLIENT_TIMEOUT, "timeout"),
        ("error: something odd", "internal"),
    ):
        with pytest.raises(RwError) as exc:
            proxy.proxy_get(Canned(1, "", stderr), "/x")
        assert exc.value.code == code, stderr


def test_an_error_from_a_verbose_call_carries_only_the_error_line():
    noisy = (
        NO_HEADERS
        + 'I1104 1 round_trippers.go:632] "Response" status="500" headers=<\n\tX: y\n >\n'
    )
    with pytest.raises(RwError) as exc:
        proxy.proxy_get(Canned(1, "", noisy + FORBIDDEN + "\n"), "/x", headers=True)
    assert exc.value.code == "permission_denied"
    assert "round_trippers" not in exc.value.message and "loader.go" not in exc.value.message


def test_the_x_jenkins_header_is_bounded_and_matched_by_name_only():
    big = '"Response" status="200 OK" headers=<\n\tX-Jenkins: ' + "9" * 200 + "\n >\n"
    reply = proxy.proxy_get(Canned(0, "x", big), "/x", headers=True)
    assert len(reply.version) == 64
    other = '"Response" status="200 OK" headers=<\n\tX-Jenkins-Session: abc\n >\n'
    assert proxy.proxy_get(Canned(0, "x", other), "/x", headers=True).version is None


def test_parse_duration():
    assert fetch.parse_duration("30m").total_seconds() == 1800
    assert fetch.parse_duration("1h30m").total_seconds() == 5400
    assert fetch.parse_duration("2d").total_seconds() == 172800
    assert fetch.parse_duration("500ms").total_seconds() == 0.5
    with pytest.raises(RwError):
        fetch.parse_duration("1w")


def test_pod_port_resolution_and_selector_string():
    pod = {
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "ports": [
                        {"name": "agent-listener", "containerPort": 50000},
                        {"name": "http", "containerPort": 9090},
                    ],
                }
            ]
        }
    }
    assert fetch.pod_port(pod) == 9090
    assert fetch.pod_port(pod, 8443) == 8443
    assert fetch.pod_port({}) == 8080
    assert fetch.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    with pytest.raises(RwError):
        fetch.selector_string({})


def test_read_queue_ignores_a_future_in_queue_since():
    items = [{"id": 1, "inQueueSince": 2_000_000_000_000}]
    out = build_queue.read_queue(items, CAPTURED, 60, 10)
    assert out["stuckAfterCount"] == 0 and out["itemCount"] == 1


def test_the_projections_never_ask_for_env():
    for projection in (project.WORKLOAD_FIELDS, project.POD_FIELDS):
        for many in (True, False):
            assert "env" not in projection.template(many)
    for path in DATA.iterdir():
        text = path.read_text().lower()
        assert '"env"' not in text and '"value"' not in text and "authorization" not in text


def test_the_queue_calls_ask_for_a_narrow_tree_without_build_parameters():
    assert "params" not in build_queue.QUEUE_TREE and "actions" not in build_queue.QUEUE_TREE
    assert build_queue.QUEUE_TREE.startswith("?tree=items[")


# ------------------------------------------------------------ deadline ----


class Clocked:
    """A ctx whose `kubectl get --raw` calls each take `step` seconds of a fake clock."""

    def __init__(self, answer, step=30):
        self.now, self.step, self.answer = 0.0, step, answer
        self.calls, self.errors = [], []

    def clock(self):
        return self.now

    def run(self, argv, check=True, **_):
        self.calls.append(list(argv))
        rc, out, err = self.answer(len(self.calls) - 1, argv)
        self.now += self.step
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})

    def error(self, code, message, scope=None, **_):
        self.errors.append((code, scope, message))

    def skip(self, reason=""):
        raise AssertionError("not expected")


WORKLOAD = {
    "kind": "StatefulSet",
    "metadata": {"name": "j", "namespace": NS},
    "spec": {"replicas": 6},
    "status": {},
}


def pods(n):
    return [
        {"metadata": {"name": f"j-{i}"}, "status": {"phase": "Running"}, "spec": {}}
        for i in range(n)
    ]


def test_the_task_deadline_leaves_the_remaining_pods_a_timeout_error():
    ctx = Clocked(lambda i, argv: (0, load("login.html"), ""))
    out = http_status.view(ctx, WORKLOAD, pods(6), "http", None, 200, fetch.Budget(clock=ctx.clock))
    # four asks at 0s, 30s, 60s, 90s (the fourth limited to the 10s left)
    assert out["okCount"] == 4
    assert [c[4] for c in ctx.calls] == [f"--request-timeout={t}s" for t in (30, 30, 30, 10)]
    assert [(c, s) for c, s, _ in ctx.errors] == [("timeout", "pod/j-4"), ("timeout", "pod/j-5")]
    assert "100s deadline" in ctx.errors[0][2]


def test_three_timeouts_in_a_row_stop_asking_the_path():
    ctx = Clocked(
        lambda i, argv: (0, load("login.html"), "") if i == 0 else (1, "", CLIENT_TIMEOUT), step=1
    )
    http_status.view(ctx, WORKLOAD, pods(6), "http", None, 200, fetch.Budget(clock=ctx.clock))
    assert len(ctx.calls) == 4 and [c for c, _, _ in ctx.errors] == ["timeout"] * 5
    assert "3 asks in a row timed out" in ctx.errors[3][2]


# ------------------------------------------------------------ permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "deployments.apps": ("apps", "deployments"),
    "pods": ("", "pods"),
}
VALUE_FLAGS = {"-n", "-o", "-l"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs. Raises on anything it
    does not know, so a script that issues a new call has to extend this."""
    if "--raw" in argv:
        path = argv[argv.index("--raw") + 1]
        assert "/pods/" in path and "/proxy/" in path, path
        return {("", "pods/proxy", "get")}
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    if positional[0] != "get":
        raise AssertionError(f"no RBAC mapping for kubectl {positional[0]}: {argv}")
    group, plural = RESOURCES[positional[1]]
    named_object = len(positional) > 2 and "-l" not in flags
    return {(group, plural, "get" if named_object else "list")}


def granted(task):
    return {
        (g, r, v)
        for rule in TASKS[task]["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


@pytest.mark.parametrize("task", list(SCRIPTS))
def test_declared_permissions_are_exactly_what_the_scripts_call(task):
    needed = set()
    for name, kind in (("jenkins-stuck", "statefulset"), ("jenkins", "deployment")):
        env, calls = run(task, name, kind=kind)
        assert env["status"] == "ok"
        for argv in calls:
            needed |= rbac_needs(list(argv))
    assert needed == granted(task), (
        f"{task}: calls {sorted(needed)}, declares {sorted(granted(task))}"
    )


def test_rbac_needs_maps_the_verbs():
    tail = ["-o", "jsonpath=x"]
    assert rbac_needs(["kubectl", "-n", "x", "get", "statefulsets.apps", "j", *tail]) == {
        ("apps", "statefulsets", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", *tail]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(
        ["kubectl", "get", "--raw", "/api/v1/namespaces/n/pods/http:p:80/proxy/x"]
    ) == {("", "pods/proxy", "get")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "exec", "p"])


def test_both_tasks_only_read_and_send_no_credential():
    assert all(t["readOnly"] is True for t in TASKS.values())
    assert all(v in ("get", "list") for t in TASKS for (_, _, v) in granted(t))
    for task in SCRIPTS:
        _, calls = run(task, "jenkins-stuck")
        assert all(c[1] in ("get", "-n") for c in calls)
        argv_words = {a for c in calls for a in c}
        assert not argv_words & {"exec", "port-forward", "--user", "-u", "--token"}
        assert not any("secret" in a.lower() or "token" in a.lower() for a in argv_words)
