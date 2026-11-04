"""Unit tests for k8s-jenkins-health's lib/ and scripts. The captured data comes from the
`rw-jenkins` namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-jenkins-health.yaml): `jenkins` (healthy), `jenkins-stuck` (a queue item waiting for days, a
blocked one, an offline node), `jenkins-locked` (anonymous read off: the JSON API answers 403),
`jenkins-prefix` (served under /jenkins, its readiness probe asks /jenkins/login) and
`jenkins-down` (still starting: 503). All are small python stubs serving the documented remote API,
not a real Jenkins. The
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
from k8s_jenkins_health import build_queue, fetch, http_status, k8s, project, proxy
from k8s_jenkins_health.errors import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-jenkins-health"
DATA = Path(__file__).parent / "data" / "k8s_jenkins_health"
UTC = timezone.utc
NS = "rw-jenkins"
CAPTURED = datetime(2026, 11, 4, 0, 30, 32, tzinfo=UTC)  # just after the queue data was captured
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
ERROR_LINE = 'Error from server (NotFound): pods "p" not found'


def load(name):
    return (DATA / name).read_text()


def script(task):
    spec = importlib.util.spec_from_file_location(
        f"jenkins_{task}", SKILL / "scripts" / f"{SCRIPTS[task]}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def raw(pod, path, scheme="http", port=8080):
    return (
        "kubectl",
        "get",
        "--raw",
        f"/api/v1/namespaces/{NS}/pods/{scheme}:{pod}:{port}/proxy{path}",
        "--request-timeout=30s",
    )


QUEUE = build_queue.QUEUE + build_queue.QUEUE_TREE
COMPUTERS = build_queue.COMPUTERS + build_queue.COMPUTER_TREE
LOGIN = "/login"
# what the readiness probes of the captured pods say (python stubs): the prefix a run derives
DERIVED_PREFIX = {"jenkins-prefix": "/jenkins"}
REPLIES = {
    "jenkins": {
        LOGIN: (0, load("login.html"), ""),
        QUEUE: (0, load("queue-empty.json"), ""),
        COMPUTERS: (0, load("computers-healthy.json"), ""),
    },
    "jenkins-stuck": {
        LOGIN: (0, load("login.html"), ""),
        QUEUE: (0, load("queue-stuck.json"), ""),
        COMPUTERS: (0, load("computers-stuck.json"), ""),
    },
    "jenkins-locked": {
        LOGIN: (0, load("login.html"), ""),
        QUEUE: (1, "", load("queue-locked.stderr.txt")),
        COMPUTERS: (1, "", load("queue-locked.stderr.txt")),
    },
    "jenkins-prefix": {
        LOGIN: (0, load("login.html"), ""),
        QUEUE: (0, load("queue-empty.json"), ""),
        COMPUTERS: (0, load("computers-healthy.json"), ""),
    },
    "jenkins-down": {
        LOGIN: (1, "", UNAVAILABLE),
        QUEUE: (1, "", UNAVAILABLE),
        COMPUTERS: (1, "", UNAVAILABLE),
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
    prefix = inputs.get("url_prefix", DERIVED_PREFIX.get(name, ""))
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
            argv = raw(pod, prefix + path, scheme, port)
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


def test_login_healthy_is_ok_with_the_version_from_the_page_footer():
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
    assert (pod["httpStatus"], pod["ok"], pod["version"], pod["versionSource"]) == (
        200,
        True,
        "2.452.3",
        "login-footer",
    )
    assert (pod["ssoRedirect"], pod["hint"]) == (None, None)
    assert "headersExposed" not in pod
    assert pod["port"] == 8080 and pod["podReady"] is True
    assert (pod["scheme"], pod["urlPrefix"]) == ("http", "")
    assert raw("jenkins-0", "/login") in calls
    check_schema("http-status", env)


def test_login_on_the_stuck_and_locked_controllers_is_still_ok():
    for name in ("jenkins-stuck", "jenkins-locked"):
        env, _ = run("http-status", name)
        assert env["status"] == "ok" and pod_of(env, "http-status")["ok"] is True


def test_no_call_asks_kubectl_for_verbose_output_or_headers():
    for task in SCRIPTS:
        _, calls = run(task)
        assert not [c for c in calls if any(a.startswith("-v") for a in c)]
    source = (SKILL / "lib" / "k8s_jenkins_health" / "proxy.py").read_text()
    assert "-v=8" not in source and "headers=True" not in source
    assert "headersExposed" not in (SKILL / "schemas" / "login.v1.json").read_text()
    assert "headersExposed" not in (SKILL / "tasks.yaml").read_text()
    assert "headersExposed" not in (SKILL / "SKILL.md").read_text()


FOOTER_PAGE = "<html><body>Sign in <div>Jenkins {}</div></body></html>"


@pytest.mark.parametrize(
    "version", ["2.452.3", "2.462", "2.440.1"], ids=["three-part", "two-part", "lts"]
)
def test_the_version_is_the_footers(version):
    page = FOOTER_PAGE.format(version)
    env, _ = run("http-status", replies={"/login": (0, page, "")})
    pod = pod_of(env, "http-status")
    assert (pod["ok"], pod["version"], pod["versionSource"]) == (True, version, "login-footer")


def test_the_bare_word_jenkins_is_not_the_identity_of_a_login_page():
    # an SSO page that says "Jenkins" is not a Jenkins page: no version marker, no ok
    page = "<html><body><h1>Sign in to Jenkins</h1><input type=password></body></html>"
    env, _ = run("http-status", replies={"/login": (0, page, "")})
    assert env["status"] == "ok" and env["errors"] == []
    pod = pod_of(env, "http-status")
    assert (pod["httpStatus"], pod["ok"], pod["ssoRedirect"]) == (200, None, "likely")
    assert "SSO realm" in pod["hint"] and "limited" in pod["hint"]
    out = env["outputs"]["login"]
    assert (out["okCount"], out["notOkCount"]) == (0, 0)
    check_schema("http-status", env)


@pytest.mark.parametrize(
    "page",
    [
        '<form><input type="password" name="p"></form>',
        "<html>Log in with your corporate account</html>",
        "<html><title>Okta</title>Sign-in required</html>",
        "<html>SAML request sent</html>",
        "<html>OpenID Connect redirect</html>",
    ],
)
def test_a_login_page_without_the_jenkins_footer_is_an_sso_redirect(page):
    # the stub's python image tag would be read as a version: use a tag that is not one
    text = load("pods_jenkins.txt").replace("python:3.12-alpine", "jenkins/jenkins:lts")
    env, _ = run("http-status", pods_text=text, replies={"/login": (0, page, "")})
    pod = pod_of(env, "http-status")
    assert (pod["ok"], pod["ssoRedirect"]) == (None, "likely")
    assert pod["version"] is None and pod["versionSource"] is None


def test_the_sso_page_still_gets_the_image_tag_version():
    page = "<html>Sign in with SSO</html>"
    text = load("pods_jenkins.txt").replace(
        "python:3.12-alpine", "jenkins/jenkins:2.452.3-lts-jdk17"
    )
    env, _ = run("http-status", pods_text=text, replies={"/login": (0, page, "")})
    pod = pod_of(env, "http-status")
    assert (pod["ssoRedirect"], pod["version"], pod["versionSource"]) == (
        "likely",
        "2.452.3",
        "image-tag",
    )


def test_the_version_falls_back_to_the_image_tag_when_the_page_gives_none():
    text = load("pods_jenkins-down.txt").replace(
        "python:3.12-alpine", "registry.example/jenkins/jenkins:2.452.3-lts-jdk17"
    )
    env, _ = run("http-status", "jenkins-down", pods_text=text)
    pod = pod_of(env, "http-status")
    assert (pod["httpStatus"], pod["ok"], pod["version"], pod["versionSource"]) == (
        503,
        False,
        "2.452.3",
        "image-tag",
    )
    check_schema("http-status", env)


@pytest.mark.parametrize(
    "image, version",
    [
        ("jenkins/jenkins:2.452.3-lts-jdk17", "2.452.3"),
        ("jenkins/jenkins:2.452.3", "2.452.3"),
        ("jenkins/jenkins:2.462-jdk21", "2.462"),
        ("registry:5000/jenkins/jenkins:2.452.3@sha256:abc", "2.452.3"),
        ("jenkins/jenkins:lts", None),
        ("jenkins/jenkins:lts-jdk17", None),
        ("jenkins/jenkins:latest", None),
        ("jenkins/jenkins", None),
        ("registry:5000/jenkins/jenkins", None),
        ("jenkins/jenkins@sha256:abcdef", None),
        ("", None),
    ],
)
def test_image_version_reads_a_version_tag_only(image, version):
    pod = {
        "spec": {
            "containers": [
                {"name": "c", "image": image, "ports": [{"name": "http", "containerPort": 8080}]}
            ]
        }
    }
    assert fetch.image_version(pod) == version


def test_the_image_tag_is_the_jenkins_containers_not_a_sidecars():
    pod = {
        "spec": {
            "containers": [
                {"name": "config-reload", "image": "kiwigrid/k8s-sidecar:1.27.4"},
                {
                    "name": "jenkins",
                    "image": "jenkins/jenkins:2.452.3-jdk17",
                    "ports": [{"name": "http", "containerPort": 8080}],
                },
            ]
        }
    }
    assert fetch.image_version(pod) == "2.452.3"
    assert fetch.image_version({"spec": {"containers": [{"name": "x", "image": "a:1.2"}]}}) is None


def test_a_200_that_is_not_jenkins_is_an_error_never_ok():
    page = "<html><body>Welcome to nginx!</body></html>"
    env, _ = run("http-status", replies={"/login": (0, page, "")})
    assert env["status"] == "failed"  # the only pod gave nothing usable
    assert codes(env) == [("internal", "pod/jenkins-0")]
    assert env["outputs"] == {}
    # the word alone, with no login page and no version, is not Jenkins either
    env, _ = run("http-status", replies={"/login": (0, "<html>Jenkins is great</html>", "")})
    assert codes(env) == [("internal", "pod/jenkins-0")]
    # one good pod keeps the result partial
    two = load("pods_jenkins.txt") + load("pods_jenkins.txt").replace("jenkins-0", "jenkins-1")
    env, _ = run("http-status", pods_text=two, replies={("jenkins-1", "/login"): (0, page, "")})
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


def test_login_unreachable_pod_fails_when_nothing_answered():
    env, _ = run("http-status", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env) == [("unreachable", "pod/jenkins-0")]


def test_url_prefix_and_port_and_scheme_change_the_url():
    env, calls = run(
        "http-status", inputs={"url_prefix": "/jenkins", "port": 8443, "scheme": "https"}
    )
    assert env["status"] == "ok"
    assert raw("jenkins-0", "/jenkins/login", "https", 8443) in calls
    pod = pod_of(env, "http-status")
    assert (pod["port"], pod["scheme"], pod["urlPrefix"]) == (8443, "https", "/jenkins")


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_the_prefix_comes_from_the_readiness_probe_when_no_input_is_given(task):
    env, calls = run(task, "jenkins-prefix")
    assert env["status"] == "ok" and env["errors"] == []
    pod = pod_of(env, task)
    assert (pod["urlPrefix"], pod["scheme"], pod["port"]) == ("/jenkins", "http", 8080)
    path = LOGIN if task == "http-status" else QUEUE
    assert raw("jenkins-prefix-0", "/jenkins" + path) in calls
    assert not [c for c in calls if "proxy/login" in c[3] or "proxy/queue" in c[3]]
    check_schema(task, env)


def test_an_empty_prefix_input_overrides_the_probe():
    env, calls = run("http-status", "jenkins-prefix", inputs={"url_prefix": ""})
    assert pod_of(env, "http-status")["urlPrefix"] == ""
    assert raw("jenkins-prefix-0", "/login") in calls


def with_probe(path="/login", port="http", scheme="HTTP", ports=None):
    """The captured jenkins pod with its readiness probe (and ports) replaced."""
    ports = ports or '[{"containerPort":8080,"name":"http","protocol":"TCP"}]'
    lines = []
    for line in load("pods_jenkins.txt").splitlines():
        if line.startswith("p:jenkins="):
            line = f"p:jenkins={path}|{port}|{scheme}"
        elif line.startswith("k:jenkins="):
            line = f"k:jenkins={ports}"
        lines.append(line)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "probe, endpoint",
    [
        ({"path": "/login"}, (8080, "http", "")),
        ({"path": "/jenkins/login", "port": "http"}, (8080, "http", "/jenkins")),
        ({"path": "/ci/jenkins/login"}, (8080, "http", "/ci/jenkins")),
        (
            {"path": "/jenkins/login", "scheme": "HTTPS", "port": "8443"},
            (8443, "https", "/jenkins"),
        ),
        (
            {"path": "/login", "port": "web", "ports": '[{"containerPort":9090,"name":"web"}]'},
            (9090, "http", ""),
        ),
        ({"path": "/jenkins/-/ready"}, (8080, "http", "")),
        ({"path": "/bad prefix/login"}, (8080, "http", "")),
        ({"path": "/a/../login"}, (8080, "http", "")),
        ({"path": "/login", "port": "nosuch"}, (8080, "http", "")),
    ],
)
def test_the_endpoint_is_derived_from_the_probe(probe, endpoint):
    pods = project.POD_FIELDS.parse(with_probe(**probe), True)["items"]
    assert tuple(fetch.pod_endpoint(pods[0])) == endpoint


def test_inputs_win_over_the_probe_one_by_one():
    pods = project.POD_FIELDS.parse(
        with_probe(path="/jenkins/login", scheme="HTTPS", port="8443"), True
    )["items"]
    pod = pods[0]
    assert tuple(fetch.pod_endpoint(pod)) == (8443, "https", "/jenkins")
    assert tuple(fetch.pod_endpoint(pod, port=9000)) == (9000, "https", "/jenkins")
    assert tuple(fetch.pod_endpoint(pod, scheme="http")) == (8443, "http", "/jenkins")
    assert tuple(fetch.pod_endpoint(pod, prefix="/other")) == (8443, "https", "/other")
    assert tuple(fetch.pod_endpoint(pod, prefix="")) == (8443, "https", "")


def test_a_pod_without_a_probe_uses_the_port_name_and_the_defaults():
    ports = [{"name": "http", "containerPort": 9090}]
    pod = {"spec": {"containers": [{"name": "c", "ports": ports}]}}
    assert tuple(fetch.pod_endpoint(pod)) == (9090, "http", "")
    assert tuple(fetch.pod_endpoint({})) == (8080, "http", "")


def test_the_probe_projection_carries_path_port_and_scheme_only():
    template = project.POD_FIELDS.template(True)
    assert ".readinessProbe.httpGet.path" in template
    assert ".readinessProbe.httpGet.port" in template
    assert "httpHeaders" not in template and ".readinessProbe}" not in template
    assert "env" not in template
    pods = project.POD_FIELDS.parse(load("pods_jenkins-prefix.txt"), True)["items"]
    container = pods[0]["spec"]["containers"][0]
    assert container["readinessProbe"] == {
        "httpGet": {"path": "/jenkins/login", "port": "http", "scheme": "HTTP"}
    }
    assert container["image"] == "python:3.12-alpine"


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
    assert item["taskUrl"] == "http://jenkins:8080/job/nightly-build/"
    assert item["why"] == "There are no nodes with the label 'arm64'"
    assert queue["discoverableCount"] == 0
    assert 3 * 86400 - 60 < item["ageSeconds"] < 3 * 86400 + 60
    (node,) = pod_of(env, "queue")["computers"]["offline"]
    assert (node["name"], node["temporarilyOffline"], node["cause"]) == (
        "agent-arm64-1",
        True,
        "Disconnected by admin: node maintenance",
    )
    check_schema("queue", env)


def test_stuck_after_is_measured_against_the_run_time():
    # the blocked item has waited 60s at capture: listed once the threshold is below its age; the
    # item Jenkins flags stuck is listed whatever the threshold, and first
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "30s"})
    assert env["outputs"]["queue"]["stuckAfterCount"] == 2
    items = pod_of(env, "queue")["queue"]["stuckAfterItems"]
    assert [i["id"] for i in items] == [41, 42]
    assert items[1]["taskUrl"] == "http://jenkins:8080/job/deploy/"
    assert items[1]["why"] == "Build #17 is already in progress (ETA: 2 min 0 sec)"
    env, _ = run("queue", "jenkins-stuck", inputs={"stuck_after": "5d"})
    assert env["outputs"]["queue"]["stuckAfterCount"] == 1
    assert [i["id"] for i in pod_of(env, "queue")["queue"]["stuckAfterItems"]] == [41]
    # a later run time ages the blocked item past the default 30m
    later = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    env, _ = run("queue", "jenkins-stuck", now=later)
    assert env["outputs"]["queue"]["stuckAfterCount"] == 2


def queue_doc(*items, **extra):
    return json.dumps({"items": list(items), **extra})


def waiting(id, age_s, now=CAPTURED, **flags):
    since = int(now.timestamp() * 1000 - age_s * 1000)
    return {
        "id": id,
        "inQueueSince": since,
        "why": f"why {id}",
        "task": {"name": f"job-{id}"},
    } | flags


def listed(items, **inputs):
    env, _ = run("queue", replies={QUEUE: (0, queue_doc(*items), "")}, inputs=inputs)
    queue = pod_of(env, "queue")["queue"]
    return [i["id"] for i in queue["stuckAfterItems"]], queue, env


def test_an_item_flagged_stuck_is_listed_whatever_its_age():
    ids, queue, env = listed([waiting(1, 5, stuck=True)])  # five seconds old, stuckAfter 30m
    assert ids == [1] and queue["stuckAfterCount"] == 1 and queue["flaggedStuckCount"] == 1
    check_schema("queue", env)


def test_blocked_and_buildable_items_are_listed_only_once_older_than_stuck_after():
    ids, queue, _ = listed(
        [
            waiting(1, 60, blocked=True),  # young blocked
            waiting(2, 3600, blocked=True),  # old blocked
            waiting(3, 3600, buildable=True),  # old buildable
            waiting(4, 60, buildable=True),  # young buildable
            waiting(5, 3600),  # old but neither flag: a waiting (quiet period) item
            waiting(6, 3600, blocked=False, buildable=False, pending=True),
        ]
    )
    assert ids == [2, 3] and queue["itemCount"] == 6  # oldest first; equal ages keep their order
    assert queue["flaggedBlockedCount"] == 2


def test_flagged_stuck_items_come_first_then_oldest_first():
    ids, _, _ = listed(
        [
            waiting(1, 7200, buildable=True),
            waiting(2, 100, stuck=True),
            waiting(3, 9000, blocked=True),
            waiting(4, 5000, stuck=True),
        ]
    )
    assert ids == [4, 2, 3, 1]


def test_a_flagged_stuck_item_without_in_queue_since_is_listed_with_no_age():
    items = [
        {"id": 1, "stuck": True, "why": "x", "task": {"name": "j"}},
        waiting(2, 10, stuck=True),
    ]
    ids, queue, env = listed(items)
    assert ids == [2, 1]  # the one with an age first
    row = queue["stuckAfterItems"][1]
    assert (row["ageSeconds"], row["inQueueSince"]) == (None, None)
    assert queue["ageUnknownCount"] == 1
    check_schema("queue", env)


def test_the_listed_items_are_capped_at_max_items_flagged_ones_first():
    items = [waiting(i, 100 + i, stuck=True) for i in range(5)] + [waiting(9, 99999, blocked=True)]
    env, _ = run("queue", replies={QUEUE: (0, queue_doc(*items), "")}, settings={"maxItems": 3})
    queue = pod_of(env, "queue")["queue"]
    assert [i["id"] for i in queue["stuckAfterItems"]] == [4, 3, 2]
    assert (queue["stuckAfterCount"], queue["stuckAfterTruncated"]) == (6, True)


def test_each_listed_item_carries_its_task_url_and_why():
    doc = json.loads(load("queue-stuck.json"))
    doc["items"][0]["task"]["url"] = "http://j/job/" + "u" * 3000
    doc["items"][0]["why"] = "w" * 3000
    env, _ = run("queue", "jenkins-stuck", replies={QUEUE: (0, json.dumps(doc), "")})
    item = pod_of(env, "queue")["queue"]["stuckAfterItems"][0]
    assert len(item["taskUrl"]) == 1024 and len(item["why"]) == 1024
    assert item["whyTruncated"] is True
    check_schema("queue", env)


def test_names_and_ids_are_capped_at_1024():
    doc = json.loads(load("queue-stuck.json"))
    doc["items"][0]["id"] = "i" * 3000
    doc["items"][0]["task"]["name"] = "t" * 3000
    nodes = json.loads(load("computers-stuck.json"))
    for node in nodes["computer"]:
        if node["offline"]:
            node["displayName"] = "n" * 3000
    env, _ = run(
        "queue",
        "jenkins-stuck",
        replies={QUEUE: (0, json.dumps(doc), ""), COMPUTERS: (0, json.dumps(nodes), "")},
    )
    pod = pod_of(env, "queue")
    item = pod["queue"]["stuckAfterItems"][0]
    assert (len(item["id"]), len(item["task"])) == (1024, 1024)
    assert len(pod["computers"]["offline"][0]["name"]) == 1024
    check_schema("queue", env)
    # a number stays a number
    ids, _, _ = listed([waiting(41, 5, stuck=True)])
    assert ids == [41]


def test_discoverable_items_are_counted():
    for doc, count in (
        (queue_doc(discoverableItems=[{"id": 7}, {"id": 8}]), 2),
        (queue_doc(discoverableItems=[]), 0),
        (queue_doc(), None),  # an older Jenkins, or a document without the list
    ):
        env, _ = run("queue", replies={QUEUE: (0, doc, "")})
        assert pod_of(env, "queue")["queue"]["discoverableCount"] == count
        check_schema("queue", env)
    env, _ = run("queue", replies={QUEUE: (0, queue_doc(discoverableItems={"a": 1}), "")})
    assert env["status"] == "partial" and codes(env) == [("internal", "pod/jenkins-0/queue")]


def test_the_queue_tree_asks_for_discoverable_items_and_the_task_url():
    assert "discoverableItems[id]" in build_queue.QUEUE_TREE
    assert "task[name,url]" in build_queue.QUEUE_TREE


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


def test_an_unflagged_item_without_in_queue_since_is_counted_not_listed():
    doc = {"items": [{"id": 1, "blocked": True, "why": "x"}]}
    env, _ = run("queue", replies={QUEUE: (0, json.dumps(doc), "")})
    queue = pod_of(env, "queue")["queue"]
    assert (queue["ageUnknownCount"], queue["flaggedBlockedCount"], queue["stuckAfterItems"]) == (
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
    # the workload wants a replica and none is Running: partial, with the error that says so
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("unreachable", "pods")]
    assert "1 replica(s) but no pod is Running" in env["errors"][0]["message"]
    assert pod_of(env, task)["queried"] is False and pod_of(env, task)["phase"] == "Pending"
    assert not [c for c in calls if "--raw" in c]


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_no_running_pod_is_only_an_error_when_the_workload_wants_replicas(task):
    text = load("pods_jenkins.txt").replace("status.phase=Running", "status.phase=Pending")
    zero = load("workload_jenkins.txt").replace("j:spec.replicas=1", "j:spec.replicas=0")
    assert zero != load("workload_jenkins.txt")
    resource = {
        "cluster": "kind",
        "namespace": NS,
        "name": "jenkins",
        "type": "kubernetes/statefulset",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/jenkins",
    }
    with make_context(resource=resource, settings=SETTINGS, now=CAPTURED) as tc:
        table = stubs(pods_text=text)
        table[next(a for a in table if "statefulsets.apps" in a)] = (0, zero, "")
        stub_commands(tc, table)
        env = run_main(script(task), tc)
    assert env["status"] == "ok" and env["errors"] == []


@pytest.mark.parametrize("task", ["http-status", "queue"])
def test_a_workload_without_pods_is_a_fact(task):
    env, _ = run(task, pods_text="")
    assert env["status"] == "partial"  # replicas 1 and no pod: see the test above
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("unreachable", "pods")]
    assert env["outputs"][OUTPUT[task]]["pods"] == []


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


def test_proxy_get_tells_the_statuses_apart():
    assert proxy.proxy_get(Canned(0, "x", ""), "/x") == proxy.Reply(200, "x")
    ctx = Canned(0, "x", "")
    proxy.proxy_get(ctx, "/x")
    assert ctx.argv[-1].startswith("--request-timeout=") and "-v=8" not in ctx.argv
    # the recorded backend 403 is a 403, in its plain one-line form
    assert proxy.proxy_get(Canned(1, "", load("queue-locked.stderr.txt")), "/x").status == 403
    assert proxy.proxy_get(Canned(1, "", NO_ROUTE), "/x").status == 404
    assert proxy.proxy_get(Canned(1, "", UNAVAILABLE), "/x").status == 503
    assert proxy.proxy_get(Canned(1, "", BACKEND_401), "/x").status == 401
    for stderr, code in (
        (FORBIDDEN, "permission_denied"),
        (UNREACHABLE, "unreachable"),
        (ERROR_LINE, "not_found"),
        (CLIENT_TIMEOUT, "timeout"),
        ("error: something odd", "internal"),
    ):
        with pytest.raises(RwError) as exc:
            proxy.proxy_get(Canned(1, "", stderr), "/x")
        assert exc.value.code == code, stderr


SPOOF_RBAC = (
    'Error from server (Forbidden): <html>pods "x" is forbidden: User "u" cannot get resource '
    '"pods/proxy"</html>'
)


def test_a_rbac_denial_is_told_by_the_anchored_wording_not_by_a_backends_body():
    with pytest.raises(RwError) as exc:
        proxy.proxy_get(Canned(1, "", FORBIDDEN), "/x")
    assert exc.value.code == "permission_denied"
    # a backend 403 whose body merely quotes the wording is the backend wanting credentials
    assert proxy.proxy_get(Canned(1, "", SPOOF_RBAC), "/x").status == 403
    body = "Error from server (Forbidden): sorry, cannot get resource: is forbidden: User nobody"
    assert proxy.proxy_get(Canned(1, "", body), "/x").status == 403


def test_other_failures_are_mapped_from_the_error_from_server_reason():
    def code(stderr, rc=1):
        with pytest.raises(RwError) as exc:
            proxy.proxy_get(Canned(rc, "", stderr), "/x")
        return exc.value.code

    assert code("Error from server (Timeout): request did not complete within 30s") == "timeout"
    assert code("Error from server (TooManyRequests): slow down") == "rate_limited"
    assert code("Error from server (InternalError): oops") == "internal"
    assert code("Error from server (SomethingNew): x") == "internal"
    assert code("Error from server (ServiceUnavailable): something else") == "unreachable"
    # the backend's body text never decides: these mention other classes' wording
    assert (
        code("Error from server (InternalError): x509 connection refused not found") == "internal"
    )
    assert code("anything", rc=124) == "timeout" and code("anything", rc=127) == "dependency"
    # a NotFound that is not the route's own message is a missing object
    assert (
        code(
            "Error from server (NotFound): the server could not find the requested resource (get pods p)"
        )
        == "not_found"
    )


def test_a_failure_shows_only_the_first_line_of_a_backend_body_cut_at_256():
    body = "Error from server (Forbidden): " + "x" * 400 + "\n<html>\nsecret page\n</html>"
    err = k8s.failure(["kubectl", "get"], type("R", (), {"returncode": 1, "stderr": body}))
    assert len(err.message) == len("kubectl exited 1: ") + 256 and "secret" not in err.message
    warned = 'Warning: deprecated\nError from server (NotFound): pods "p" not found\nmore'
    err = k8s.failure(["kubectl", "get"], type("R", (), {"returncode": 1, "stderr": warned}))
    assert err.message == 'kubectl exited 1: Error from server (NotFound): pods "p" not found'
    assert err.code == "not_found"


def test_a_401_line_is_a_credential_failure_for_the_api_and_the_backends_401_on_a_proxy_call():
    assert k8s.classify(1, BACKEND_401) == "permission_denied"
    assert proxy.proxy_get(Canned(1, "", BACKEND_401), "/x").status == 401
    unauthorized = "Error from server (Unauthorized): Unauthorized"
    assert proxy.proxy_get(Canned(1, "", unauthorized), "/x").status == 401


def test_the_redirect_gap_is_marked_in_proxy_py():
    assert "redirect-token test pending" in (SKILL / "lib/k8s_jenkins_health/proxy.py").read_text()


def test_parse_duration():
    assert fetch.parse_duration("30m").total_seconds() == 1800
    assert fetch.parse_duration("1h30m").total_seconds() == 5400
    assert fetch.parse_duration("2d").total_seconds() == 172800
    assert fetch.parse_duration("500ms").total_seconds() == 0.5
    with pytest.raises(RwError):
        fetch.parse_duration("1w")


def test_selector_string():
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
    assert build_queue.QUEUE_TREE.startswith("?tree=discoverableItems[id],items[")


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
