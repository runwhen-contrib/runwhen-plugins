"""Unit tests for k8s-litellm-health's lib/ and scripts. Captured JSON and text come from the
`rw-litellm` namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-litellm-health.yaml): three stub proxies that answer LiteLLM's unauthenticated health
endpoints as v1.102.1 does (the real image needs more memory than the cluster has): `litellm`
(healthy, detailed readiness payload), `litellm-degraded` (200 with db "disconnected"; its Service
exposes 8080) and `litellm-draining` (every endpoint 503; not Ready). `readiness-public.json` is
the public payload the real v1.102.1 image returned. The scripts run through the SDK's test
context (`ctx.run` raises on a non-zero exit like the real runner; `stub_commands` matches the
exact kubectl argv, so an unexpected call fails)."""

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_litellm_health import fetch, health, k8s, project, proxy, service
from k8s_litellm_health.errors import RwError
from k8s_litellm_health.project import POD_FIELDS, WORKLOAD_FIELDS, Projection
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-litellm-health"
DATA = Path(__file__).parent / "data" / "k8s_litellm_health"
NS = "rw-litellm"
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
SETTINGS = {"maxItems": 200}
SCRIPTS = {"liveness": "liveness", "readiness": "readiness", "service": "service"}
OUTPUT = {"liveness": "liveness", "readiness": "readiness", "service": "service"}
NAMES = ("litellm", "litellm-degraded", "litellm-draining")
FORBIDDEN = (
    'Error from server (Forbidden): {r} is forbidden: User "u" cannot {v} resource "{r}" '
    'in API group "{g}" in the namespace "rw-litellm"'
)
UNREACHABLE = (
    "Error from server (ServiceUnavailable): error trying to reach service: "
    "dial tcp 10.244.0.5:4000: connect: connection refused"
)
UNAVAILABLE = (
    "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
)
NO_ROUTE = "Error from server (NotFound): the server could not find the requested resource"
PATHS = ("/health/liveliness", "/health/liveness", "/health/readiness")


def load(name):
    text = (DATA / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def reply_of(kind, name):
    """(rc, stdout, stderr) the pod proxy gave for `kind` (liveliness, liveness, readiness)."""
    doc = load(f"{kind}-{name}.json")
    return doc["rc"], doc["stdout"], doc["stderr"]


def script(task):
    spec = importlib.util.spec_from_file_location(
        f"litellm_{task}", SKILL / "scripts" / f"{SCRIPTS[task]}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def jsonpath(projection, many):
    return f"jsonpath={projection.template(many)}"


def pod_name(name):
    return POD_FIELDS.parse(load(f"pods-{name}.txt"), True)["items"][0]["metadata"]["name"]


def raw(name, path, scheme="http", port=4000):
    return (
        "kubectl",
        "get",
        "--raw",
        f"/api/v1/namespaces/{NS}/pods/{scheme}:{pod_name(name)}:{port}/proxy{path}",
        "--request-timeout=30s",
    )


def render(pods):
    """kubectl's jsonpath output (the projection's line format) for pod documents."""
    lines = []
    for pod in pods:
        status = pod.get("status", {})
        lines += [
            "@",
            f"s:metadata.name={pod['metadata']['name']}",
            "s:metadata.deletionTimestamp=",
        ]
        lines.append(f"s:status.phase={status['phase']}")
        lines.append(f"j:status.conditions={json.dumps(status.get('conditions', []))}")
        for port in pod["spec"]["containers"][0].get("ports", []):
            lines.append(f"p:{port.get('name', '')}={port['containerPort']}")
    return "\n".join(lines) + "\n"


def pods_of(name):
    return POD_FIELDS.parse(load(f"pods-{name}.txt"), True)["items"]


def stubs(
    name="litellm",
    fail=None,
    replies=None,
    pods=None,
    scheme="http",
    port=4000,
    services=None,
    slices=None,
    workload_text=None,
):
    """kubectl answers for a run on workload `name`. `fail` maps 'workload', 'pods', 'services' or
    'slices' to stderr; `replies` maps a path (or (pod, path)) to (rc, stdout, stderr)."""
    fail, replies = fail or {}, replies or {}
    out = {}
    base = ("kubectl", "-n", NS, "get")
    text = workload_text if workload_text is not None else load(f"workload-{name}.txt")
    out[(*base, "deployments.apps", name, "-o", jsonpath(WORKLOAD_FIELDS, False))] = (
        (1, "", fail["workload"]) if "workload" in fail else (0, text, "")
    )
    pod_text = load(f"pods-{name}.txt") if pods is None else render(pods)
    selector = f"app.kubernetes.io/name={name}"
    out[(*base, "pods", "-l", selector, "-o", jsonpath(POD_FIELDS, True))] = (
        (1, "", fail["pods"]) if "pods" in fail else (0, pod_text, "")
    )
    all_services = load("services.json") if services is None else services
    out[(*base, "services", "-o", "json")] = (
        (1, "", fail["services"]) if "services" in fail else (0, json.dumps(all_services), "")
    )
    labels = project.WORKLOAD_FIELDS.parse(text, False)["spec"]["template"]["metadata"]["labels"]
    names = sorted(
        s["metadata"]["name"] for s in all_services["items"] if service.selects(s, labels)
    )
    all_slices = load("endpointslices.json") if slices is None else slices
    if names:
        query = f"kubernetes.io/service-name in ({','.join(names)})"
        out[(*base, "endpointslices", "-l", query, "-o", "json")] = (
            (1, "", fail["slices"]) if "slices" in fail else (0, json.dumps(all_slices), "")
        )
    for pod in pods_of(name) if pods is None else pods:
        pname = pod["metadata"]["name"]
        for kind, path in (
            ("liveliness", PATHS[0]),
            ("liveness", PATHS[1]),
            ("readiness", PATHS[2]),
        ):
            argv = (
                "kubectl",
                "get",
                "--raw",
                f"/api/v1/namespaces/{NS}/pods/{scheme}:{pname}:{port}/proxy{path}",
                "--request-timeout=30s",
            )
            default = reply_of(kind, name)
            reply = replies.get((pname, path), replies.get(path, default))
            out[argv] = (1, "", fail["proxy"]) if "proxy" in fail else reply
    return out


def run(task, name="litellm", res=None, settings=None, inputs=None, **kw):
    """(envelope, calls) of one task on workload `name`."""
    resource = res or {
        "cluster": "kind",
        "namespace": NS,
        "name": name,
        "type": "kubernetes/deployment",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/deployments/{name}",
    }
    inputs = inputs or {}
    with make_context(resource=resource, settings=settings or SETTINGS) as tc:
        proxy_args = {k: inputs[k] for k in ("scheme", "port") if k in inputs}
        calls = stub_commands(tc, stubs(name, **proxy_args, **kw))
        env = run_main(script(task), tc, **inputs)
    return env, calls


def check_schemas(task, env):
    for key, value in env["outputs"].items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        jsonschema.validate(value, schema, cls=jsonschema.Draft202012Validator)


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def only_pod(env, task):
    [pod] = env["outputs"][OUTPUT[task]]["pods"]
    return pod


# ------------------------------------------------------------ lib ----


def test_alive_reply_is_never_true_for_unexpected_text():
    assert health.alive_reply(proxy.Reply(200, '"I\'m alive!"'), "w") == (True, "I'm alive!")
    assert health.alive_reply(proxy.Reply(503, ""), "w") == (False, None)
    for reply in (
        proxy.Reply(200, "<html>Sign in</html>"),
        proxy.Reply(200, '"ok"'),
        proxy.Reply(200, '{"status":"healthy"}'),
        proxy.Reply(200, ""),
        proxy.Reply(404, ""),
    ):
        with pytest.raises(RwError):
            health.alive_reply(reply, "w")


def test_readiness_doc_rejects_what_is_not_readiness_json():
    ok = proxy.Reply(200, json.dumps(load("readiness-public.json")))
    assert health.readiness_doc(ok, "w")["db"] == "Not connected"
    for body in (
        "<html><body>login</body></html>",
        "[]",
        '{"db":"connected"}',
        '{"status":1}',
        '{"status":"healthy","db":3}',
        '{"status":"healthy","litellm_version":1}',
        '{"status":"healthy","success_callbacks":"x"}',
        '{"status":"healthy","success_callbacks":[1]}',
        "",
    ):
        with pytest.raises(RwError) as exc:
            health.readiness_doc(proxy.Reply(200, body), "w")
        assert exc.value.code == "internal"


def test_cache_name_reads_a_type_or_a_semantic_cache_object():
    assert health.cache_name(None) is None
    assert health.cache_name("redis") == "redis"
    assert health.cache_name({"type": "redis-semantic", "index_info": "x"}) == "redis-semantic"


def test_selector_string_and_port():
    assert fetch.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    with pytest.raises(RwError):
        fetch.selector_string({})
    pod = pods_of("litellm")[0]
    assert fetch.pod_port(pod) == 4000 and fetch.pod_port(pod, 8080) == 8080
    pod["spec"]["containers"][0]["ports"] = [{"name": "litellm", "containerPort": 4400}]
    assert fetch.pod_port(pod) == 4400
    assert fetch.pod_port({"spec": {"containers": [{"ports": [{"containerPort": 1}]}]}}) == 4000


def test_check_inputs():
    for scheme, port in (("ftp", None), ("http", 0), ("http", "80"), ("http", True)):
        with pytest.raises(RwError) as exc:
            fetch.check_inputs(scheme, port)
        assert exc.value.code == "invalid_input"
    fetch.check_inputs("https", 4000)
    for bad in (0, 70000, "4000", True):
        with pytest.raises(RwError):
            service.check_service_port(bad)


def test_classify_and_proxy_get_tell_the_statuses_apart():
    assert k8s.classify(1, UNREACHABLE) == "unreachable"
    assert k8s.classify(1, FORBIDDEN.format(r="pods/proxy", v="get", g="")) == "permission_denied"
    assert k8s.classify(1, 'Error from server (NotFound): pods "x" not found') == "not_found"

    class Fake:
        def __init__(self, rc, out="", err=""):
            self.result = type("R", (), {"returncode": rc, "stdout": out, "stderr": err})

        def run(self, argv, check=True, **_):
            return self.result

    assert proxy.proxy_get(Fake(0, "ok"), "/x") == proxy.Reply(200, "ok")
    assert proxy.proxy_get(Fake(1, "", NO_ROUTE), "/x").status == 404
    assert proxy.proxy_get(Fake(1, "", UNAVAILABLE), "/x").status == 503
    with pytest.raises(RwError) as exc:
        proxy.proxy_get(Fake(1, "", UNREACHABLE), "/x")
    assert exc.value.code == "unreachable"


def test_reads_are_projected_so_no_env_value_leaves_the_cluster():
    _, calls = run("readiness")
    reads = [c for c in calls if "--raw" not in c]
    assert reads and all(c[-2] == "-o" and c[-1].startswith("jsonpath=") for c in reads)
    for projection in (WORKLOAD_FIELDS, POD_FIELDS):
        template = projection.template(True)
        assert "env" not in template and "annotations" not in template
    # the container ports are the only part of the containers that is read
    assert POD_FIELDS.template(False).count("spec.containers") == 1
    assert WORKLOAD_FIELDS.template(False).count("spec.template.spec.containers") == 1


def test_projection_parses_what_kubectl_prints_and_refuses_anything_else():
    [pod] = POD_FIELDS.parse(load("pods-litellm.txt"), True)["items"]
    assert pod["status"]["phase"] == "Running"
    assert pod["spec"]["containers"][0]["ports"] == [{"name": "http", "containerPort": 4000}]
    workload = WORKLOAD_FIELDS.parse(load("workload-litellm.txt"), False)
    assert workload["kind"] == "Deployment" and workload["spec"]["replicas"] == 1
    assert workload["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/name": "litellm"
    }
    assert workload["spec"]["template"]["spec"]["containers"][0]["ports"][0]["name"] == "http"
    assert Projection(strings=("a.b",)).parse("@\ns:a.b=\n", False) == {}
    for text in ("", "x\n", "@\nj:a=not json\n", "@\np:x=notaport\n", "s:a=1\n"):
        with pytest.raises(RwError) as exc:
            Projection(strings=("a",), values=("a",), ports=("spec", "containers")).parse(
                text, False
            )
        assert exc.value.code == "internal"


# ------------------------------------------------------------ scripts: liveness ----


def test_liveness_healthy():
    env, calls = run("liveness")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("liveness", env)
    out = env["outputs"]["liveness"]
    assert out["workload"] == {
        "kind": "Deployment",
        "name": "litellm",
        "replicas": 1,
        "readyReplicas": 1,
    }
    assert (out["aliveCount"], out["notAliveCount"]) == (1, 0)
    pod = only_pod(env, "liveness")
    assert pod["alive"] is True and pod["podReady"] is True and pod["port"] == 4000
    assert [(p["path"], p["served"], p["httpStatus"], p["alive"]) for p in pod["paths"]] == [
        ("/health/liveliness", True, 200, True),
        ("/health/liveness", True, 200, True),
    ]
    assert pod["paths"][0]["body"] == "I'm alive!"
    assert raw("litellm", "/health/liveliness") in calls


def test_liveness_503_is_not_alive_and_the_pod_is_not_ready_for_kubernetes_either():
    env, _ = run("liveness", name="litellm-draining")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("liveness", env)
    pod = only_pod(env, "liveness")
    assert pod["alive"] is False and pod["podReady"] is False
    assert [p["httpStatus"] for p in pod["paths"]] == [503, 503]
    assert env["outputs"]["liveness"]["notAliveCount"] == 1


def test_liveness_a_path_the_proxy_does_not_serve_is_a_fact_when_the_other_one_is():
    env, _ = run("liveness", replies={"/health/liveness": (1, "", NO_ROUTE)})
    assert env["status"] == "ok" and env["errors"] == []
    pod = only_pod(env, "liveness")
    assert pod["alive"] is True
    assert (pod["paths"][1]["served"], pod["paths"][1]["alive"]) == (False, None)


def test_liveness_neither_path_served_is_a_dependency_error_never_alive():
    replies = {p: (1, "", NO_ROUTE) for p in PATHS[:2]}
    env, _ = run("liveness", replies=replies)
    assert env["status"] == "failed" and codes(env) == [
        ("dependency", "pod/litellm-86bdbc9c6f-xh5wd")
    ]
    assert env["outputs"] == {}


def test_liveness_a_reply_that_is_not_the_alive_text_is_an_error_never_alive():
    env, _ = run("liveness", replies={p: (0, "<html>login</html>", "") for p in PATHS[:2]})
    assert env["status"] == "failed" and codes(env)[0][0] == "internal"
    assert env["outputs"] == {}


def test_liveness_one_bad_path_keeps_what_the_other_said():
    replies = {"/health/liveliness": (1, "", UNAVAILABLE), "/health/liveness": (0, "<html>", "")}
    env, _ = run("liveness", replies=replies)
    assert env["status"] == "partial" and codes(env) == [
        ("internal", "pod/litellm-86bdbc9c6f-xh5wd")
    ]
    check_schemas("liveness", env)
    pod = only_pod(env, "liveness")
    assert pod["alive"] is False  # the 503 is a fact; the bad answer is the error
    assert [p["alive"] for p in pod["paths"]] == [False, None]
    replies = {
        "/health/liveliness": (0, '"I\'m alive!"', ""),
        "/health/liveness": (0, "<html>", ""),
    }
    env, _ = run("liveness", replies=replies)
    assert only_pod(env, "liveness")["alive"] is None  # alive on one path, unknown on the other


def test_liveness_one_dead_pod_is_one_error_and_the_other_still_reports():
    second = pods_of("litellm")[0]
    first = json.loads(json.dumps(second))
    second["metadata"]["name"] = "litellm-zzz"
    unreachable = {(second["metadata"]["name"], p): (1, "", UNREACHABLE) for p in PATHS}
    env, _ = run("liveness", pods=[first, second], replies=unreachable)
    assert env["status"] == "partial" and codes(env) == [("unreachable", "pod/litellm-zzz")]
    rows = {p["name"]: p for p in env["outputs"]["liveness"]["pods"]}
    assert (
        rows["litellm-86bdbc9c6f-xh5wd"]["alive"] is True and rows["litellm-zzz"]["alive"] is None
    )
    assert env["outputs"]["liveness"]["aliveCount"] == 1
    check_schemas("liveness", env)


def test_liveness_pending_pod_is_listed_and_not_asked_and_no_pods_is_a_fact():
    pod = pods_of("litellm")[0]
    pod["status"]["phase"] = "Pending"
    env, calls = run("liveness", pods=[pod])
    row = only_pod(env, "liveness")
    assert row["queried"] is False and row["alive"] is None and row["paths"] == []
    assert not [c for c in calls if "--raw" in c]
    check_schemas("liveness", env)
    env, _ = run("liveness", pods=[])
    assert env["status"] == "ok" and env["outputs"]["liveness"]["podCount"] == 0


# ------------------------------------------------------------ scripts: readiness ----


def test_readiness_healthy_detailed_payload():
    env, calls = run("readiness")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("readiness", env)
    pod = only_pod(env, "readiness")
    assert (pod["ready"], pod["httpStatus"], pod["status"], pod["db"]) == (
        True,
        200,
        "healthy",
        "connected",
    )
    assert pod["cache"] == "redis" and pod["litellmVersion"] == "1.102.1"
    assert pod["detailed"] is True and pod["successCallbacks"][-1] == "prometheus"
    assert (
        env["outputs"]["readiness"]["readyCount"],
        env["outputs"]["readiness"]["notReadyCount"],
    ) == (1, 0)
    assert raw("litellm", "/health/readiness") in calls


def test_readiness_db_disconnected_is_a_fact_while_the_proxy_still_answers_200():
    env, _ = run("readiness", name="litellm-degraded")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("readiness", env)
    pod = only_pod(env, "readiness")
    assert pod["ready"] is True and pod["db"] == "disconnected" and pod["cache"] is None
    assert pod["litellmVersion"] == "1.102.1"


def test_readiness_503_is_not_ready_not_an_error():
    env, _ = run("readiness", name="litellm-draining")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("readiness", env)
    pod = only_pod(env, "readiness")
    assert pod["ready"] is False and pod["httpStatus"] == 503 and pod["podReady"] is False
    assert pod["db"] is None and pod["status"] is None  # the proxy does not pass the body on
    assert env["outputs"]["readiness"]["notReadyCount"] == 1


def test_readiness_the_public_payload_has_status_and_db_only():
    """What the real v1.102.1 image answers without allow_public_health_readiness_details."""
    public = (0, (DATA / "readiness-public.json").read_text(), "")
    env, _ = run("readiness", replies={"/health/readiness": public})
    assert env["status"] == "ok"
    check_schemas("readiness", env)
    pod = only_pod(env, "readiness")
    assert (pod["ready"], pod["db"], pod["detailed"]) == (True, "Not connected", False)
    assert (
        pod["cache"] is None and pod["litellmVersion"] is None and pod["successCallbacks"] is None
    )


def test_readiness_a_status_that_is_not_healthy_is_not_ready():
    body = (0, json.dumps({"status": "unhealthy", "db": "connected"}), "")
    env, _ = run("readiness", replies={"/health/readiness": body})
    pod = only_pod(env, "readiness")
    assert pod["ready"] is False and pod["status"] == "unhealthy" and pod["httpStatus"] == 200


def test_readiness_a_reply_that_is_not_readiness_json_is_an_error_never_ready():
    env, _ = run("readiness", replies={"/health/readiness": (0, "<html>login</html>", "")})
    assert env["status"] == "failed" and codes(env) == [
        ("internal", "pod/litellm-86bdbc9c6f-xh5wd")
    ]
    assert env["outputs"] == {}
    env, _ = run("readiness", replies={"/health/readiness": (1, "", NO_ROUTE)})
    assert env["status"] == "failed" and codes(env)[0][0] == "dependency"


def test_readiness_a_semantic_cache_object_and_long_values_are_bounded():
    doc = {
        "status": "healthy",
        "db": "x" * 3000,
        "cache": {"type": "redis-semantic", "index_info": {"huge": "y" * 5000}},
        "litellm_version": "1.2.3",
        "success_callbacks": [f"cb{i}" for i in range(80)],
    }
    env, _ = run("readiness", replies={"/health/readiness": (0, json.dumps(doc), "")})
    check_schemas("readiness", env)
    pod = only_pod(env, "readiness")
    assert pod["cache"] == "redis-semantic" and "huge" not in json.dumps(env["outputs"])
    assert len(pod["db"]) == 1024 and pod["truncated"] is True
    assert len(pod["successCallbacks"]) == 50 and pod["successCallbacksTruncated"] is True


def test_readiness_one_dead_pod_is_one_error_and_the_other_still_reports():
    second = pods_of("litellm")[0]
    first = json.loads(json.dumps(second))
    second["metadata"]["name"] = "litellm-zzz"
    env, _ = run(
        "readiness",
        pods=[first, second],
        replies={("litellm-zzz", "/health/readiness"): (1, "", UNREACHABLE)},
    )
    assert env["status"] == "partial" and codes(env) == [("unreachable", "pod/litellm-zzz")]
    rows = {p["name"]: p for p in env["outputs"]["readiness"]["pods"]}
    assert (
        rows["litellm-86bdbc9c6f-xh5wd"]["ready"] is True and rows["litellm-zzz"]["ready"] is None
    )
    check_schemas("readiness", env)


def test_readiness_inputs_choose_the_port_and_scheme():
    env, calls = run("readiness", inputs={"port": 8080, "scheme": "https"})
    assert env["status"] == "ok"
    assert raw("litellm", "/health/readiness", "https", 8080) in calls


def test_the_skill_never_reads_secrets_execs_or_calls_a_keyed_endpoint():
    for task in ("liveness", "readiness", "service"):
        for name in NAMES:
            _, calls = run(task, name)
            for argv in calls:
                text = " ".join(argv)
                assert "secret" not in text and "exec" not in argv and "port-forward" not in argv
                assert "/model/info" not in text and "/v1/models" not in text
                if "--raw" in argv:
                    assert argv[argv.index("--raw") + 1].rsplit("/proxy", 1)[1] in PATHS


# ------------------------------------------------------------ scripts: service ----


def service_row(env, name):
    return next(s for s in env["outputs"]["service"]["services"] if s["name"] == name)


def test_service_healthy_selects_the_workload_with_a_ready_endpoint():
    env, calls = run("service")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("service", env)
    out = env["outputs"]["service"]
    assert out["servicePort"] == 4000 and out["serviceCount"] == 1
    assert out["containerPorts"] == [{"name": "http", "port": 4000}]
    row = service_row(env, "litellm")
    assert row["exposesPort"] is True and row["headless"] is False
    assert row["ports"] == [
        {
            "name": "http",
            "port": 4000,
            "targetPort": "http",
            "protocol": "TCP",
            "targetPortDeclared": True,
        }
    ]
    assert row["endpoints"]["readyEndpoints"] == 1 and row["endpoints"]["notReadyEndpoints"] == 0
    assert row["endpoints"]["ports"] == [{"name": "http", "port": 4000, "protocol": "TCP"}]
    literal = (
        "kubectl",
        "-n",
        NS,
        "get",
        "endpointslices",
        "-l",
        "kubernetes.io/service-name in (litellm)",
        "-o",
        "json",
    )
    assert literal in calls


def test_service_not_ready_endpoint_and_a_service_port_that_is_not_the_expected_one():
    env, _ = run("service", name="litellm-draining")
    row = service_row(env, "litellm-draining")
    assert row["endpoints"]["readyEndpoints"] == 0 and row["endpoints"]["notReadyEndpoints"] == 1
    env, _ = run("service", name="litellm-degraded")
    row = service_row(env, "litellm-degraded")
    assert row["exposesPort"] is False  # the Service is on 8080 and the proxy is expected on 4000
    assert row["ports"][0]["port"] == 8080 and row["ports"][0]["targetPort"] == 4000
    assert row["endpoints"]["readyEndpoints"] == 1
    env, _ = run("service", name="litellm-degraded", inputs={"service_port": 8080})
    assert service_row(env, "litellm-degraded")["exposesPort"] is True
    assert env["outputs"]["service"]["servicePort"] == 8080


def test_service_a_target_port_name_the_template_does_not_declare():
    doc = load("services.json")
    svc = next(s for s in doc["items"] if s["metadata"]["name"] == "litellm")
    svc["spec"]["ports"][0]["targetPort"] = "web"
    env, _ = run("service", services=doc)
    port = service_row(env, "litellm")["ports"][0]
    assert port["targetPort"] == "web" and port["targetPortDeclared"] is False
    svc["spec"]["ports"][0]["targetPort"] = 9999  # an undeclared number is legal, still a fact
    env, _ = run("service", services=doc)
    assert service_row(env, "litellm")["ports"][0]["targetPortDeclared"] is False


def test_service_selects_only_services_whose_selector_is_in_the_template_labels():
    doc = load("services.json")
    svc = next(s for s in doc["items"] if s["metadata"]["name"] == "litellm")
    extra = json.loads(json.dumps(svc))
    extra["metadata"]["name"] = "narrower"
    extra["spec"]["selector"]["tier"] = "x"
    empty = json.loads(json.dumps(svc))
    empty["metadata"]["name"] = "nosel"
    empty["spec"]["selector"] = {}
    broader = json.loads(json.dumps(svc))
    broader["metadata"]["name"] = "broader"
    doc["items"] += [extra, empty, broader]
    env, _ = run("service", services=doc)
    assert [s["name"] for s in env["outputs"]["service"]["services"]] == ["broader", "litellm"]
    assert env["outputs"]["service"]["serviceCount"] == 2


def test_service_no_selecting_service_is_a_fact_and_asks_for_no_endpoint_slices():
    doc = load("services.json")
    doc["items"] = [s for s in doc["items"] if s["metadata"]["name"] != "litellm"]
    env, calls = run("service", services=doc)
    assert env["status"] == "ok" and env["outputs"]["service"]["services"] == []
    assert not [c for c in calls if "endpointslices" in c]
    check_schemas("service", env)


def test_service_forbidden_endpoint_slices_keep_the_services_and_report_it():
    env, _ = run(
        "service",
        fail={"slices": FORBIDDEN.format(r="endpointslices", v="list", g="discovery.k8s.io")},
    )
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "endpointslices")]
    check_schemas("service", env)
    assert service_row(env, "litellm")["endpoints"] is None
    assert service_row(env, "litellm")["exposesPort"] is True


def test_service_endpoint_ready_condition_unset_counts_as_ready():
    doc = load("endpointslices.json")
    for item in doc["items"]:
        for endpoint in item["endpoints"]:
            endpoint["conditions"] = {}
    env, _ = run("service", name="litellm-draining", slices=doc)
    assert service_row(env, "litellm-draining")["endpoints"]["readyEndpoints"] == 1


def test_service_bad_port_is_invalid_input_before_any_call():
    for bad in (0, 70000):
        env, calls = run("service", inputs={"service_port": bad})
        assert env["status"] == "failed" and codes(env) == [("invalid_input", None)]


def test_service_forbidden_services_list_fails():
    env, _ = run("service", fail={"services": FORBIDDEN.format(r="services", v="list", g="")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]


# ------------------------------------------------------------ failures common to all ----


@pytest.mark.parametrize("task", list(SCRIPTS))
@pytest.mark.parametrize("name", NAMES)
def test_not_found_forbidden_and_unreachable_workload(task, name):
    n = "deployments.apps"
    env, _ = run(
        task, name, fail={"workload": f'Error from server (NotFound): {n} "{name}" not found'}
    )
    assert env["status"] == "failed" and codes(env) == [("not_found", None)]
    env, _ = run(task, name, fail={"workload": FORBIDDEN.format(r=n, v="get", g="apps")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(
        task, name, fail={"workload": "Unable to connect to the server: dial tcp: i/o timeout"}
    )
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


@pytest.mark.parametrize("task", ["liveness", "readiness"])
def test_forbidden_pods_list_and_forbidden_proxy_fail_with_permission_denied(task):
    env, _ = run(task, fail={"pods": FORBIDDEN.format(r="pods", v="list", g="")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(task, fail={"proxy": FORBIDDEN.format(r="pods/proxy", v="get", g="")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]


@pytest.mark.parametrize("task", ["liveness", "readiness"])
def test_every_pod_failing_fails_the_task(task):
    env, _ = run(task, fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env) == [
        ("unreachable", "pod/litellm-86bdbc9c6f-xh5wd")
    ]


@pytest.mark.parametrize("task", list(SCRIPTS))
def test_a_target_of_another_type_is_invalid_input_and_the_path_gives_the_type(task):
    res = {"cluster": "kind", "namespace": NS, "name": "x", "type": "kubernetes/daemonset"}
    env, _ = run(task, res=res)
    assert env["status"] == "failed" and codes(env) == [("invalid_input", None)]
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "litellm",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/deployments/litellm",
    }
    env, _ = run(task, res=res)
    assert env["status"] == "ok"


@pytest.mark.parametrize("inputs", [{"scheme": "ftp"}, {"port": 0}, {"port": 70000}])
@pytest.mark.parametrize("task", ["liveness", "readiness"])
def test_bad_input_is_invalid_input_before_any_call(task, inputs):
    resource = {
        "cluster": "kind",
        "namespace": NS,
        "name": "litellm",
        "type": "kubernetes/deployment",
    }
    with make_context(resource=resource, settings=SETTINGS) as tc:
        calls = stub_commands(tc, {})
        env = run_main(script(task), tc, **inputs)
    assert env["status"] == "failed" and codes(env) == [("invalid_input", None)] and calls == []


# ------------------------------------------------------------ deadline ----


class Clocked:
    """A ctx whose `kubectl get --raw` calls each take `step` seconds of a fake clock."""

    def __init__(self, answer, step=30):
        self.now = 0.0
        self.step = step
        self.answer = answer
        self.calls = []
        self.errors = []

    def clock(self):
        return self.now

    def run(self, argv, check=True, **_):
        self.calls.append(list(argv))
        rc, out, err = self.answer(len(self.calls) - 1, argv)
        self.now += self.step
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})

    def error(self, code, message, scope=None, **_):
        self.errors.append((code, scope, message))


WORKLOAD = {"kind": "Deployment", "metadata": {"name": "litellm", "namespace": NS}, "spec": {}}
TIMEOUT = "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)"


def pods(n):
    return [
        {"metadata": {"name": f"litellm-{i}"}, "status": {"phase": "Running"}, "spec": {}}
        for i in range(n)
    ]


def test_readiness_the_task_deadline_leaves_the_remaining_pods_a_timeout_error():
    ready = (0, json.dumps({"status": "healthy", "db": "connected"}), "")
    ctx = Clocked(lambda i, argv: ready)
    budget = fetch.Budget(clock=ctx.clock)
    out = health.view(ctx, "readiness", WORKLOAD, pods(6), "http", None, 200, budget)
    # 0s, 30s, 60s, 90s: four asks; the fourth is limited to what is left of the 100s
    assert out["readyCount"] == 4
    assert [c[4] for c in ctx.calls] == [f"--request-timeout={t}s" for t in (30, 30, 30, 10)]
    assert [(c, s) for c, s, _ in ctx.errors] == [
        ("timeout", "pod/litellm-4"),
        ("timeout", "pod/litellm-5"),
    ]
    assert "100s deadline" in ctx.errors[0][2]
    assert [p["ready"] for p in out["pods"][4:]] == [None, None]


def test_liveness_three_consecutive_timeouts_stop_asking():
    alive = (0, '"I\'m alive!"', "")
    ctx = Clocked(lambda i, argv: alive if i < 2 else (1, "", TIMEOUT), step=1)
    budget = fetch.Budget(clock=ctx.clock)
    out = health.view(ctx, "liveness", WORKLOAD, pods(5), "http", None, 200, budget)
    # pod 0 answers both paths; pod 1 times out twice and pod 2's first ask is the third: from
    # then on nothing is asked
    assert out["aliveCount"] == 1 and len(ctx.calls) == 5
    assert {c for c, _, _ in ctx.errors} == {"timeout"}
    assert any("consecutive" in m for _, _, m in ctx.errors)


# ------------------------------------------------------------ permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "deployments.apps": ("apps", "deployments"),
    "pods": ("", "pods"),
    "services": ("", "services"),
    "endpointslices": ("discovery.k8s.io", "endpointslices"),
}
VALUE_FLAGS = {"-n", "-o", "-l"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs. Raises on anything it does
    not know, so a script that issues a new call has to extend this."""
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
    for name in NAMES:
        _, calls = run(task, name)
        for argv in calls:
            needed |= rbac_needs(list(argv))
    # a StatefulSet target is read with `get statefulsets`: the rule names both workload kinds
    needed.add(("apps", "statefulsets", "get"))
    assert needed == granted(task), (
        f"{task}: calls {sorted(needed)}, declares {sorted(granted(task))}"
    )


def test_a_statefulset_target_is_read_as_a_statefulset():
    text = load("workload-litellm.txt").replace("s:kind=Deployment", "s:kind=StatefulSet")
    resource = {
        "cluster": "kind",
        "namespace": NS,
        "name": "litellm",
        "type": "kubernetes/statefulset",
    }
    table = stubs("litellm", workload_text=text)
    workload = table.pop(
        (
            "kubectl",
            "-n",
            NS,
            "get",
            "deployments.apps",
            "litellm",
            "-o",
            jsonpath(WORKLOAD_FIELDS, False),
        )
    )
    table[
        (
            "kubectl",
            "-n",
            NS,
            "get",
            "statefulsets.apps",
            "litellm",
            "-o",
            jsonpath(WORKLOAD_FIELDS, False),
        )
    ] = workload
    with make_context(resource=resource, settings=SETTINGS) as tc:
        stub_commands(tc, table)
        env = run_main(script("readiness"), tc)
    assert (
        env["status"] == "ok" and env["outputs"]["readiness"]["workload"]["kind"] == "StatefulSet"
    )


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "deployments.apps", "l", "-o", "json"]) == {
        ("apps", "deployments", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "services", "-o", "json"]) == {
        ("", "services", "list")
    }
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "endpointslices", "-l", "a=b", "-o", "json"]
    ) == {("discovery.k8s.io", "endpointslices", "list")}
    path = "/api/v1/namespaces/n/pods/http:p:4000/proxy/health/readiness"
    assert rbac_needs(["kubectl", "get", "--raw", path]) == {("", "pods/proxy", "get")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "-n", "x", "delete", "pods", "p"])
