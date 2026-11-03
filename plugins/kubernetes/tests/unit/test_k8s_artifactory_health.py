"""Unit tests for k8s-artifactory-health's lib/ and script. The captured data comes from the
`rw-artifactory` namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-artifactory-health.yaml): `artifactory`, a healthy StatefulSet, and `artifactory-broken`,
whose readiness answers 503 and whose router lists a service UNHEALTHY. Both are nginx stubs
serving the documented endpoints, not a real Artifactory. The workload, pod and Service data are
the narrow `-o jsonpath` projections the script asks for (no env, no pod template). The script runs
through the SDK's test context (`ctx.run` raises on a non-zero exit like the real runner;
`stub_commands` matches the exact kubectl argv, so an unexpected call fails)."""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_artifactory_health import fetch, health, project, proxy
from k8s_artifactory_health.errors import RwError
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-artifactory-health"
DATA = Path(__file__).parent / "data" / "k8s_artifactory_health"
NS = "rw-artifactory"
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
SETTINGS = {"maxItems": 200}
FORBIDDEN = (
    'Error from server (Forbidden): {r} "x" is forbidden: User "u" cannot get resource "{r}" '
    'in API group "" in the namespace "rw-artifactory"'
)
UNREACHABLE = (
    "Error from server (ServiceUnavailable): error trying to reach service: dial tcp "
    "10.244.0.5:8081: connect: connection refused"
)
UNAVAILABLE = (
    "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
)
NO_ENDPOINTS = 'Error from server (ServiceUnavailable): no endpoints available for service "x"'
NO_ROUTE = "Error from server (NotFound): the server could not find the requested resource"
BACKEND_403 = "Error from server (Forbidden): <html><body>Authentication required</body></html>"
BACKEND_401 = (
    "error: You must be logged in to the server "
    "(the server has asked for the client to provide credentials)"
)
CLIENT_TIMEOUT = (
    "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)"
)
LIVENESS = "/artifactory/api/v1/system/liveness"
READINESS = "/artifactory/api/v1/system/readiness"
ROUTER = "/router/api/v1/system/health"
KINDS = {"statefulset": "StatefulSet", "deployment": "Deployment"}
NAMES = {LIVENESS: "liveness", READINESS: "readiness", ROUTER: "router"}


def load(name):
    return (DATA / name).read_text()


def script():
    spec = importlib.util.spec_from_file_location("art_health", SKILL / "scripts" / "health.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def router_doc(state="HEALTHY"):
    return json.dumps(
        {
            "router": {"node_id": "n", "state": "HEALTHY", "message": "OK"},
            "services": [
                {"service_id": "jfac@1", "node_id": "n", "state": "HEALTHY", "message": "OK"},
                {
                    "service_id": "jfrt@1",
                    "node_id": "n",
                    "state": state,
                    "message": "OK" if state == "HEALTHY" else "db down",
                },
            ],
        }
    )


HEALTHY = {LIVENESS: (0, "OK", ""), READINESS: (0, "OK", ""), ROUTER: (0, router_doc(), "")}
BROKEN = {
    LIVENESS: (0, "OK", ""),
    READINESS: (1, "", UNAVAILABLE),
    ROUTER: (0, router_doc("UNHEALTHY"), ""),
}
SELECTOR = {
    "artifactory": "app=artifactory,release=artifactory,role=artifactory",
    "artifactory-broken": "app=artifactory,release=artifactory-broken,role=artifactory",
}


def raw(kind, name, port, path, scheme="http"):
    return (
        "kubectl",
        "get",
        "--raw",
        f"/api/v1/namespaces/{NS}/{kind}/{scheme}:{name}:{port}/proxy{path}",
        "--request-timeout=30s",
    )


def projected(argv_tail, template, text, fail, key):
    argv = ("kubectl", "-n", NS, "get", *argv_tail, "-o", "jsonpath=" + template)
    return argv, ((1, "", fail[key]) if key in fail else (0, text, ""))


def stubs(
    name="artifactory",
    fail=None,
    replies=None,
    pods_text=None,
    scheme="http",
    kind="statefulset",
    **ports,
):
    """kubectl answers for a run on workload `name`. `fail` maps 'workload', 'pods', 'services',
    'proxy' or 'svc_proxy' to stderr; `replies` maps a path (all asks) or (target, path) to
    (rc, stdout, stderr)."""
    fail, replies = fail or {}, replies or {}
    base = HEALTHY if name == "artifactory" else BROKEN
    a_port, r_port = ports.get("port", 8081), ports.get("router_port", 8082)
    pods_text = load(f"pods_{name}.txt") if pods_text is None else pods_text
    out = dict(
        [
            projected(
                (f"{kind}s.apps", name),
                project.WORKLOAD_FIELDS.template(False),
                load(f"workload_{name}.txt").replace("StatefulSet", KINDS[kind]),
                fail,
                "workload",
            ),
            projected(
                ("pods", "-l", SELECTOR[name]),
                project.POD_FIELDS.template(True),
                pods_text,
                fail,
                "pods",
            ),
            projected(
                ("services",),
                project.SERVICE_FIELDS.template(True),
                load("services.txt"),
                fail,
                "services",
            ),
        ]
    )
    pod = f"{name}-0"
    for path, default in base.items():
        port = r_port if path == ROUTER else a_port
        reply = replies.get((pod, path), replies.get(path, default))
        out[raw("pods", pod, port, path, scheme)] = (
            (1, "", fail["proxy"]) if "proxy" in fail else reply
        )
        svc_port = "http-router" if path == ROUTER else "http-artifactory"
        reply = replies.get((name, path), replies.get(path, default))
        if name == "artifactory-broken":
            reply = replies.get((name, path), (1, "", NO_ENDPOINTS))
        out[raw("services", name, svc_port, path, scheme)] = (
            (1, "", fail["svc_proxy"]) if "svc_proxy" in fail else reply
        )
    return out


def run(name="artifactory", inputs=None, res=None, settings=None, kind="statefulset", **kw):
    """(envelope, calls) of the task on the StatefulSet (or Deployment) `name`."""
    resource = res or {
        "cluster": "kind",
        "namespace": NS,
        "name": name,
        "type": f"kubernetes/{kind}",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/{kind}s/{name}",
    }
    inputs = inputs or {}
    with make_context(resource=resource, settings=settings or SETTINGS) as tc:
        table = stubs(name, kind=kind, **inputs, **kw)
        calls = stub_commands(tc, table)
        env = run_main(script(), tc, **inputs)
    return env, calls


def check_schema(env):
    schema = json.loads((SKILL / TASKS["health"]["outputs"]["health"]["schema"]).read_text())
    jsonschema.validate(env["outputs"]["health"], schema, cls=jsonschema.Draft202012Validator)


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def pod_of(env):
    return env["outputs"]["health"]["pods"][0]


def service_of(env):
    return env["outputs"]["health"]["services"][0]


# ------------------------------------------------------------ the scenarios ----


def test_healthy_every_pod_and_service_endpoint_is_ok():
    env, calls = run()
    assert env["status"] == "ok" and env["errors"] == []
    out = env["outputs"]["health"]
    assert out["workload"] == {
        "kind": "StatefulSet",
        "name": "artifactory",
        "replicas": 1,
        "readyReplicas": 1,
    }
    assert (out["podCount"], out["serviceCount"], out["servicesRead"], out["notOkCount"]) == (
        1,
        1,
        True,
        0,
    )
    for row in (pod_of(env), service_of(env)):
        assert [row[n]["ok"] for n in ("liveness", "readiness", "router")] == [True] * 3
        assert row["liveness"]["body"] == "OK"
        assert row["router"]["router"]["unhealthyCount"] == 0
    assert pod_of(env)["podReady"] is True
    assert (pod_of(env)["artifactoryPort"], pod_of(env)["routerPort"]) == (8081, 8082)
    check_schema(env)
    # the Service is asked on its own port names: the ones whose targetPort is the pod's port
    assert raw("services", "artifactory", "http-artifactory", LIVENESS) in calls
    assert raw("services", "artifactory", "http-router", ROUTER) in calls
    assert raw("pods", "artifactory-0", 8081, LIVENESS) in calls


def test_broken_a_503_is_a_fact_and_the_router_names_the_unhealthy_service():
    env, _ = run("artifactory-broken")
    assert env["status"] == "ok" and env["errors"] == []
    pod = pod_of(env)
    assert (pod["liveness"]["ok"], pod["liveness"]["httpStatus"]) == (True, 200)
    assert (pod["readiness"]["ok"], pod["readiness"]["httpStatus"]) == (False, 503)
    assert pod["router"]["ok"] is False and pod["router"]["httpStatus"] == 200
    assert pod["router"]["router"]["unhealthyCount"] == 1
    unhealthy = [s for s in pod["router"]["router"]["services"] if s["state"] != "HEALTHY"]
    assert [(s["serviceId"], s["message"]) for s in unhealthy] == [("jfrt@1", "db down")]
    assert pod["podReady"] is False
    service = service_of(env)
    for name in ("liveness", "readiness", "router"):
        row = service[name]
        assert (row["httpStatus"], row["ok"], row["noEndpoints"]) == (503, False, True)
    assert env["outputs"]["health"]["notOkCount"] == 5
    assert env["outputs"]["health"]["workload"]["readyReplicas"] is None
    check_schema(env)


@pytest.mark.parametrize(
    "path, reply",
    [
        (LIVENESS, (0, "<html><body>Sign in</body></html>", "")),
        (READINESS, (0, "", "")),
        (READINESS, (0, "NOT OK", "")),
        (ROUTER, (0, "<html>nope</html>", "")),
        (ROUTER, (0, '{"router": {}}', "")),
        (ROUTER, (0, "[]", "")),
    ],
)
def test_unexpected_payload_is_an_error_never_ok(path, reply):
    env, _ = run(replies={path: reply})
    name = NAMES[path]
    assert env["status"] == "partial"
    assert codes(env) == [
        ("internal", f"pod/artifactory-0/{name}"),
        ("internal", f"service/artifactory/{name}"),
    ]
    for row in (pod_of(env), service_of(env)):
        assert row[name]["ok"] is None and row[name]["httpStatus"] is None
    check_schema(env)


def test_a_404_is_served_false_and_not_an_error():
    env, _ = run(replies={ROUTER: (1, "", NO_ROUTE)})
    assert env["status"] == "ok"
    for row in (pod_of(env), service_of(env)):
        assert (row["router"]["served"], row["router"]["ok"], row["router"]["httpStatus"]) == (
            False,
            None,
            404,
        )
        assert row["liveness"]["ok"] is True


def test_every_endpoint_wanting_credentials_skips_the_task():
    env, _ = run(
        replies={
            LIVENESS: (1, "", BACKEND_403),
            READINESS: (1, "", BACKEND_403),
            ROUTER: (1, "", BACKEND_401),
        }
    )
    assert env["status"] == "skipped"
    assert "credentials" in env["reason"] and "misc plugin, M4" in env["reason"]
    assert env["errors"] == [] and env["outputs"] == {}


def test_some_endpoints_wanting_credentials_are_partial_with_a_scoped_permission_error():
    env, _ = run(replies={READINESS: (1, "", BACKEND_403)})
    assert env["status"] == "partial"
    assert codes(env) == [
        ("permission_denied", "pod/artifactory-0/readiness"),
        ("permission_denied", "service/artifactory/readiness"),
    ]
    assert "generic connection" in env["errors"][0]["hint"]
    row = pod_of(env)["readiness"]
    assert (row["authRequired"], row["ok"], row["httpStatus"]) == (True, None, 403)
    assert pod_of(env)["liveness"]["ok"] is True
    check_schema(env)


def test_a_backend_401_is_auth_required_not_an_api_credential_failure():
    env, _ = run(replies={LIVENESS: (1, "", BACKEND_401)})
    assert env["status"] == "partial"
    assert pod_of(env)["liveness"]["authRequired"] is True
    assert pod_of(env)["liveness"]["httpStatus"] == 401


def test_rbac_refusal_of_the_pod_proxy_fails_the_task_with_permission_denied():
    env, _ = run(fail={"proxy": FORBIDDEN.format(r="pods/proxy")})
    assert env["status"] == "failed"
    assert env["errors"][0]["code"] == "permission_denied" and env["outputs"] == {}


@pytest.mark.parametrize(
    "fail, code",
    [
        ({"workload": FORBIDDEN.format(r="statefulsets.apps")}, "permission_denied"),
        (
            {"workload": 'Error from server (NotFound): statefulsets.apps "x" not found'},
            "not_found",
        ),
        ({"workload": "Unable to connect to the server: dial tcp: i/o timeout"}, "unreachable"),
        ({"pods": FORBIDDEN.format(r="pods")}, "permission_denied"),
    ],
)
def test_a_failed_primary_read_fails_the_task_with_its_code(fail, code):
    env, _ = run(fail=fail)
    assert (env["status"], env["errors"][0]["code"], env["outputs"]) == ("failed", code, {})


def test_forbidden_service_list_keeps_the_pod_facts():
    env, _ = run(fail={"services": FORBIDDEN.format(r="services")})
    assert env["status"] == "partial"
    assert codes(env) == [("permission_denied", "services")]
    out = env["outputs"]["health"]
    assert (out["servicesRead"], out["serviceCount"], out["services"]) == (False, 0, [])
    assert pod_of(env)["readiness"]["ok"] is True
    check_schema(env)


def test_forbidden_service_proxy_keeps_the_pod_facts():
    env, _ = run(fail={"svc_proxy": FORBIDDEN.format(r="services/proxy")})
    assert env["status"] == "partial"
    assert codes(env) == [("permission_denied", "services/proxy")]
    assert service_of(env)["queried"] is False
    assert service_of(env)["liveness"] is None
    assert pod_of(env)["liveness"]["ok"] is True


def test_an_unreachable_pod_fails_the_task_when_nothing_answered():
    env, _ = run(fail={"proxy": UNREACHABLE, "svc_proxy": UNREACHABLE})
    assert env["status"] == "failed"
    assert env["errors"][0]["code"] == "unreachable"
    assert env["errors"][0]["scope"] == "pod/artifactory-0/liveness"
    assert env["outputs"] == {}


def test_one_unreachable_endpoint_is_partial_and_the_rest_stands():
    env, _ = run(replies={("artifactory-0", ROUTER): (1, "", UNREACHABLE)})
    assert env["status"] == "partial"
    assert codes(env) == [("unreachable", "pod/artifactory-0/router")]
    assert pod_of(env)["router"]["httpStatus"] is None
    assert pod_of(env)["liveness"]["ok"] is True
    assert service_of(env)["router"]["ok"] is True


def test_pods_that_are_not_running_are_listed_and_not_asked():
    text = load("pods_artifactory.txt").replace("status.phase=Running", "status.phase=Pending")
    env, calls = run(pods_text=text)
    pod = pod_of(env)
    assert (pod["phase"], pod["queried"], pod["liveness"]) == ("Pending", False, None)
    assert not [c for c in calls if "/pods/" in c[3]]
    assert env["status"] == "ok"


def test_a_workload_without_pods_is_a_fact():
    env, calls = run(pods_text="")
    assert env["status"] == "ok"
    out = env["outputs"]["health"]
    assert (out["podCount"], out["pods"]) == (0, [])
    # the Services are asked on the chart's port names, as there is no pod to name the ports
    assert service_of(env)["liveness"]["port"] == 8081


@pytest.mark.parametrize(
    "inputs", [{"scheme": "ftp"}, {"port": 0}, {"port": 70000}, {"router_port": "x"}]
)
def test_bad_input_is_invalid_input_before_any_kubectl_call(inputs):
    env, calls = run(inputs=inputs)
    assert env["status"] == "failed" and env["errors"][0]["code"] == "invalid_input"
    assert calls == []


def test_a_target_that_is_not_a_statefulset_or_deployment_is_invalid_input():
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "x",
        "type": "kubernetes/daemonset",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/daemonsets/x",
    }
    env, calls = run(res=res)
    assert env["errors"][0]["code"] == "invalid_input" and calls == []


def test_the_type_falls_back_to_the_plural_of_the_path():
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "artifactory",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/artifactory",
    }
    env, _ = run(res=res)
    assert env["status"] == "ok"


def test_port_inputs_and_https_change_the_urls():
    env, calls = run(inputs={"port": 8091, "router_port": 9000, "scheme": "https"})
    assert env["status"] == "ok"
    assert raw("pods", "artifactory-0", 8091, LIVENESS, "https") in calls
    assert raw("pods", "artifactory-0", 9000, ROUTER, "https") in calls


def test_max_items_cuts_the_pod_list_and_says_so():
    one = load("pods_artifactory.txt")
    two = one + one.replace("artifactory-0", "artifactory-1")
    env, _ = run(pods_text=two, settings={"maxItems": 1})
    out = env["outputs"]["health"]
    assert (out["podCount"], len(out["pods"]), out["truncated"]) == (2, 1, True)


# ------------------------------------------------------------ lib ----


class Canned:
    def __init__(self, rc, out, err):
        self.result = type("R", (), {"returncode": rc, "stdout": out, "stderr": err})

    def run(self, argv, check=True, **_):
        assert check is False
        return self.result


def test_proxy_get_tells_the_statuses_apart():
    assert proxy.proxy_get(Canned(0, "OK", ""), "/x") == proxy.Reply(200, "OK")
    assert proxy.proxy_get(Canned(1, "", NO_ROUTE), "/x").status == 404
    assert proxy.proxy_get(Canned(1, "", UNAVAILABLE), "/x") == proxy.Reply(503, "", "unavailable")
    assert proxy.proxy_get(Canned(1, "", NO_ENDPOINTS), "/x") == proxy.Reply(
        503, "", "no_endpoints"
    )
    assert proxy.proxy_get(Canned(1, "", BACKEND_403), "/x").status == 403
    assert proxy.proxy_get(Canned(1, "", BACKEND_401), "/x").status == 401
    for stderr, code in (
        (FORBIDDEN.format(r="pods/proxy"), "permission_denied"),
        (FORBIDDEN.format(r="services/proxy"), "permission_denied"),
        (UNREACHABLE, "unreachable"),
        ('Error from server (NotFound): pods "p" not found', "not_found"),
        (CLIENT_TIMEOUT, "timeout"),
        ("error: something odd", "internal"),
    ):
        with pytest.raises(RwError) as exc:
            proxy.proxy_get(Canned(1, "", stderr), "/x")
        assert exc.value.code == code, stderr


def test_parse_router_caps_and_rejects():
    big = {
        "router": {"state": "HEALTHY"},
        "services": [
            {"service_id": str(i), "state": "HEALTHY", "message": "m" * 2000} for i in range(60)
        ],
    }
    doc = health.parse_router(json.dumps(big), "w")
    assert (doc["serviceCount"], len(doc["services"]), doc["servicesTruncated"]) == (60, 50, True)
    assert doc["services"][0]["messageTruncated"] is True
    assert len(doc["services"][0]["message"]) == 1024
    for body in (
        "",
        "<html>",
        "[]",
        '{"router": {"state": 1}, "services": []}',
        '{"router": {"state": "HEALTHY"}}',
    ):
        with pytest.raises(RwError):
            health.parse_router(body, "w")


def test_port_resolution_and_service_matching():
    ports = [
        {"name": "http", "containerPort": 8082},
        {"name": "http-internal", "containerPort": 8081},
    ]
    pod = {"spec": {"containers": [{"name": "c", "ports": ports}]}}
    assert fetch.pod_port(pod, "artifactory") == (8081, "http-internal")
    assert fetch.pod_port(pod, "router") == (8082, "http")
    assert fetch.pod_port(pod, "artifactory", 8091) == (8091, None)
    assert fetch.pod_port({}, "artifactory") == (8081, None)
    assert fetch.pod_port({}, "router") == (8082, None)
    svc = {
        "spec": {
            "ports": [
                {"name": "a", "port": 80, "targetPort": 8081},
                {"port": 81, "targetPort": "http"},
            ]
        }
    }
    assert fetch.service_port_for(svc, 8081, ("http-internal",)) == ("a", 80)
    assert fetch.service_port_for(svc, 8082, ("http",)) == (81, 81)
    assert fetch.service_port_for(svc, 9999) is None


def test_selector_string():
    assert fetch.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    sel = {"matchExpressions": [{"key": "k", "operator": "In", "values": ["x", "y"]}]}
    assert fetch.selector_string(sel) == "k in (x,y)"
    with pytest.raises(RwError):
        fetch.selector_string({})


def test_services_are_matched_by_the_pod_template_labels():
    # a Service whose selector is not within the template's labels, and one without a selector,
    # are not this workload's
    workload = project.WORKLOAD_FIELDS.parse(load("workload_artifactory.txt"), False)
    other = (
        load("services.txt")
        + '@\ns:metadata.name=other\ns:spec.type=ClusterIP\nj:spec.selector={"app":"other"}\n'
        + "@\ns:metadata.name=all\ns:spec.type=ClusterIP\n"
    )

    class Ctx:
        def run(self, argv, check=True, **_):
            return type("R", (), {"returncode": 0, "stdout": other, "stderr": ""})

    services, readable = fetch.workload_services(Ctx(), workload)
    assert readable is True and [s["metadata"]["name"] for s in services] == ["artifactory"]


def test_the_projections_never_ask_for_env():
    for projection in (project.WORKLOAD_FIELDS, project.POD_FIELDS, project.SERVICE_FIELDS):
        for many in (True, False):
            assert "env" not in projection.template(many)
    for path in DATA.iterdir():
        text = path.read_text().lower()
        assert '"env"' not in text and '"value"' not in text


def test_projection_parse_rejects_what_is_not_the_projection():
    with pytest.raises(RwError):
        project.POD_FIELDS.parse('{"items": []}', True)
    with pytest.raises(RwError):
        project.WORKLOAD_FIELDS.parse("", False)


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
    "metadata": {"name": "a", "namespace": NS},
    "spec": {"replicas": 3},
    "status": {},
}


def pods(n):
    return [
        {"metadata": {"name": f"a-{i}"}, "status": {"phase": "Running"}, "spec": {}}
        for i in range(n)
    ]


def test_the_task_deadline_leaves_the_remaining_asks_a_timeout_error():
    ctx = Clocked(lambda i, argv: (0, router_doc() if "router" in argv[3] else "OK", ""))
    budget = fetch.Budget(clock=ctx.clock)
    out = health.view(ctx, WORKLOAD, pods(3), [], True, "http", None, None, 200, budget)
    # four asks at 0s, 30s, 60s, 90s (the fourth limited to the 10s left); the rest time out
    assert len(ctx.calls) == 4
    assert [c[4] for c in ctx.calls] == [f"--request-timeout={t}s" for t in (30, 30, 30, 10)]
    assert {c for c, _, _ in ctx.errors} == {"timeout"} and len(ctx.errors) == 5
    assert "100s deadline" in ctx.errors[0][2]
    assert out["pods"][2]["router"]["httpStatus"] is None


def test_three_timeouts_in_a_row_on_a_path_stop_asking_it():
    ctx = Clocked(lambda i, argv: (0, "OK", "") if i == 0 else (1, "", CLIENT_TIMEOUT), step=1)
    budget = fetch.Budget(clock=ctx.clock)
    health.view(ctx, WORKLOAD, pods(6), [], True, "http", None, None, 200, budget)
    liveness = [c for c in ctx.calls if c[3].endswith(LIVENESS)]
    assert len(liveness) == 4  # one ok, three timeouts, then no more asks of that path
    assert any("3 asks in a row timed out" in m for _, _, m in ctx.errors)


# ------------------------------------------------------------ permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "deployments.apps": ("apps", "deployments"),
    "pods": ("", "pods"),
    "services": ("", "services"),
}
VALUE_FLAGS = {"-n", "-o", "-l"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs. Raises on anything it
    does not know, so a script that issues a new call has to extend this."""
    if "--raw" in argv:
        path = argv[argv.index("--raw") + 1]
        for kind in ("pods", "services"):
            if f"/{kind}/" in path and "/proxy/" in path:
                return {("", f"{kind}/proxy", "get")}
        raise AssertionError(path)
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


def granted():
    return {
        (g, r, v)
        for rule in TASKS["health"]["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def test_declared_permissions_are_exactly_what_the_script_calls():
    needed = set()
    for name, kind in (("artifactory", "statefulset"), ("artifactory-broken", "deployment")):
        env, calls = run(name, kind=kind)
        assert env["status"] == "ok"
        for argv in calls:
            needed |= rbac_needs(list(argv))
    assert needed == granted(), f"calls {sorted(needed)}, declares {sorted(granted())}"


def test_rbac_needs_maps_the_verbs():
    tail = ["-o", "jsonpath=x"]
    assert rbac_needs(["kubectl", "-n", "x", "get", "statefulsets.apps", "a", *tail]) == {
        ("apps", "statefulsets", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", *tail]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "services", *tail]) == {
        ("", "services", "list")
    }
    path = "/api/v1/namespaces/n/services/http:s:p/proxy/x"
    assert rbac_needs(["kubectl", "get", "--raw", path]) == {("", "services/proxy", "get")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "exec", "p"])


def test_the_task_only_reads_and_never_execs():
    assert TASKS["health"]["readOnly"] is True
    assert all(v in ("get", "list") for (_, _, v) in granted())
    _, calls = run()
    assert all(c[1] in ("get", "-n") for c in calls)
    assert not [c for c in calls if any(a in ("exec", "port-forward") for a in c)]
