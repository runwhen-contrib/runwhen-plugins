"""Unit tests for k8s-artifactory-health's lib/ and script. The captured data comes from the
`rw-artifactory` namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-artifactory-health.yaml): `artifactory`, a healthy StatefulSet, and `artifactory-broken`,
whose readiness answers 503 and whose router lists a service UNHEALTHY. Both are nginx stubs
serving the documented endpoints, not a real Artifactory. `artifactory-numeric` is a second Service
of `artifactory` whose only port is unnamed with a numeric targetPort. The workload, pod and Service data are
the narrow `-o jsonpath` projections the script asks for (no env, no pod template). The script runs
through the SDK's test context (`ctx.run` raises on a non-zero exit like the real runner;
`stub_commands` matches the exact kubectl argv, so an unexpected call fails). The pod and Service proxy
reads go through `ctx.k8s_proxy_get`, stood in for by `proxy_fake.ProxyFake`; a proxy answer written
in the tables as kubectl's (rc, stdout, stderr) is turned into the helper's answer by `served_as`."""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_artifactory_health import fetch, health, k8s, project, proxy
from k8s_artifactory_health.errors import RwError
from proxy_fake import ProxyFake, api_error, cross_host_redirect, reply
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
ROUTER_LIVE = "/router/api/v1/system/liveness"
ROUTER_READY = "/router/api/v1/system/readiness"
KINDS = {"statefulset": "StatefulSet", "deployment": "Deployment"}
NAMES = {
    LIVENESS: "liveness",
    READINESS: "readiness",
    ROUTER: "router",
    ROUTER_LIVE: "routerLiveness",
    ROUTER_READY: "routerReadiness",
}
CODE_OK = '{"code":"OK"}'
# the Services of each workload, with the proxy port each is asked on per port kind
SERVICES_OF = {
    "artifactory": {
        "artifactory": {"artifactory": 8081, "router": "http-router"},
        "artifactory-numeric": {"artifactory": 80},
    },
    "artifactory-broken": {"artifactory-broken": {"artifactory": 8081, "router": "http-router"}},
}


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


HEALTHY = {
    LIVENESS: (0, "OK", ""),
    READINESS: (0, "OK", ""),
    ROUTER: (0, router_doc(), ""),
    ROUTER_LIVE: (0, CODE_OK, ""),
    ROUTER_READY: (0, CODE_OK, ""),
}
BROKEN = {
    LIVENESS: (0, CODE_OK, ""),
    READINESS: (1, "", UNAVAILABLE),
    ROUTER: (0, router_doc("UNHEALTHY"), ""),
    ROUTER_LIVE: (0, CODE_OK, ""),
    ROUTER_READY: (0, CODE_OK, ""),
}
SELECTOR = {
    "artifactory": "app=artifactory,release=artifactory,role=artifactory",
    "artifactory-broken": "app=artifactory,release=artifactory-broken,role=artifactory",
}


def raw(kind, name, port, path, scheme="http"):
    """The key of one proxied request: a `ctx.k8s_proxy_get` call, recorded in `calls` as this tuple
    (kind is pods or services)."""
    return ("k8s_proxy_get", NS, kind, name, str(port), path, scheme)


def served_as(answer):
    """The ctx.k8s_proxy_get answer for a table entry (rc, stdout, stderr): a body for 0, the
    backend's own 401/403/404/503 for the stderr kubectl printed for them, the API server's no-endpoints
    and RBAC refusals as RwErrors, else the error `k8s.classify` names."""
    if isinstance(answer, dict):
        return answer
    rc, out, err = answer
    if rc == 0:
        return out
    if err == UNAVAILABLE:
        return reply(503, "service unavailable")
    if err == NO_ROUTE:
        return reply(404, "404 page not found")
    if err == BACKEND_401:
        return reply(401, "Unauthorized")
    if err == BACKEND_403 or err == SPOOF_RBAC:
        return reply(403, "<html><body>Authentication required</body></html>")
    if err == NO_ENDPOINTS:
        return api_error(err, lambda rc, e: "unreachable")
    return api_error(err, k8s.classify)


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
        kind_of = "router" if path.startswith("/router") else "artifactory"
        port = r_port if kind_of == "router" else a_port
        reply = replies.get((pod, path), replies.get(path, default))
        out[raw("pods", pod, port, path, scheme)] = (
            (1, "", fail["proxy"]) if "proxy" in fail else reply
        )
        for service, svc_ports in SERVICES_OF[name].items():
            if kind_of not in svc_ports:
                continue
            reply = replies.get((service, path), replies.get(path, default))
            if name == "artifactory-broken":
                reply = replies.get((service, path), (1, "", NO_ENDPOINTS))
            out[raw("services", service, svc_ports[kind_of], path, scheme)] = (
                (1, "", fail["svc_proxy"]) if "svc_proxy" in fail else reply
            )
    return out


RESOURCE = {
    "cluster": "kind",
    "namespace": NS,
    "name": "artifactory",
    "type": "kubernetes/statefulset",
    "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/artifactory",
}


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
        calls = stub_commands(tc, {k: v for k, v in table.items() if k[0] == "kubectl"})

        def answer(request):
            key = ("k8s_proxy_get", request["namespace"], request["kind"], request["name"])
            key += (str(request["port"]), request["path"], request["scheme"])
            calls.append(key)
            assert key in table, f"ctx.k8s_proxy_get called with an unstubbed request {key!r}"
            return served_as(table[key])

        tc.ctx.k8s_proxy_get = ProxyFake(answer)
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
        2,
        True,
        0,
    )
    for row in (pod_of(env), service_of(env)):
        assert [row[n]["ok"] for n in NAMES.values()] == [True] * 5
        assert row["liveness"]["body"] == "OK"
        assert row["routerLiveness"]["body"] == CODE_OK
        assert row["router"]["router"]["unhealthyCount"] == 0
    assert pod_of(env)["podReady"] is True
    assert (pod_of(env)["artifactoryPort"], pod_of(env)["routerPort"]) == (8081, 8082)
    check_schema(env)
    # the Service is asked on its own port names: the ones whose targetPort is the pod's port
    # a Service port name longer than the helper's 15 characters (`http-artifactory`) is asked by number
    assert raw("services", "artifactory", 8081, LIVENESS) in calls
    assert raw("services", "artifactory", "http-router", ROUTER) in calls
    assert raw("pods", "artifactory-0", 8081, LIVENESS) in calls
    # the router's own liveness and readiness (the chart's probes) are asked on the router port
    assert raw("pods", "artifactory-0", 8082, ROUTER_LIVE) in calls
    assert raw("pods", "artifactory-0", 8082, ROUTER_READY) in calls
    assert raw("services", "artifactory", "http-router", ROUTER_READY) in calls


def test_a_service_with_a_numeric_target_port_is_asked_on_its_port_number():
    env, calls = run()
    numeric = env["outputs"]["health"]["services"][1]
    assert numeric["name"] == "artifactory-numeric"
    # its only port is unnamed (port 80 -> targetPort 8081): the number is the proxy port, and the
    # router endpoints, which it does not expose, are null rather than asked
    assert raw("services", "artifactory-numeric", 80, LIVENESS) in calls
    assert (numeric["liveness"]["port"], numeric["liveness"]["ok"]) == (80, True)
    assert (numeric["readiness"]["ok"], numeric["router"]) == (True, None)
    assert numeric["routerLiveness"] is None and numeric["routerReadiness"] is None
    assert not [c for c in calls if c[3] == "artifactory-numeric" and "/router/" in c[5]]


def test_broken_a_503_is_a_fact_and_the_router_names_the_unhealthy_service():
    env, _ = run("artifactory-broken")
    assert env["status"] == "ok" and env["errors"] == []
    pod = pod_of(env)
    assert (pod["liveness"]["ok"], pod["liveness"]["httpStatus"]) == (True, 200)
    assert (pod["readiness"]["ok"], pod["readiness"]["httpStatus"]) == (False, 503)
    assert pod["router"]["ok"] is False and pod["router"]["httpStatus"] == 200
    assert (pod["liveness"]["body"], pod["routerReadiness"]["ok"]) == (CODE_OK, True)
    assert pod["router"]["router"]["unhealthyCount"] == 1
    unhealthy = [s for s in pod["router"]["router"]["services"] if s["state"] != "HEALTHY"]
    assert [(s["serviceId"], s["message"]) for s in unhealthy] == [("jfrt@1", "db down")]
    assert pod["podReady"] is False
    service = service_of(env)
    for name in NAMES.values():
        row = service[name]
        assert (row["httpStatus"], row["ok"], row["noEndpoints"]) == (503, False, True)
    assert (
        env["outputs"]["health"]["notOkCount"] == 7
    )  # the pod's readiness and router, 5 on the Service
    assert env["outputs"]["health"]["workload"]["readyReplicas"] is None
    check_schema(env)


def asked_services(path):
    """The Services of `artifactory` that are asked `path`: the numeric one has no router port."""
    return ["artifactory"] if path.startswith("/router") else ["artifactory", "artifactory-numeric"]


@pytest.mark.parametrize(
    "path, reply",
    [
        (LIVENESS, (0, "<html><body>Sign in</body></html>", "")),
        (LIVENESS, (0, '{"code":"UNAVAILABLE"}', "")),
        (LIVENESS, (0, '{"status":"OK"}', "")),
        (LIVENESS, (0, '"OK"', "")),
        (READINESS, (0, "", "")),
        (READINESS, (0, "NOT OK", "")),
        (ROUTER, (0, "<html>nope</html>", "")),
        (ROUTER, (0, '{"router": {}}', "")),
        (ROUTER, (0, "[]", "")),
        (ROUTER_LIVE, (0, "OK but not really", "")),
        (ROUTER_READY, (0, '{"code":"ERROR"}', "")),
    ],
)
def test_unexpected_payload_is_an_error_never_ok(path, reply):
    env, _ = run(replies={path: reply})
    name = NAMES[path]
    assert env["status"] == "partial"
    assert codes(env) == [("internal", f"pod/artifactory-0/{name}")] + [
        ("internal", f"service/{service}/{name}") for service in asked_services(path)
    ]
    for row in (pod_of(env), service_of(env)):
        assert row[name]["ok"] is None and row[name]["httpStatus"] is None
    check_schema(env)


@pytest.mark.parametrize(
    "path, reply, body",
    [
        (LIVENESS, (0, "OK", ""), "OK"),
        (LIVENESS, (0, "OK\n", ""), "OK"),
        (READINESS, (0, '{"code":"OK"}', ""), '{"code":"OK"}'),
        (READINESS, (0, ' {"code": "OK", "message": "x"} ', ""), '{"code": "OK", "message": "x"}'),
        (ROUTER_LIVE, (0, CODE_OK, ""), CODE_OK),
        (ROUTER_READY, (0, "OK", ""), "OK"),
    ],
)
def test_ok_is_the_bare_text_or_a_json_document_with_code_ok(path, reply, body):
    env, _ = run(replies={path: reply})
    assert env["status"] == "ok" and env["errors"] == []
    row = pod_of(env)[NAMES[path]]
    assert (row["ok"], row["httpStatus"], row["body"]) == (True, 200, body)
    check_schema(env)


def test_the_router_endpoints_are_probed_on_the_router_port_input():
    env, calls = run(inputs={"router_port": 9000})
    assert env["status"] == "ok"
    for path in (ROUTER, ROUTER_LIVE, ROUTER_READY):
        assert raw("pods", "artifactory-0", 9000, path) in calls
    assert (pod_of(env)["routerLiveness"]["port"], pod_of(env)["routerPort"]) == (9000, 9000)


def test_a_404_from_a_pods_liveness_or_readiness_is_ok_false_with_a_reason():
    env, _ = run(replies={("artifactory-0", READINESS): (1, "", NO_ROUTE)})
    assert env["status"] == "ok" and env["errors"] == []
    row = pod_of(env)["readiness"]
    assert (row["httpStatus"], row["served"], row["ok"]) == (404, False, False)
    assert row["reason"] == "not served (booting, wrong port, or not Artifactory)"
    assert pod_of(env)["liveness"]["reason"] is None
    out = env["outputs"]["health"]
    assert out["notOkCount"] == 1
    check_schema(env)


@pytest.mark.parametrize("path", [LIVENESS, READINESS, ROUTER_LIVE, ROUTER_READY])
def test_each_probe_endpoint_on_a_pod_counts_a_404_as_not_ok(path):
    env, _ = run(replies={("artifactory-0", path): (1, "", NO_ROUTE)})
    row = pod_of(env)[NAMES[path]]
    assert (row["ok"], row["served"], row["reason"] is not None) == (False, False, True)
    assert env["outputs"]["health"]["notOkCount"] == 1


def test_a_404_on_a_service_or_on_the_router_health_stays_served_false_ok_null():
    env, _ = run(replies={("artifactory", READINESS): (1, "", NO_ROUTE), ROUTER: (1, "", NO_ROUTE)})
    assert env["status"] == "ok"
    for row in (pod_of(env)["router"], service_of(env)["readiness"], service_of(env)["router"]):
        assert (row["served"], row["ok"], row["reason"]) == (False, None, None)
    assert env["outputs"]["health"]["notOkCount"] == 0


def test_every_endpoint_of_every_pod_answering_404_fails_with_not_found():
    env, _ = run(replies={p: (1, "", NO_ROUTE) for p in NAMES})
    assert env["status"] == "failed" and env["outputs"] == {}
    assert [(e["code"], e["message"]) for e in env["errors"]] == [
        ("not_found", "no Artifactory endpoint answered; check the port input")
    ]


def test_one_endpoint_answering_something_else_keeps_the_result():
    replies = {p: (1, "", NO_ROUTE) for p in NAMES}
    replies[("artifactory-0", LIVENESS)] = (0, "OK", "")
    env, _ = run(replies=replies)
    assert env["status"] == "ok"
    assert pod_of(env)["liveness"]["ok"] is True and pod_of(env)["readiness"]["ok"] is False
    # an endpoint that failed (not answered) is not a 404 either
    replies[("artifactory-0", LIVENESS)] = (1, "", UNREACHABLE)
    env, _ = run(replies=replies)
    assert env["status"] == "partial"
    assert [e["code"] for e in env["errors"]] == ["unreachable"]


def test_every_endpoint_wanting_credentials_skips_the_task():
    wants = {p: (1, "", BACKEND_403) for p in (LIVENESS, READINESS)}
    wants |= {p: (1, "", BACKEND_401) for p in (ROUTER, ROUTER_LIVE, ROUTER_READY)}
    env, _ = run(replies=wants)
    assert env["status"] == "skipped"
    assert "credentials" in env["reason"] and "misc plugin, M4" in env["reason"]
    assert env["errors"] == [] and env["outputs"] == {}


def test_some_endpoints_wanting_credentials_are_partial_with_a_scoped_permission_error():
    env, _ = run(replies={READINESS: (1, "", BACKEND_403)})
    assert env["status"] == "partial"
    assert codes(env) == [
        ("permission_denied", "pod/artifactory-0/readiness"),
        ("permission_denied", "service/artifactory/readiness"),
        ("permission_denied", "service/artifactory-numeric/readiness"),
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
    assert not [c for c in calls if c[0] == "k8s_proxy_get" and c[2] == "pods"]
    # the workload wants a replica and none is Running: the result is partial, not clean
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("unreachable", "pods")]
    assert "1 replica(s) but no pod is Running" in env["errors"][0]["message"]


def test_no_running_pod_is_only_an_error_when_the_workload_wants_replicas():
    text = load("pods_artifactory.txt").replace("status.phase=Running", "status.phase=Pending")
    zero = load("workload_artifactory.txt").replace("j:spec.replicas=1", "j:spec.replicas=0")
    assert zero != load("workload_artifactory.txt")
    with make_context(resource=RESOURCE, settings=SETTINGS) as tc:
        table = stubs(pods_text=text)
        table[next(a for a in table if "statefulsets.apps" in a)] = (0, zero, "")
        stub_commands(tc, {k: v for k, v in table.items() if k[0] == "kubectl"})
        tc.ctx.k8s_proxy_get = ProxyFake(
            lambda r: served_as(
                table[
                    ("k8s_proxy_get", r["namespace"], r["kind"], r["name"])
                    + (str(r["port"]), r["path"], r["scheme"])
                ]
            )
        )
        env = run_main(script(), tc)
    assert env["status"] == "ok" and env["errors"] == []


def test_a_workload_without_pods_is_a_fact():
    env, calls = run(pods_text="")
    assert env["status"] == "partial"  # replicas 1, no pod: see the test above
    assert [(e["code"], e["scope"]) for e in env["errors"]] == [("unreachable", "pods")]
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


class FakeProxyCtx:
    def __init__(self, answer):
        self.k8s_proxy_get = ProxyFake(answer)


def ask(answer, kind="pods"):
    target = proxy.Target(NS, kind, "p", "http", 8081, "/x")
    return proxy.proxy_get(FakeProxyCtx(answer), target)


def test_proxy_get_tells_the_statuses_apart():
    assert ask("OK") == proxy.Reply(200, "OK")
    assert ask(reply(404, "404 page not found")).status == 404
    assert ask(reply(503, "unavailable")) == proxy.Reply(503, "", "unavailable")
    assert ask(reply(403, "Authentication required")).status == 403
    assert ask(reply(401, "Unauthorized")).status == 401
    for kind in ("pods", "services"):  # the Service with no ready endpoint is a 503 with its reason
        no_endpoints = api_error(NO_ENDPOINTS, lambda rc, e: "unreachable")
        assert ask(no_endpoints, kind) == proxy.Reply(503, "", "no_endpoints")
    for stderr, code in (
        (FORBIDDEN.format(r="pods/proxy"), "permission_denied"),
        (FORBIDDEN.format(r="services/proxy"), "permission_denied"),
        (UNREACHABLE, "unreachable"),
        ('Error from server (NotFound): pods "p" not found', "not_found"),
        (CLIENT_TIMEOUT, "timeout"),
    ):
        with pytest.raises(RwError) as exc:
            ask(api_error(stderr, k8s.classify))
        assert exc.value.code == code, stderr


def test_proxy_get_asks_the_helper_for_the_pod_or_service_with_scheme_port_and_timeout():
    ctx = FakeProxyCtx("OK")
    proxy.proxy_get(ctx, proxy.service_target(NS, "svc", "https", "http-router", "/y"), 7)
    proxy.proxy_get(ctx, proxy.pod_target(NS, "pod-0", "http", 8081, "/x"))
    assert ctx.k8s_proxy_get.requests == [
        {
            "namespace": NS,
            "kind": "services",
            "name": "svc",
            "port": "http-router",
            "path": "/y",
            "scheme": "https",
            "timeout": 7,
        },
        {
            "namespace": NS,
            "kind": "pods",
            "name": "pod-0",
            "port": 8081,
            "path": "/x",
            "scheme": "http",
            "timeout": 30,
        },
    ]


def test_a_backend_403_that_quotes_the_rbac_wording_is_still_the_backends():
    # the helper tells the API server's refusal (an RwError) from the backend's status structurally,
    # so a body that merely mentions "is forbidden: User" is a 403 reply, never permission_denied
    quoted = reply(403, 'pods "x" is forbidden: User "u" cannot get resource "pods/proxy"')
    assert ask(quoted).status == 403


def test_a_cross_host_redirect_is_reported_and_never_followed():
    """S17: the helper drops the Location; the skill reports the host and asks nothing more."""
    ctx = FakeProxyCtx(cross_host_redirect("sso.example.invalid"))
    with pytest.raises(RwError) as exc:
        proxy.proxy_get(ctx, proxy.Target(NS, "services", "svc", "http", 8081, "/x"))
    assert exc.value.code == "dependency"
    assert "off cluster to sso.example.invalid (not followed)" in exc.value.message
    assert len(ctx.k8s_proxy_get.requests) == 1


def test_a_cross_host_redirect_through_the_task_is_an_error_per_endpoint_and_asks_once_each():
    env, calls = run(replies={LIVENESS: cross_host_redirect("sso.example.invalid")})
    assert env["status"] == "partial"
    redirected = [e for e in env["errors"] if "sso.example.invalid" in e["message"]]
    assert redirected and all(e["code"] == "dependency" for e in redirected)
    asked = [c for c in calls if c[0] == "k8s_proxy_get" and c[5] == LIVENESS]
    assert len(asked) == len(set(asked))  # each target asked once: nothing was chased


def test_a_same_target_redirect_is_followed_once_and_no_more():
    ctx = FakeProxyCtx(
        lambda r: reply(301, "", location="/a/x") if r["path"] == "/x" else reply(200, "OK")
    )
    assert proxy.proxy_get(ctx, proxy.pod_target(NS, "p", "http", 1, "/x")) == proxy.Reply(
        200, "OK"
    )
    assert ctx.k8s_proxy_get.paths() == ["/x", "/a/x"]
    loop = FakeProxyCtx(lambda r: reply(302, "", location="/again"))
    with pytest.raises(RwError):
        proxy.proxy_get(loop, proxy.pod_target(NS, "p", "http", 1, "/x"))
    assert len(loop.k8s_proxy_get.requests) == 2


def test_other_statuses_and_a_cut_body_are_internal_errors():
    for status in (400, 500, 502):
        with pytest.raises(RwError) as exc:
            ask(reply(status, "oops"))
        assert exc.value.code == "internal" and f"HTTP {status}" in exc.value.message
    with pytest.raises(RwError) as exc:
        ask(reply(200, '{"router": ', truncated=True))
    assert exc.value.code == "internal" and exc.value.hint


SPOOF_RBAC = (
    'Error from server (Forbidden): <html>pods "x" is forbidden: User "u" cannot get resource '
    '"pods/proxy"</html>'
)


def test_other_failures_are_mapped_from_the_error_from_server_reason():
    def code(stderr, rc=1):
        return k8s.classify(rc, stderr)

    assert code('Error from server (NotFound): pods "p" not found') == "not_found"
    assert code("Error from server (Timeout): request did not complete within 30s") == "timeout"
    assert code("Error from server (TooManyRequests): slow down") == "rate_limited"
    assert code("Error from server (InternalError): oops") == "internal"
    assert code("Error from server (SomethingNew): x") == "internal"
    assert code(UNREACHABLE) == "unreachable"
    assert code("Error from server (ServiceUnavailable): something else") == "unreachable"
    # the backend's body text never decides: these bodies mention other classes' wording
    assert (
        code("Error from server (InternalError): x509 connection refused not found") == "internal"
    )
    assert code("anything", rc=124) == "timeout" and code("anything", rc=127) == "dependency"


def test_a_failure_shows_only_the_first_line_of_a_backend_body_cut_at_256():
    body = "Error from server (Forbidden): " + "x" * 400 + "\n<html>\nsecret page\n</html>"
    err = k8s.failure(["kubectl", "get"], type("R", (), {"returncode": 1, "stderr": body}))
    assert err.message.startswith("kubectl exited 1: Error from server (Forbidden): xxx")
    assert len(err.message) == len("kubectl exited 1: ") + 256 and "secret" not in err.message
    warned = 'Warning: deprecated\nError from server (NotFound): pods "p" not found\nmore'
    err = k8s.failure(["kubectl", "get"], type("R", (), {"returncode": 1, "stderr": warned}))
    assert err.message == 'kubectl exited 1: Error from server (NotFound): pods "p" not found'
    assert err.code == "not_found"


def test_a_401_line_is_a_credential_failure_for_the_api_and_the_backends_401_on_a_proxy_call():
    assert k8s.classify(1, BACKEND_401) == "permission_denied"
    assert ask(reply(401, "Unauthorized")).status == 401  # on a proxy call it is the backend's


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
    # ids are cut at 1024 as well
    long_ids = {
        "router": {"state": "HEALTHY"},
        "services": [{"service_id": "s" * 3000, "node_id": "n" * 3000, "state": "HEALTHY"}],
    }
    row = health.parse_router(json.dumps(long_ids), "w")["services"][0]
    assert (len(row["serviceId"]), len(row["nodeId"])) == (1024, 1024)
    jsonschema.validate(
        row,
        json.loads((SKILL / "schemas/health.v1.json").read_text())["properties"]["pods"]["items"][
            "properties"
        ]["router"]["properties"]["router"]["properties"]["services"]["items"],
        cls=jsonschema.Draft202012Validator,
    )
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
    long_name = {
        "spec": {"ports": [{"name": "http-artifactory", "port": 8081, "targetPort": 8081}]}
    }
    assert fetch.service_port_for(long_name, 8081) == (
        8081,
        8081,
    )  # over the helper's 15 characters


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
    assert readable is True
    assert [s["metadata"]["name"] for s in services] == ["artifactory", "artifactory-numeric"]


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
    """A ctx whose `k8s_proxy_get` calls each take `step` seconds of a fake clock."""

    def __init__(self, answer, step=30):
        self.now, self.step, self.answer = 0.0, step, answer
        self.calls, self.errors = [], []

    def clock(self):
        return self.now

    def k8s_proxy_get(self, namespace, kind, name, port, path, *, scheme="http", timeout=30):
        def answer(request):
            self.calls.append(request)
            out = served_as(self.answer(len(self.calls) - 1, request))
            self.now += self.step
            return out

        return ProxyFake(answer)(namespace, kind, name, port, path, scheme=scheme, timeout=timeout)

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
    ctx = Clocked(lambda i, r: (0, router_doc() if r["path"].endswith("/health") else "OK", ""))
    budget = fetch.Budget(clock=ctx.clock)
    out = health.view(ctx, WORKLOAD, pods(3), [], True, "http", None, None, 200, budget)
    # four asks at 0s, 30s, 60s, 90s (the fourth limited to the 10s left); the other 11 of the
    # 3 pods' 5 endpoints time out
    assert len(ctx.calls) == 4
    assert [c["timeout"] for c in ctx.calls] == [30, 30, 30, 10]
    assert {c for c, _, _ in ctx.errors} == {"timeout"} and len(ctx.errors) == 11
    assert "100s deadline" in ctx.errors[0][2]
    assert out["pods"][2]["router"]["httpStatus"] is None


def test_three_timeouts_in_a_row_on_a_path_stop_asking_it():
    ctx = Clocked(lambda i, r: (0, "OK", "") if i == 0 else (1, "", CLIENT_TIMEOUT), step=1)
    budget = fetch.Budget(clock=ctx.clock)
    health.view(ctx, WORKLOAD, pods(6), [], True, "http", None, None, 200, budget)
    liveness = [c for c in ctx.calls if c["path"].endswith(LIVENESS)]
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
    assert "--raw" not in argv, argv  # proxy reads go through ctx.k8s_proxy_get (S17)
    if argv[0] == "k8s_proxy_get":  # (ns, kind, name, ...): pods/proxy or services/proxy get
        return {("", f"{argv[2]}/proxy", "get")}
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
    assert rbac_needs(list(raw("services", "s", "p", "/x"))) == {("", "services/proxy", "get")}
    assert rbac_needs(list(raw("pods", "p", 8081, "/x"))) == {("", "pods/proxy", "get")}
    with pytest.raises(AssertionError):  # S17: a proxied read through kubectl get --raw
        rbac_needs(["kubectl", "get", "--raw", "/api/v1/namespaces/n/services/s:p/proxy/x"])
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "exec", "p"])


def test_the_task_only_reads_and_never_execs():
    assert TASKS["health"]["readOnly"] is True
    assert all(v in ("get", "list") for (_, _, v) in granted())
    _, calls = run()
    assert all(c[1] in ("get", "-n") for c in calls if c[0] == "kubectl")
    assert not [c for c in calls if any(a in ("exec", "port-forward") for a in c)]
