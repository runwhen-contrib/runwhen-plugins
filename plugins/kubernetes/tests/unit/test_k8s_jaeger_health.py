"""Unit tests for k8s-jaeger-health's lib/ and script. Captured JSON comes from the `rw-jaeger`
namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-jaeger-health.yaml): a real jaegertracing/all-in-one with in-memory storage and spans sent
over OTLP/HTTP, 5xx server spans with OpenTelemetry's error status (Jaeger's tag error=true):
`checkout` (POST /pay 500 x4 and 503 x2, GET /cart 404 x3, a reset path with an id 404 x2, outbound
POST payments 503 x2 of span.kind client), `catalog` (404s only), `gateway` (502 x2 on the newer
http.response.status_code key, 404 x2 with http.route) and `inventory` (507 x3, a 5xx no default
code lists). `traces-<service>.json` is everything Jaeger held for the service (spans cut to the
fields the skill reads); the tests answer each tag query the way Jaeger does (a trace matches when
a span's tag equals the value as a string, a bool as true/false). The script runs through the SDK's test context (`ctx.run` raises on a non-zero exit like
the real runner; `stub_commands` matches the exact kubectl argv, so an unexpected call fails). The pod
proxy reads go through `ctx.k8s_proxy_get`, stood in for by `proxy_fake.ProxyFake` (a stub written
as kubectl's (rc, stdout, stderr) is turned into the helper's answer by `served_as`)."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest
import yaml
from k8s_jaeger_health import fetch, http_errors, k8s, proxy, traces
from k8s_jaeger_health.common import parse_duration
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.project import POD_FIELDS, WORKLOAD_FIELDS, Projection
from proxy_fake import ProxyFake, api_error, cross_host_redirect, reply
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-jaeger-health"
DATA = Path(__file__).parent / "data" / "k8s_jaeger_health"
UTC = timezone.utc
NS = "rw-jaeger"
POD = "jaeger-f6bd9dd68-j59qx"
CAPTURED = datetime(2026, 11, 4, 0, 14, 0, tzinfo=UTC)  # just after the data was captured
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
CODES = [400, 401, 403, 404, 429]
SETTINGS = {"maxItems": 50, "statusCodes": CODES}
RESOURCE = {
    "cluster": "kind",
    "namespace": NS,
    "name": "jaeger",
    "type": "kubernetes/deployment",
    "path": f"kubernetes/clusters/kind/namespaces/{NS}/deployments/jaeger",
}
FORBIDDEN = (
    'Error from server (Forbidden): {r} is forbidden: User "u" cannot {v} resource "{r}" '
    'in API group "" in the namespace "rw-jaeger"'
)
UNREACHABLE = (
    "Error from server (ServiceUnavailable): error trying to reach service: "
    "dial tcp 10.244.0.5:16686: connect: connection refused"
)
# what a test writes as (rc, stdout, stderr) for the workload's own 503 / 404
UNAVAILABLE = (1, "", "backend 503")
NO_ROUTE = (1, "", "backend 404")
SERVICES = ("catalog", "checkout", "gateway", "inventory", "jaeger-all-in-one")  # last: no spans
EMPTY = {"data": [], "total": 0, "limit": 0, "offset": 0, "errors": None}


def load(name):
    return json.loads((DATA / name).read_text())


def jsonpath(projection, many):
    return f"jsonpath={projection.template(many)}"


def base_pod():
    """The captured Jaeger pod, parsed from the recorded jsonpath output."""
    return POD_FIELDS.parse((DATA / "pods.txt").read_text(), True)["items"][0]


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
        for container in pod["spec"]["containers"]:
            lines.append(f"c:{container.get('name', '')}={container.get('image', '')}")
            for port in container.get("ports", []):
                lines.append(f"p:{port.get('name', '')}={port['containerPort']}")
    return "\n".join(lines) + "\n"


def trace_doc(service):
    """Everything Jaeger held for `service` (nothing for jaeger's own service)."""
    path = DATA / f"traces-{service}.json"
    return json.loads(path.read_text()) if path.exists() else dict(EMPTY)


def script():
    spec = importlib.util.spec_from_file_location(
        "jaeger_http_errors", SKILL / "scripts" / "http_errors.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def micros(stamp):
    return http_errors.micros(stamp)


def raw(path, scheme="http", port=16686):
    """The key of one proxied request: a `ctx.k8s_proxy_get` call, recorded in `calls` as this tuple."""
    return ("k8s_proxy_get", NS, POD, str(port), path, scheme)


def served_as(answer):
    """The ctx.k8s_proxy_get answer for a test's (rc, stdout, stderr): a body for 0, the workload's
    503 / 404 for UNAVAILABLE / NO_ROUTE, a dict as it is, else the API-server error the stderr names."""
    if isinstance(answer, dict):
        return answer
    rc, out, err = answer
    if rc == 0:
        return out
    if answer == UNAVAILABLE:
        return reply(503, "service unavailable")
    if answer == NO_ROUTE:
        return reply(404, "404 page not found")
    return api_error(err, k8s.classify)


def first_start(trace):
    return min(s["startTime"] for s in trace["spans"])


def tag_text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def tag_matches(trace, key, code):
    return any(
        t["key"] == key and tag_text(t["value"]) == str(code)
        for span in trace["spans"]
        for t in span["tags"]
    )


def jaeger_answer(doc, start, end, limit, key, code):
    """What Jaeger returns for one tag query: the traces in the window with a span whose tag `key`
    equals `code` as a string, newest first, at most `limit`."""
    hits = [t for t in doc["data"] if tag_matches(t, key, code) and start <= first_start(t) <= end]
    hits.sort(key=lambda t: -first_start(t))
    return {**doc, "data": hits[:limit]}


def stubs(
    now,
    since,
    limit,
    codes=CODES,
    fail=None,
    replies=None,
    docs=None,
    pods=None,
    answer_all=None,
    **kw,
):
    """kubectl answers for a run. `fail` maps 'workload', 'pods' or 'proxy' to stderr; `replies`
    maps a proxy path, or (service, key, code), to (rc, stdout, stderr); `docs` replaces a
    service's trace document; `answer_all` answers every traces query the same way."""
    fail, replies, docs = fail or {}, replies or {}, docs or {}
    scheme, port = kw.get("scheme", "http"), kw.get("port", 16686)
    out = {}
    plural = "statefulsets.apps" if kw.get("kind") == "statefulset" else "deployments.apps"
    workload = (
        "kubectl",
        "-n",
        NS,
        "get",
        plural,
        "jaeger",
        "-o",
        jsonpath(WORKLOAD_FIELDS, False),
    )
    text = (DATA / "workload.txt").read_text()
    if kw.get("kind") == "statefulset":
        text = text.replace("s:kind=Deployment", "s:kind=StatefulSet")
    out[workload] = (1, "", fail["workload"]) if "workload" in fail else (0, text, "")
    selector = "app=jaeger,app.kubernetes.io/component=all-in-one"
    pod_text = (DATA / "pods.txt").read_text() if pods is None else render(pods)
    out[("kubectl", "-n", NS, "get", "pods", "-l", selector, "-o", jsonpath(POD_FIELDS, True))] = (
        (1, "", fail["pods"]) if "pods" in fail else (0, pod_text, "")
    )
    start, end = micros(now - since), micros(now)

    def put(path, default, key=None):
        reply = replies.get(key, replies.get(path, default))
        out[raw(path, scheme, port)] = (1, "", fail["proxy"]) if "proxy" in fail else reply

    put("/api/services", (0, json.dumps(load("services.json")), ""))
    searches = [traces.ERROR_TAG] + [(k, c) for k in traces.STATUS_KEYS for c in codes]
    for service in (*SERVICES, "x"):
        for key, code in searches:
            path = traces.traces_path(service, start, end, limit, key, code)
            if answer_all is not None:
                default = answer_all
            elif service in SERVICES:
                doc = docs.get(service) or trace_doc(service)
                default = (0, json.dumps(jaeger_answer(doc, start, end, limit, key, code)), "")
            else:
                default = (0, json.dumps(EMPTY), "")
            put(path, default, (service, key, code))
    return out


def run(now=CAPTURED, res=None, settings=None, inputs=None, **kw):
    """(envelope, calls) of one run."""
    inputs = inputs or {}
    since = parse_duration(inputs.get("since", "1h"))
    with make_context(resource=res or RESOURCE, settings=settings or SETTINGS, now=now) as tc:
        table = stubs(
            now,
            since,
            inputs.get("limit", 100),
            codes=(settings or SETTINGS)["statusCodes"],
            **inputs_of(inputs),
            **kw,
        )
        calls = stub_commands(tc, {k: v for k, v in table.items() if k[0] == "kubectl"})

        def answer(request):
            key = ("k8s_proxy_get", request["namespace"], request["name"], str(request["port"]))
            key += (request["path"], request["scheme"])
            calls.append(key)
            assert key in table, f"ctx.k8s_proxy_get called with an unstubbed request {key!r}"
            return served_as(table[key])

        tc.ctx.k8s_proxy_get = ProxyFake(answer)
        env = run_main(script(), tc, **inputs)
    return env, calls


def inputs_of(inputs):
    return {k: inputs[k] for k in ("scheme", "port") if k in inputs}


def check_schema(env):
    schema = json.loads(
        (SKILL / TASKS["http-errors"]["outputs"]["httpErrors"]["schema"]).read_text()
    )
    jsonschema.validate(env["outputs"]["httpErrors"], schema, cls=jsonschema.Draft202012Validator)


def codes_of(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def service_row(env, name):
    return next(s for s in env["outputs"]["httpErrors"]["services"] if s["name"] == name)


def operation(env, service, name):
    return next(o for o in service_row(env, service)["operations"] if o["operation"] == name)


def span_tags(status, error=None, **extra):
    """A server span's tags; like OpenTelemetry's, a 5xx one carries error=true."""
    tags = [{"key": "http.status_code", "type": "int64", "value": status}]
    if status >= 500 if error is None else error:
        tags.append({"key": "error", "type": "bool", "value": True})
    return tags + [{"key": k, "type": "string", "value": v} for k, v in extra.items()]


# ------------------------------------------------------------ lib ----


def test_traces_path_is_one_exact_tag_query_in_microseconds():
    path = traces.traces_path("check out", 1000, 2000, 100, "http.status_code", 500)
    parts = urlsplit(path)
    query = parse_qs(parts.query)
    assert parts.path == "/api/traces"
    assert query["service"] == ["check out"] and query["start"] == ["1000"]
    assert query["end"] == ["2000"] and query["limit"] == ["100"]
    assert json.loads(query["tags"][0]) == {"http.status_code": "500"}
    assert " " not in path and "{" not in path  # percent-encoded: a plain request path


def test_the_window_is_microseconds_before_the_reference_time():
    assert micros(CAPTURED) == 1793751240000000
    env, calls = run()
    start, end = micros(CAPTURED - timedelta(hours=1)), micros(CAPTURED)
    literal = (
        f"/api/traces?service=checkout&start={start}&end={end}&limit=100"
        "&tags=%7B%22error%22%3A%22true%22%7D"
    )
    assert raw(literal) in calls
    exact = literal.replace("%22error%22%3A%22true%22", "%22http.status_code%22%3A%22404%22")
    assert raw(exact) in calls
    assert env["outputs"]["httpErrors"]["window"] == {
        "since": 3600,
        "start": "2026-11-03T23:14:00Z",
        "end": "2026-11-04T00:14:00Z",
        "startMicros": start,
        "endMicros": end,
    }


def test_status_of_reads_an_int_or_digit_string_on_either_key_and_nothing_else():
    assert traces.status_of({"http.status_code": 503}) == 503
    assert traces.status_of({"http.response.status_code": "404"}) == 404
    assert traces.status_of({"http.status_code": True}) is None
    assert traces.status_of({"http.status_code": "ok"}) is None
    assert traces.status_of({"rpc.grpc.status_code": 2}) is None
    assert traces.status_of({}) is None


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"http.url": "http://checkout:8080/cart/items?token=abc#frag"}, "/cart/items"),
        ({"url.full": "https://h.example/a/b?x=1"}, "/a/b"),
        ({"http.url": "http://checkout:8080"}, "/"),
        ({"http.target": "/items/9?debug=1"}, "/items/{id}"),
        ({"url.path": "/api", "http.target": "/other?x"}, "/api"),
        ({"http.route": "/items/{id}"}, "/items/{id}"),
        # the route (a template) wins over the raw path, which may hold ids
        ({"http.route": "/users/{id}", "url.path": "/users/42"}, "/users/{id}"),
        ({"http.method": "GET"}, None),
    ],
)
def test_path_of_never_carries_a_query_string(tags, expected):
    assert traces.path_of(tags) == expected


def test_query_json_rejects_what_is_not_jaeger_json():
    ok = proxy.Reply(200, json.dumps({"data": [], "errors": None}))
    assert traces.query_json(ok, "w")["data"] == []
    for body in (
        "<!doctype html><html><title>Jaeger UI</title></html>",
        "[]",
        '{"x":1}',
        "",
        json.dumps({"data": None, "errors": [{"code": 400, "msg": "bad"}]}),
    ):
        with pytest.raises(RwError) as exc:
            traces.query_json(proxy.Reply(200, body), "w")
        assert exc.value.code == "internal"
    for status in (404, 503):
        with pytest.raises(RwError) as exc:
            traces.query_json(proxy.Reply(status, ""), "w")
        assert exc.value.code == "dependency"


def test_service_names_and_trace_list_check_their_shape():
    assert traces.service_names(proxy.Reply(200, '{"data":["b","a","a"]}'), "w") == ["a", "b"]
    assert traces.service_names(proxy.Reply(200, '{"data":null}'), "w") == []
    for body in ('{"data":[1]}', '{"data":"x"}'):
        with pytest.raises(RwError):
            traces.service_names(proxy.Reply(200, body), "w")
    for body in ('{"data":{}}', '{"data":[{"spans":[]}]}', '{"data":["x"]}'):
        with pytest.raises(RwError):
            traces.trace_list(proxy.Reply(200, body), "w")


def test_select_filters_and_reports_unknown_names():
    names = ["a", "b", "c"]
    assert http_errors.select(names, [], []) == (["a", "b", "c"], [], [])
    assert http_errors.select(names, ["a", "z"], []) == (["a"], ["z"], [])
    assert http_errors.select(names, [], ["b", "z"]) == (["a", "c"], [], ["b"])
    assert http_errors.select(names, ["a", "b"], ["b"]) == (["a"], [], ["b"])


def test_selector_string_and_port():
    assert fetch.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    with pytest.raises(RwError):
        fetch.selector_string({})
    pod = base_pod()
    assert fetch.pod_port(pod) == 16686 and fetch.pod_port(pod, 8080) == 8080
    pod["spec"]["containers"][0]["ports"] = [{"name": "http-query", "containerPort": 9000}]
    assert fetch.pod_port(pod) == 9000
    assert fetch.pod_port({"spec": {"containers": [{"ports": [{"containerPort": 1}]}]}}) == 16686


def two_containers(sidecar_first):
    """A pod with Jaeger and an injected sidecar that also has a port called `query`."""
    sidecar = {
        "name": "istio-proxy",
        "image": "istio/proxyv2:1.24",
        "ports": [{"name": "query", "containerPort": 9999}],
    }
    jaeger = {"name": "jaeger", "image": "jaegertracing/all-in-one:1.76.0", "ports": []}
    pod = base_pod()
    pod["spec"]["containers"] = [sidecar, jaeger] if sidecar_first else [jaeger, sidecar]
    return pod, jaeger


def test_the_port_comes_from_the_main_container_and_the_input_overrides_it():
    for sidecar_first in (True, False):
        pod, jaeger = two_containers(sidecar_first)
        assert fetch.main_container(pod) is jaeger
        # Jaeger's container declares no port: the sidecar's `query` port is not taken
        assert fetch.pod_port(pod) == 16686
        jaeger["ports"] = [{"name": "query", "containerPort": 8686}]
        assert fetch.pod_port(pod) == 8686
        assert fetch.pod_port(pod, 7000) == 7000
    # no Jaeger image: the first container that is not a known sidecar
    pod, _ = two_containers(True)
    pod["spec"]["containers"][1]["image"] = "registry.example/tracing:1"
    pod["spec"]["containers"][1]["ports"] = [{"name": "query", "containerPort": 8001}]
    assert fetch.main_container(pod)["name"] == "jaeger" and fetch.pod_port(pod) == 8001
    # a jaeger-agent sidecar is not the query service
    assert not fetch.is_jaeger_image("jaegertracing/jaeger-agent:1.62")
    assert fetch.is_jaeger_image("jaegertracing/jaeger-query:1.62")


def test_the_asked_pod_reports_its_start_time_and_restarts():
    env, _ = run()
    pod = env["outputs"]["httpErrors"]["queryPod"]
    assert pod == {
        "name": POD,
        "container": "jaeger",
        "image": "jaegertracing/all-in-one:1.76.0",
        "startTime": "2026-11-04T00:13:08Z",
        "containerStartedAt": "2026-11-04T00:13:09Z",
        "restartCount": 0,
    }
    restarted = base_pod()
    restarted["status"]["containerStatuses"] = [
        {
            "name": "jaeger",
            "restartCount": 3,
            "state": {"running": {"startedAt": "2026-11-04T00:00:00Z"}},
        }
    ]
    assert fetch.query_pod(restarted)["restartCount"] == 3
    assert fetch.query_pod(restarted)["containerStartedAt"] == "2026-11-04T00:00:00Z"


@pytest.mark.parametrize(
    "replicas, image, sharded",
    [
        (1, "jaegertracing/all-in-one:1.76.0", False),
        (3, "jaegertracing/all-in-one:1.76.0", True),
        (3, "registry:5000/jaegertracing/all-in-one@sha256:abc", True),
        (2, "jaegertracing/jaeger:2.4.0", True),
        (3, "jaegertracing/jaeger-query:1.76.0", False),
        (None, "jaegertracing/all-in-one:1.76.0", False),
    ],
)
def test_more_than_one_all_in_one_replica_may_have_sharded_storage(replicas, image, sharded):
    workload = {"kind": "Deployment", "metadata": {"name": "j"}, "spec": {"replicas": replicas}}
    pod = base_pod()
    pod["spec"]["containers"][0]["image"] = image
    got, basis = fetch.storage_basis(workload, [pod], pod)
    assert got is sharded and (basis is not None) is sharded


def test_storage_may_be_sharded_is_a_fact_in_the_output():
    out = http_errors.view(
        Clocked(lambda i, argv: (0, json.dumps(EMPTY), "")),
        {**WORKLOAD, "spec": {"replicas": 3}},
        [base_pod()],
        "http",
        None,
        timedelta(hours=1),
        CAPTURED,
        PARAMS,
        50,
        fetch.Budget(clock=lambda: 0.0),
    )
    assert out["storageMayBeSharded"] is True and "all-in-one" in out["storageBasis"]
    assert out["workload"]["replicas"] == 3
    env, _ = run()
    assert env["outputs"]["httpErrors"]["storageMayBeSharded"] is False
    assert env["outputs"]["httpErrors"]["storageBasis"] is None


def test_check_inputs_codes_limit_and_duration():
    for scheme, port in (("ftp", None), ("http", 0), ("http", "80"), ("http", True)):
        with pytest.raises(RwError) as exc:
            fetch.check_inputs(scheme, port)
        assert exc.value.code == "invalid_input"
    fetch.check_inputs("https", 16686)
    assert http_errors.check_codes([503, 500, 500]) == [500, 503]
    for bad in ([], [200], [600], ["500"], [True], None):
        with pytest.raises(RwError):
            http_errors.check_codes(bad)
    for bad in (0, 501, 1500, "5", True):
        with pytest.raises(RwError):
            http_errors.check_limit(bad)
    assert http_errors.check_limit(500) == 500
    for text in ("", "soon", "0s", "1h30"):
        with pytest.raises(RwError):
            parse_duration(text)
    assert parse_duration("1h30m") == timedelta(minutes=90)


class FakeProxyCtx:
    def __init__(self, answer):
        self.k8s_proxy_get = ProxyFake(answer)


def ask(answer, path="/x"):
    return proxy.proxy_get(FakeProxyCtx(answer), "ns", POD, "http", 16686, path)


def test_classify_and_proxy_get_tell_the_statuses_apart():
    assert k8s.classify(1, UNREACHABLE) == "unreachable"
    assert k8s.classify(1, FORBIDDEN.format(r="pods/proxy", v="get")) == "permission_denied"
    assert k8s.classify(1, 'Error from server (NotFound): pods "x" not found') == "not_found"
    assert ask("ok") == proxy.Reply(200, "ok")
    assert ask(reply(404, "404 page not found")).status == 404
    assert ask(reply(503, "unavailable")) == proxy.Reply(503, "unavailable")
    assert ask(reply(403, "forbidden")).status == 403  # the backend's 403 is data, not RBAC
    with pytest.raises(RwError) as exc:
        ask(api_error(UNREACHABLE, k8s.classify))
    assert exc.value.code == "unreachable"
    with pytest.raises(RwError) as exc:
        ask(api_error(FORBIDDEN.format(r="pods/proxy", v="get"), k8s.classify))
    assert exc.value.code == "permission_denied"


def test_proxy_get_asks_the_pod_proxy_with_scheme_port_path_and_timeout():
    ctx = FakeProxyCtx("ok")
    proxy.proxy_get(ctx, "ns", POD, "https", 9000, "/api/services?x=1", 7)
    assert ctx.k8s_proxy_get.requests == [
        {
            "namespace": "ns",
            "kind": "pods",
            "name": POD,
            "port": 9000,
            "path": "/api/services?x=1",
            "scheme": "https",
            "timeout": 7,
        }
    ]


def test_a_cross_host_redirect_is_reported_and_never_followed():
    """S17: the helper drops the Location; the skill reports the host and asks nothing more."""
    ctx = FakeProxyCtx(cross_host_redirect("sso.example.invalid"))
    got = proxy.proxy_get(ctx, "ns", POD, "http", 16686, "/api/services")
    assert got.status == 302 and got.redirect_dropped == "sso.example.invalid"
    assert len(ctx.k8s_proxy_get.requests) == 1
    with pytest.raises(RwError) as exc:
        proxy.expect_200(got, "services")
    assert exc.value.code == "dependency"
    assert "off cluster to sso.example.invalid (not followed)" in exc.value.message


def test_a_cross_host_redirect_through_the_task_is_an_error_and_asks_once():
    env, calls = run(replies={"/api/services": cross_host_redirect("sso.example.invalid")})
    assert env["status"] == "failed" and codes_of(env) == [("dependency", None)]
    assert "sso.example.invalid" in env["errors"][0]["message"]
    assert [c for c in calls if c[0] == "k8s_proxy_get"] == [raw("/api/services")]


def test_a_same_pod_redirect_is_followed_once_and_no_more():
    ctx = FakeProxyCtx(
        lambda r: reply(301, "", location="/jaeger/api/x") if r["path"] == "/api/x" else "moved"
    )
    assert ask_with(ctx, "/api/x").body == "moved"
    assert ctx.k8s_proxy_get.paths() == ["/api/x", "/jaeger/api/x"]
    loop = FakeProxyCtx(lambda r: reply(302, "", location="/again"))
    assert ask_with(loop, "/a").status == 302 and len(loop.k8s_proxy_get.requests) == 2


def ask_with(ctx, path):
    return proxy.proxy_get(ctx, "ns", POD, "http", 16686, path)


def test_a_cut_body_is_internal_never_a_partial_parse():
    with pytest.raises(RwError) as exc:
        ask(reply(200, '{"data": [', truncated=True))
    assert exc.value.code == "internal" and exc.value.hint


def test_the_error_reason_decides_before_the_message_text():
    forbidden_not_found = (
        'Error from server (Forbidden): pods "x" is forbidden: User "u" cannot get resource '
        '"pods/proxy": the server could not find the requested resource (not found)'
    )
    assert k8s.classify(1, forbidden_not_found) == "permission_denied"
    assert k8s.classify(1, "Error from server (Timeout): request did not complete") == "timeout"
    assert k8s.classify(1, "Error from server (TooManyRequests): slow down") == "rate_limited"
    assert k8s.classify(1, "Error from server (Unauthorized): Unauthorized") == "permission_denied"
    assert (
        k8s.classify(
            1, 'Error from server (NotFound): the server doesn\'t have a resource type "x"'
        )
        == "dependency"
    )
    # a Reason that does not decide falls back to the text; no Reason line: text only
    internal = "Error from server (InternalError): context deadline exceeded"
    assert k8s.classify(1, internal) == "timeout"
    assert k8s.classify(1, "Error from server (BadRequest): nope") == "internal"
    assert (
        k8s.classify(1, "Unable to connect to the server: dial tcp: i/o timeout") == "unreachable"
    )
    # the Reason is read on the line that starts with it, not anywhere in a message
    warned = "Warning: Error from server (Forbidden) in a log\nError from server (NotFound): gone"
    assert k8s.classify(1, warned) == "not_found"


def test_workload_and_pod_reads_are_projected_so_no_env_value_leaves_the_cluster():
    _, calls = run()
    reads = [c for c in calls if c[0] == "kubectl"]
    assert reads and all(c[-2] == "-o" and c[-1].startswith("jsonpath=") for c in reads)
    for projection in (WORKLOAD_FIELDS, POD_FIELDS):
        template = projection.template(True)
        assert "env" not in template and "annotations" not in template
    # of spec.containers only the name, image and ports are read; of the status the restart count
    # and start time. No args, command, env or volume mounts
    template = POD_FIELDS.template(False)
    assert template.count("spec.containers") == 1
    for field in (".args", ".command", ".env", ".volumeMounts", ".lifecycle", ".securityContext"):
        assert field not in template


def test_projection_parses_what_kubectl_prints_and_refuses_anything_else():
    doc = POD_FIELDS.parse((DATA / "pods.txt").read_text(), True)
    [pod] = doc["items"]
    assert pod["metadata"] == {"name": POD}
    assert pod["status"]["phase"] == "Running"
    assert pod["spec"]["containers"][0]["ports"][0] == {"name": "query", "containerPort": 16686}
    assert pod["spec"]["containers"][0]["image"] == "jaegertracing/all-in-one:1.76.0"
    assert pod["status"]["startTime"] == "2026-11-04T00:13:08Z"
    assert pod["status"]["containerStatuses"] == [
        {
            "name": "jaeger",
            "restartCount": 0,
            "state": {"running": {"startedAt": "2026-11-04T00:13:09Z"}},
        }
    ]
    two = Projection(containers=True).parse("@\nc:a=i1\np:x=1\nc:b=i2\np:=2\n", False)
    assert [c.get("ports") for c in two["spec"]["containers"]] == [
        [{"name": "x", "containerPort": 1}],
        [{"containerPort": 2}],
    ]
    workload = WORKLOAD_FIELDS.parse((DATA / "workload.txt").read_text(), False)
    assert workload["kind"] == "Deployment" and workload["spec"]["replicas"] == 1
    assert workload["spec"]["selector"]["matchLabels"]["app"] == "jaeger"
    assert Projection(strings=("a.b",)).parse("@\ns:a.b=\n", False) == {}  # absent: left out
    bad = ("", "x\n", "@\nj:a=not json\n", "@\np:x=notaport\n", "@\nr:a=many|\n", "s:a=1\n")
    for text in bad:
        with pytest.raises(RwError) as exc:
            Projection(strings=("a",), values=("a",), containers=True).parse(text, False)
        assert exc.value.code == "internal"


# ------------------------------------------------------------ script ----


def test_broken_reports_5xx_and_4xx_per_service_and_operation():
    env, calls = run()
    assert env["status"] == "ok" and env["errors"] == []
    check_schema(env)
    out = env["outputs"]["httpErrors"]
    assert (out["serverErrorSpans"], out["clientErrorSpans"]) == (11, 10)
    assert out["outboundErrorSpans"] == 2
    assert out["serviceCount"] == 5 and out["queriedCount"] == 5 and out["truncated"] is False
    assert out["completeCount"] == 5 and out["servicesNotQueried"] == 0
    assert out["limitReached"] is False
    assert [s["name"] for s in out["services"]] == [
        "checkout",
        "inventory",
        "gateway",
        "catalog",
        "jaeger-all-in-one",
    ]
    pay = operation(env, "checkout", "POST /pay")
    assert pay["serverErrorSpans"] == 6 and pay["clientErrorSpans"] == 0
    assert pay["byStatus"] == {"500": 4, "503": 2} and pay["urlPaths"] == ["/pay"]
    assert len(pay["sampleServerTraceIds"]) == 5 and pay["sampleClientTraceIds"] == []
    cart = operation(env, "checkout", "GET /cart")
    assert cart["serverErrorSpans"] == 0 and cart["clientErrorSpans"] == 3
    assert cart["byStatus"] == {"404": 3} and cart["urlPaths"] == ["/cart/items"]
    # the newer semantic-convention keys: status on http.response.status_code, path on url.path
    api = operation(env, "gateway", "GET /api")
    assert api["byStatus"] == {"502": 2} and api["urlPaths"] == ["/api"]
    # a 507 is in no statusCodes list: the error=true pass finds it
    assert 507 not in CODES
    stock = operation(env, "inventory", "POST /stock")
    assert (
        stock["byStatus"] == {"507": 3} and service_row(env, "inventory")["serverErrorSpans"] == 3
    )
    # one error=true query per service, then one query per service, status attribute key and code
    queries = [c for c in calls if c[0] == "k8s_proxy_get" and "/api/traces" in c[4]]
    assert len(queries) == 5 * (1 + 2 * len(CODES))
    assert "tags=%7B%22error%22%3A%22true%22%7D" in queries[0][4]


def test_all_error_true_queries_come_before_any_exact_code_query():
    _, calls = run()
    tags = [
        parse_qs(urlsplit(c[4]).query)["tags"][0]
        for c in calls
        if c[0] == "k8s_proxy_get" and "/api/traces" in c[4]
    ]
    first_exact = next(i for i, t in enumerate(tags) if "error" not in json.loads(t))
    assert all(json.loads(t) == {"error": "true"} for t in tags[:first_exact])
    assert first_exact == len(SERVICES)
    assert all("error" not in json.loads(t) for t in tags[first_exact:])


def test_an_id_in_the_path_and_in_the_operation_name_is_replaced():
    env, _ = run(inputs={"services": ["checkout"]})
    reset = operation(env, "checkout", "GET /reset/{id}/confirm")
    assert reset["urlPaths"] == ["/reset/{id}/confirm"] and reset["clientErrorSpans"] == 2
    text = json.dumps(env)
    assert "9f86d081884c" not in text and "not-a-real-code" not in text
    # catalog's raw /items/9 and gateway's http.route /v2/users/{id}
    env, _ = run(inputs={"services": ["catalog", "gateway"]})
    assert operation(env, "catalog", "GET /items/{id}")["urlPaths"] == ["/items/{id}"]
    users = operation(env, "gateway", "GET /v2/users/{id}")
    assert users["urlPaths"] == ["/v2/users/{id}"] and "42" not in json.dumps(users)


def test_outbound_client_spans_are_counted_apart_from_inbound_ones():
    env, _ = run(inputs={"services": ["checkout"]})
    row = service_row(env, "checkout")
    assert (row["serverErrorSpans"], row["clientErrorSpans"], row["outboundErrorSpans"]) == (
        6,
        5,
        2,
    )
    out = operation(env, "checkout", "POST payments")
    assert out["serverErrorSpans"] == 0 and out["byStatus"] == {}
    assert out["outboundByStatus"] == {"503": 2} and out["outboundErrorSpans"] == 2
    assert out["urlPaths"] == [] and len(out["sampleOutboundTraceIds"]) == 2
    check_schema(env)


def test_healthy_service_has_no_5xx():
    env, _ = run(inputs={"services": ["catalog"]})
    assert env["status"] == "ok" and env["errors"] == []
    check_schema(env)
    out = env["outputs"]["httpErrors"]
    assert out["serverErrorSpans"] == 0 and out["clientErrorSpans"] == 3
    [row] = out["services"]
    assert row["name"] == "catalog" and row["serverErrorSpans"] == 0
    [op] = row["operations"]
    assert op["operation"] == "GET /items/{id}" and op["clientErrorSpans"] == 3
    assert op["urlPaths"] == ["/items/{id}"]  # `/items/9?debug=1`: the id and the query are gone


def test_a_service_with_no_error_spans_has_zero_counts_and_no_operations():
    env, _ = run(answer_all=(0, json.dumps(EMPTY), ""))
    assert env["status"] == "ok"
    check_schema(env)
    out = env["outputs"]["httpErrors"]
    assert out["serverErrorSpans"] == 0 and out["clientErrorSpans"] == 0
    assert all(
        s["queried"] and s["complete"] and s["operations"] == [] and s["tracesSeen"] == 0
        for s in out["services"]
    )


def test_no_tag_value_or_query_string_leaves_the_task():
    env, _ = run()
    text = json.dumps(env)
    for leaked in (
        "not-a-real-token",
        "not-a-real-key",
        "not-a-real-code",
        "9f86d081884c",
        "user=42",
        "page=2",
        "debug=1",
        "http.url",
    ):
        assert leaked not in text
    assert set(operation(env, "checkout", "GET /cart")) == {
        "operation",
        "serverErrorSpans",
        "clientErrorSpans",
        "outboundErrorSpans",
        "byStatus",
        "outboundByStatus",
        "urlPaths",
        "urlPathsTruncated",
        "sampleServerTraceIds",
        "sampleClientTraceIds",
        "sampleOutboundTraceIds",
        "truncated",
    }


SECRET_PATHS = [
    ("/reset/9f86d081884c7d659a2feaa0c55ad015/confirm", "/reset/{id}/confirm"),
    ("/p/sk-abcdef123456", "/p/{id}"),
    ("/p/sk-abcdefghijkl", "/p/{id}"),
    ("/app/page;jsessionid=ABCDEF0123456789", "/app/page"),
    ("/app;jsessionid=X/page;a=b/more", "/app/page/more"),
    (
        "/services/T0123ABCD45/B0123ABCD45/AbCdEfGhIjKlMnOpQrStUvWx",
        "/services/T0123ABCD45/B0123ABCD45/{id}",
    ),
    (
        "/services/T0123ABCD45/B0123ABCD45/xYz4kQw9LmN2pR7sT1uV5aBc",
        "/services/T0123ABCD45/B0123ABCD45/{id}",
    ),
    ("/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage", "/{id}/sendMessage"),
    ("/hooks/eyJhbGciOiJIUzI1NiJ9.e30.abc", "/hooks/{id}"),
    ("/orders/123456/items/42", "/orders/{id}/items/{id}"),
    ("/users/123e4567-e89b-12d3-a456-426614174000/profile", "/users/{id}/profile"),
    ("/files/deadbeefdeadbeef", "/files/{id}"),
    ("/cart?token=abc#frag", "/cart"),
    ("/api/authentication/get-user-profile", "/api/authentication/get-user-profile"),
    ("/v2/users/{id}", "/v2/users/{id}"),
    ("", "/"),
]


@pytest.mark.parametrize("raw_path, expected", SECRET_PATHS)
def test_redact_path_replaces_ids_and_secrets_and_keeps_names(raw_path, expected):
    assert traces.redact_path(raw_path) == expected


def test_no_secret_shaped_path_leaves_the_task_in_a_path_or_an_operation_name():
    """Each secret-shaped path is the span's URL path and part of its operation name."""
    doc = load("traces-checkout.json")
    base = doc["data"][0]
    many = []
    for i, (path, _) in enumerate(SECRET_PATHS[:10]):
        t = json.loads(json.dumps(base))
        t["traceID"] = f"{i:032x}"
        t["spans"][0].update(
            traceID=t["traceID"],
            spanID=f"{i:016x}",
            operationName=f"GET {path}?secret=not-a-real-secret#frag",
        )
        t["spans"][0]["tags"] = span_tags(500, **{"http.target": f"{path}?secret=x"})
        many.append(t)
    doc["data"] = many
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    check_schema(env)
    text = json.dumps(env["outputs"])
    for leaked in (
        "9f86d081884c",
        "sk-abcdef",
        "jsessionid",
        "AbCdEfGhIjKlMnOpQrStUvWx",
        "xYz4kQw9LmN2pR7sT1uV5aBc",
        "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        "123456789",
        "not-a-real-secret",
        "frag",
    ):
        assert leaked not in text, leaked
    ops = {o["operation"] for o in service_row(env, "checkout")["operations"]}
    assert "GET /reset/{id}/confirm" in ops and "GET /app/page" in ops
    assert "GET /services/T0123ABCD45/B0123ABCD45/{id}" in ops


def test_clean_operation_cuts_query_and_fragment_only_from_urls():
    assert traces.clean_operation("GET /cart?token=x#f") == "GET /cart"
    assert traces.clean_operation("GET https://u:pw@h.example/a/123456?x=1") == "GET /a/{id}"
    assert traces.clean_operation("UserService#get") == "UserService#get"
    assert traces.clean_operation("SELECT x FROM t WHERE a = ?") == "SELECT x FROM t WHERE a = ?"
    assert traces.clean_operation("POST /pay") == "POST /pay"


def test_outside_the_window_nothing_counts_even_if_jaeger_returns_the_trace():
    """A trace that matched on one span may carry spans outside the window: only spans inside it
    count."""
    later = CAPTURED + timedelta(hours=2)
    replies = {}
    for service in SERVICES:
        doc = trace_doc(service)
        for key, code in [traces.ERROR_TAG] + [(k, c) for k in traces.STATUS_KEYS for c in CODES]:
            hits = [t for t in doc["data"] if tag_matches(t, key, code)]
            replies[(service, key, code)] = (0, json.dumps({**doc, "data": hits}), "")
    env, _ = run(now=later, replies=replies)
    out = env["outputs"]["httpErrors"]
    assert out["window"]["startMicros"] == micros(later - timedelta(hours=1))
    assert out["serverErrorSpans"] == 0 and out["clientErrorSpans"] == 0
    assert service_row(env, "checkout")["tracesSeen"] > 0


def test_every_error_status_in_a_returned_trace_counts_listed_or_not_and_only_the_services_own():
    doc = load("traces-checkout.json")
    trace = doc["data"][0]
    span = trace["spans"][0]
    trace["processes"]["p2"] = {"serviceName": "other", "tags": []}
    other = {**span, "spanID": "ff01", "processID": "p2", "tags": span_tags(500)}
    teapot = {**span, "spanID": "ff02", "operationName": "teapot", "tags": span_tags(418)}
    as_text = {
        **span,
        "spanID": "ff03",
        "operationName": "textual",
        "tags": [
            {"key": "http.status_code", "type": "string", "value": "500"},
            {"key": "error", "type": "bool", "value": True},
        ],
    }
    ok = {**span, "spanID": "ff04", "operationName": "fine", "tags": span_tags(399)}
    trace["spans"] += [other, teapot, as_text, ok]
    # the trace is returned by an error=true or a 404 query only if one of its spans carries that
    trace["spans"][0]["tags"] += [{"key": "error", "type": "bool", "value": True}]
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    row = service_row(env, "checkout")
    assert row["serverErrorSpans"] == 7  # 6 + the string "500"; not the other service's
    ops = {o["operation"]: o for o in row["operations"]}
    assert ops["textual"]["byStatus"] == {"500": 1}
    assert ops["teapot"]["byStatus"] == {"418": 1}  # 418 is in no list: counted, it was seen
    assert row["clientErrorSpans"] == 6 and "fine" not in ops


def test_a_5xx_without_the_error_flag_is_found_only_when_a_returned_trace_carries_it():
    """The documented gap: a 503 span with no error status and no listed code is in no search."""
    doc = load("traces-gateway.json")
    for t in doc["data"]:
        for sp in t["spans"]:
            sp["tags"] = [x for x in sp["tags"] if x["key"] != "error"]
    env, _ = run(docs={"gateway": doc}, inputs={"services": ["gateway"]})
    row = service_row(env, "gateway")
    assert row["serverErrorSpans"] == 0 and row["clientErrorSpans"] == 2  # the 502s are missed
    # ... but a trace returned for another reason (a listed 404) shows the 5xx beside it
    carrier = next(t for t in doc["data"] if tag_matches(t, "http.response.status_code", 404))
    extra = {**carrier["spans"][0], "spanID": "ee01", "operationName": "beside"}
    extra["tags"] = [
        {"key": "http.response.status_code", "type": "int64", "value": 503},
        {"key": "span.kind", "type": "string", "value": "server"},
    ]
    carrier["spans"].append(extra)
    env, _ = run(docs={"gateway": doc}, inputs={"services": ["gateway"]})
    assert operation(env, "gateway", "beside")["byStatus"] == {"503": 1}


def test_a_span_found_by_two_queries_is_counted_once():
    doc = load("traces-checkout.json")
    both = next(t for t in doc["data"] if tag_matches(t, "http.status_code", 404))
    for span in both["spans"]:
        span["tags"].append({"key": "http.response.status_code", "type": "int64", "value": 404})
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    assert operation(env, "checkout", "GET /cart")["clientErrorSpans"] == 3
    # a 500 span is returned by the error=true query and by a listed-code query: still one span
    flagged = next(
        t
        for t in doc["data"]
        if tag_matches(t, "error", "true") and tag_matches(t, "http.status_code", 500)
    )
    flagged["spans"][0]["tags"].append(
        {"key": "http.response.status_code", "type": "int64", "value": 500}
    )
    env, _ = run(
        docs={"checkout": doc},
        inputs={"services": ["checkout"]},
        settings={**SETTINGS, "statusCodes": [404, 500]},
    )
    assert operation(env, "checkout", "POST /pay")["serverErrorSpans"] == 6


def test_limit_reached_marks_the_counts_as_a_lower_bound():
    env, _ = run(inputs={"limit": 2, "services": ["checkout"]})
    row = service_row(env, "checkout")
    out = env["outputs"]["httpErrors"]
    assert row["limitReached"] is True and out["limit"] == 2 and out["limitReached"] is True
    assert row["serverErrorSpans"] < 6  # the error=true query returned 2 of its 8 traces
    env, _ = run(inputs={"limit": 100, "services": ["checkout"]})
    assert service_row(env, "checkout")["limitReached"] is False
    assert env["outputs"]["httpErrors"]["limitReached"] is False


def test_services_inputs_unknown_and_excluded_names_are_facts():
    env, _ = run(inputs={"services": ["checkout", "nope"], "exclude_services": ["checkout"]})
    out = env["outputs"]["httpErrors"]
    assert env["status"] == "ok" and out["services"] == []
    assert out["unknownServices"] == ["nope"] and out["excludedServices"] == ["checkout"]
    env, _ = run(inputs={"exclude_services": ["checkout", "gateway"]})
    assert [s["name"] for s in env["outputs"]["httpErrors"]["services"]] == [
        "inventory",
        "catalog",
        "jaeger-all-in-one",
    ]
    check_schema(env)


def test_services_beyond_max_items_are_counted_not_queried():
    env, calls = run(settings={**SETTINGS, "maxItems": 2})
    out = env["outputs"]["httpErrors"]
    assert out["truncated"] is True and out["serviceCount"] == 5
    assert out["queriedCount"] == 2 and out["servicesNotQueried"] == 3
    assert [s["name"] for s in out["services"]] == ["checkout", "catalog"]
    assert len([c for c in calls if c[0] == "k8s_proxy_get" and "/api/traces" in c[4]]) == 2 * (
        1 + 2 * len(CODES)
    )
    check_schema(env)


def test_no_services_in_jaeger_is_a_fact():
    env, calls = run(replies={"/api/services": (0, '{"data":null,"total":0}', "")})
    assert env["status"] == "ok"
    out = env["outputs"]["httpErrors"]
    assert out["serviceCount"] == 0 and out["services"] == []
    assert not [c for c in calls if c[0] == "k8s_proxy_get" and "/api/traces" in c[4]]


def test_operations_are_capped_at_50_and_long_text_is_cut():
    doc = load("traces-checkout.json")
    base = doc["data"][0]
    base["spans"][0]["tags"] = span_tags(500, **{"http.target": "/p" * 700})
    many = []
    for i in range(60):
        t = json.loads(json.dumps(base))
        t["traceID"] = f"{i:032x}"
        t["spans"][0].update(
            traceID=t["traceID"],
            spanID=f"{i:016x}",
            operationName=f"op-{i:02d}" + ("x" * 2000 if i == 0 else ""),
        )
        many.append(t)
    doc["data"] = many
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"], "limit": 500})
    check_schema(env)
    row = service_row(env, "checkout")
    assert row["operationCount"] == 60 and len(row["operations"]) == 50
    assert row["operationsTruncated"] is True
    long = next(o for o in row["operations"] if o["operation"].startswith("op-00"))
    assert len(long["operation"]) == 1024 and long["truncated"] is True
    assert len(long["urlPaths"][0]) == 1024


def test_url_paths_and_samples_are_capped():
    doc = load("traces-checkout.json")
    many = []
    for i in range(12):
        t = json.loads(json.dumps(doc["data"][0]))
        t["traceID"] = f"{i:032x}"
        t["spans"][0].update(traceID=t["traceID"], spanID=f"{i:016x}", operationName="GET /x")
        t["spans"][0]["tags"] = span_tags(500, **{"http.target": f"/x/name-{chr(97 + i)}"})
        many.append(t)
    doc["data"] = many
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    op = operation(env, "checkout", "GET /x")
    assert op["serverErrorSpans"] == 12 and len(op["urlPaths"]) == 10 and op["urlPathsTruncated"]
    assert len(op["sampleServerTraceIds"]) == 5
    check_schema(env)


# ------------------------------------------------------------ failures ----


def test_html_where_services_were_expected_fails_the_task():
    html = "<!doctype html><html><title>Jaeger UI</title></html>"
    env, _ = run(replies={"/api/services": (0, html, "")})
    assert env["status"] == "failed" and codes_of(env) == [("internal", None)]
    assert env["outputs"] == {}


def test_a_services_path_the_pod_does_not_serve_is_a_dependency_error():
    env, _ = run(replies={"/api/services": NO_ROUTE})
    assert env["status"] == "failed" and codes_of(env) == [("dependency", None)]


def test_a_service_whose_first_query_fails_is_one_error_and_the_others_still_report():
    env, _ = run(replies={("catalog", "error", "true"): (0, "<html>login</html>", "")})
    assert env["status"] == "partial" and codes_of(env) == [("internal", "service/catalog")]
    check_schema(env)
    catalog = service_row(env, "catalog")
    assert catalog["queried"] is False and catalog["serverErrorSpans"] is None
    assert catalog["complete"] is False and catalog["queriesDone"] == 0
    assert catalog["operations"] == []
    out = env["outputs"]["httpErrors"]
    assert out["queriedCount"] == 4 and out["servicesNotQueried"] == 1
    assert [s["name"] for s in out["services"]][-1] == "catalog"
    assert service_row(env, "checkout")["serverErrorSpans"] == 6


def test_a_service_cut_off_mid_pass_keeps_its_finished_queries():
    env, calls = run(replies={("checkout", "http.status_code", 404): (0, "<html>login</html>", "")})
    assert env["status"] == "partial" and codes_of(env) == [("internal", "service/checkout")]
    check_schema(env)
    row = service_row(env, "checkout")
    # pass 1 (error=true) and the 400, 401 and 403 queries finished; the 404 one failed and the
    # service's remaining queries were not asked
    assert row["queried"] is True and row["complete"] is False
    assert row["queriesDone"] == 4 and row["queriesPlanned"] == 1 + 2 * len(CODES)
    assert row["serverErrorSpans"] == 6 and row["clientErrorSpans"] == 0
    checkout_queries = [c for c in calls if c[0] == "k8s_proxy_get" and "service=checkout" in c[4]]
    assert len(checkout_queries) == 5
    other = service_row(env, "catalog")
    assert other["complete"] is True and other["clientErrorSpans"] == 3
    out = env["outputs"]["httpErrors"]
    assert out["queriedCount"] == 5 and out["completeCount"] == 4


def test_jaeger_errors_in_a_200_body_are_an_error_never_no_errors():
    body = json.dumps({"data": None, "errors": [{"code": 400, "msg": "invalid tags"}]})
    env, _ = run(answer_all=(0, body, ""))
    assert env["status"] == "failed" and codes_of(env) == [("internal", "service/catalog")]
    assert "invalid tags" in env["errors"][0]["message"]


def test_a_long_error_message_is_cut_to_1024_on_every_path():
    long_err = "Error from server (InternalError): " + "x" * 5000
    # every service fails: the first failure is raised
    env, _ = run(answer_all=(1, "", long_err))
    assert env["status"] == "failed" and len(env["errors"][0]["message"]) <= 1024
    # one service fails: ctx.error
    env, _ = run(replies={("catalog", "error", "true"): (1, "", long_err)})
    assert env["status"] == "partial" and len(env["errors"][0]["message"]) <= 1024
    # a failing kubectl read before any query
    env, _ = run(fail={"workload": long_err})
    assert env["status"] == "failed" and len(env["errors"][0]["message"]) <= 1024
    # a service name that is itself very long
    name = "s" * 3000
    err = k8s.failure(["kubectl"], SimpleNamespace(returncode=1, stdout="", stderr=long_err))
    assert len(err.message) <= 1024
    assert len(RwError("internal", name).message) == 1024


def test_every_service_failing_fails_the_task():
    env, _ = run(answer_all=(1, "", UNREACHABLE))
    assert env["status"] == "failed" and codes_of(env) == [("unreachable", "service/catalog")]


def test_forbidden_proxy_fails_with_permission_denied():
    env, _ = run(fail={"proxy": FORBIDDEN.format(r="pods/proxy", v="get")})
    assert env["status"] == "failed" and codes_of(env) == [("permission_denied", None)]


def test_forbidden_on_one_traces_query_is_not_swallowed():
    denied = (1, "", FORBIDDEN.format(r="pods/proxy", v="get"))
    env, _ = run(replies={("catalog", "http.status_code", 404): denied})
    assert env["status"] == "failed" and codes_of(env) == [("permission_denied", None)]


def test_not_found_forbidden_and_unreachable_workload():
    n = "deployments.apps"
    env, _ = run(fail={"workload": f'Error from server (NotFound): {n} "jaeger" not found'})
    assert env["status"] == "failed" and codes_of(env) == [("not_found", None)]
    env, _ = run(fail={"workload": FORBIDDEN.format(r=n, v="get")})
    assert env["status"] == "failed" and codes_of(env) == [("permission_denied", None)]
    env, _ = run(fail={"pods": FORBIDDEN.format(r="pods", v="list")})
    assert env["status"] == "failed" and codes_of(env) == [("permission_denied", None)]
    env, _ = run(fail={"workload": "Unable to connect to the server: dial tcp: i/o timeout"})
    assert env["status"] == "failed" and codes_of(env) == [("unreachable", None)]


def test_no_running_pod_is_unreachable_and_a_ready_pod_is_asked_first():
    pod = base_pod()
    pod["status"]["phase"] = "Pending"
    env, calls = run(pods=[pod])
    assert env["status"] == "failed" and codes_of(env) == [("unreachable", None)]
    assert not [c for c in calls if c[0] == "k8s_proxy_get"]
    ready = base_pod()
    not_ready = json.loads(json.dumps(ready))
    not_ready["metadata"]["name"] = "aaa-not-ready"
    not_ready["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    workload = {"kind": "Deployment", "metadata": {"name": "j"}}
    assert fetch.chosen_pod(workload, [not_ready, ready]) is ready


def bare_run(settings=None, **inputs):
    """A run with no stubbed command: any kubectl call fails the test."""
    with make_context(resource=RESOURCE, settings=settings or SETTINGS, now=CAPTURED) as tc:
        calls = stub_commands(tc, {})
        return run_main(script(), tc, **inputs), calls


@pytest.mark.parametrize(
    "inputs",
    [
        {"since": "soon"},
        {"since": "0s"},
        {"limit": 0},
        {"limit": 501},
        {"limit": 5000},
        {"scheme": "ftp"},
        {"port": 0},
        {"services": "checkout"},
        {"exclude_services": [1]},
    ],
)
def test_bad_input_is_invalid_input_before_any_call(inputs):
    env, calls = bare_run(**inputs)
    assert env["status"] == "failed" and codes_of(env) == [("invalid_input", None)]
    assert calls == []


def test_bad_status_codes_setting_is_invalid_input_before_any_call():
    env, calls = bare_run(settings={**SETTINGS, "statusCodes": [200]})
    assert env["status"] == "failed" and codes_of(env) == [("invalid_input", None)]
    assert calls == []


def test_a_target_of_another_type_is_invalid_input_and_the_path_gives_the_type():
    res = {"cluster": "kind", "namespace": NS, "name": "x", "type": "kubernetes/daemonset"}
    env, _ = run(res=res)
    assert env["status"] == "failed" and codes_of(env) == [("invalid_input", None)]
    res = {k: v for k, v in RESOURCE.items() if k != "type"}
    env, _ = run(res=res)
    assert env["status"] == "ok"


def test_inputs_choose_the_port_and_scheme():
    env, calls = run(inputs={"port": 9000, "scheme": "https", "services": ["catalog"]})
    assert env["status"] == "ok"
    assert raw("/api/services", "https", 9000) in calls


# ------------------------------------------------------------ deadline ----


class Clocked:
    """A ctx whose `k8s_proxy_get` calls each take `step` seconds of a fake clock."""

    def __init__(self, answer, step=30):
        self.now = 0.0
        self.step = step
        self.answer = answer
        self.calls = []
        self.errors = []

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


WORKLOAD = {"kind": "Deployment", "metadata": {"name": "jaeger", "namespace": NS}}
PODS = [{"metadata": {"name": POD}, "status": {"phase": "Running"}, "spec": {}}]
PARAMS = {"statusCodes": [404], "limit": 100, "services": [], "excludeServices": []}
TIMEOUT = "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)"


def test_the_deadline_leaves_the_services_not_asked_a_timeout_error():
    names = json.dumps({"data": ["a", "b", "c", "d"]})
    ctx = Clocked(lambda i, argv: (0, names, "") if i == 0 else (0, json.dumps(EMPTY), ""), step=30)
    budget = fetch.Budget(clock=ctx.clock)
    out = http_errors.view(
        ctx, WORKLOAD, PODS, "http", None, timedelta(hours=1), CAPTURED, PARAMS, 200, budget
    )
    # /api/services at 0s, then pass 1 asks a, b, c at 30s each: the 100s deadline falls before d
    assert [s["queried"] for s in out["services"]] == [True, True, True, False]
    # pass 2 (two queries per service) finds no time left: a, b and c are incomplete
    assert [s["complete"] for s in out["services"]] == [False, False, False, False]
    assert [s["name"] for s in out["services"] if s["queried"]] == ["a", "b", "c"]
    assert [s for _, s, _ in ctx.errors] == ["service/a", "service/b", "service/c", "service/d"]
    assert all(code == "timeout" and "deadline" in msg for code, _, msg in ctx.errors)
    assert out["queriedCount"] == 3 and out["servicesNotQueried"] == 1
    assert out["completeCount"] == 0


def test_pass_1_covers_every_service_before_pass_2_spends_budget():
    names = json.dumps({"data": [f"s{i:02d}" for i in range(10)]})
    ctx = Clocked(lambda i, argv: (0, names, "") if i == 0 else (0, json.dumps(EMPTY), ""), step=5)
    budget = fetch.Budget(clock=ctx.clock)
    out = http_errors.view(
        ctx, WORKLOAD, PODS, "http", None, timedelta(hours=1), CAPTURED, PARAMS, 200, budget
    )
    # 100s at 5s per ask: /api/services, 10 pass-1 asks, then 9 asks of pass 2 (two per service)
    assert out["queriedCount"] == 10 and out["servicesNotQueried"] == 0
    rows = {s["name"]: s for s in out["services"]}
    assert rows["s00"]["complete"] is True and rows["s00"]["queriesDone"] == 3
    assert rows["s04"]["complete"] is False and rows["s04"]["queriesDone"] == 2
    assert rows["s09"]["complete"] is False and rows["s09"]["queriesDone"] == 1
    assert out["completeCount"] == 4


def test_three_consecutive_timeouts_stop_asking():
    names = json.dumps({"data": ["a", "b", "c", "d"]})
    ctx = Clocked(lambda i, argv: (0, names, "") if i == 0 else (1, "", TIMEOUT), step=1)
    budget = fetch.Budget(clock=ctx.clock)
    with pytest.raises(RwError) as exc:
        http_errors.view(
            ctx, WORKLOAD, PODS, "http", None, timedelta(hours=1), CAPTURED, PARAMS, 200, budget
        )
    assert exc.value.code == "timeout" and exc.value.scope == "service/a"
    assert len(ctx.calls) == 4  # services, then each of a, b and c asked once and timed out
    assert ctx.errors == []  # every service failed: raised, not reported per service


def test_a_slow_jaeger_stops_after_three_timeouts_with_the_rest_reported():
    names = json.dumps({"data": ["a", "b", "c", "d", "e"]})

    def answer(i, argv):
        if i == 0:
            return 0, names, ""
        return (0, json.dumps(EMPTY), "") if i <= 2 else (1, "", TIMEOUT)

    ctx = Clocked(answer, step=1)
    budget = fetch.Budget(clock=ctx.clock)
    out = http_errors.view(
        ctx, WORKLOAD, PODS, "http", None, timedelta(hours=1), CAPTURED, PARAMS, 200, budget
    )
    # a and b answered pass 1; c, d and e timed out (three in a row): pass 2 is not asked at all
    assert [s["queried"] for s in out["services"]] == [True, True, False, False, False]
    assert [c for c, _, _ in ctx.errors] == ["timeout"] * 5
    assert "consecutive" in ctx.errors[0][2] and "Timeout" in ctx.errors[4][2]
    assert len(ctx.calls) == 6  # services, then a to e once each


# ------------------------------------------------------------ permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "deployments.apps": ("apps", "deployments"),
    "pods": ("", "pods"),
}
VALUE_FLAGS = {"-n", "-o", "-l"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs. Raises on anything it does
    not know, so a script that issues a new call has to extend this."""
    assert "--raw" not in argv, argv  # pod proxy reads go through ctx.k8s_proxy_get (S17)
    if argv[0] == "k8s_proxy_get":  # ctx.k8s_proxy_get(ns, "pods", ...): pods/proxy get
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


def granted():
    return {
        (g, r, v)
        for rule in TASKS["http-errors"]["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def test_a_statefulset_target_works_too():
    res = {**RESOURCE, "type": "kubernetes/statefulset"}
    env, calls = run(res=res, kind="statefulset", inputs={"services": ["checkout"]})
    assert env["status"] == "ok" and env["outputs"]["httpErrors"]["workload"] == {
        "kind": "StatefulSet",
        "name": "jaeger",
        "replicas": 1,
    }
    assert (
        "kubectl",
        "-n",
        NS,
        "get",
        "statefulsets.apps",
        "jaeger",
        "-o",
        jsonpath(WORKLOAD_FIELDS, False),
    ) in calls


def test_declared_permissions_are_exactly_what_the_script_calls():
    needed = set()
    for kind, rtype in (("deployment", "deployment"), ("statefulset", "statefulset")):
        res = {**RESOURCE, "type": f"kubernetes/{rtype}"}
        _, calls = run(res=res, kind=kind)
        for argv in calls:
            needed |= rbac_needs(list(argv))
    assert needed == granted(), f"calls {sorted(needed)}, declares {sorted(granted())}"


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "deployments.apps", "j", "-o", "json"]) == {
        ("apps", "deployments", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(list(raw("/api/services"))) == {("", "pods/proxy", "get")}
    with pytest.raises(AssertionError):  # S17: a proxied read through kubectl get --raw
        rbac_needs(["kubectl", "get", "--raw", "/api/v1/namespaces/n/pods/p:16686/proxy/api"])
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "-n", "x", "delete", "pods", "p"])
