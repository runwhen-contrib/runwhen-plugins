"""Unit tests for k8s-jaeger-health's lib/ and script. Captured JSON comes from the `rw-jaeger`
namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-jaeger-health.yaml): a real jaegertracing/all-in-one with in-memory storage and spans sent
over OTLP/HTTP: `checkout` (POST /pay 500 x4 and 503 x2, GET /cart 404 x3, 200s), `catalog` (404s
only) and `gateway` (502 x2 on the newer http.response.status_code key). `traces-<service>.json`
is everything Jaeger held for the service (spans cut to the fields the skill reads); the tests
answer each tag query the way Jaeger does (a trace matches when a span's tag equals the value as a
string). The script runs through the SDK's test context (`ctx.run` raises on a non-zero exit like
the real runner; `stub_commands` matches the exact kubectl argv, so an unexpected call fails)."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest
import yaml
from k8s_jaeger_health import fetch, http_errors, k8s, proxy, traces
from k8s_jaeger_health.common import parse_duration
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.project import POD_FIELDS, WORKLOAD_FIELDS, Projection
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-jaeger-health"
DATA = Path(__file__).parent / "data" / "k8s_jaeger_health"
UTC = timezone.utc
NS = "rw-jaeger"
POD = "jaeger-f6bd9dd68-gr6tn"
CAPTURED = datetime(2026, 11, 3, 23, 32, 28, tzinfo=UTC)  # just after the data was captured
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
CODES = [400, 401, 403, 404, 429, 500, 502, 503, 504]
SETTINGS = {"maxItems": 200, "statusCodes": CODES}
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
UNAVAILABLE = (
    "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
)
NO_ROUTE = "Error from server (NotFound): the server could not find the requested resource"
SERVICES = ("catalog", "checkout", "gateway", "jaeger-all-in-one")  # the last has no scenario spans
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
        for port in pod["spec"]["containers"][0].get("ports", []):
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
    return (
        "kubectl",
        "get",
        "--raw",
        f"/api/v1/namespaces/{NS}/pods/{scheme}:{POD}:{port}/proxy{path}",
        "--request-timeout=30s",
    )


def first_start(trace):
    return min(s["startTime"] for s in trace["spans"])


def tag_matches(trace, key, code):
    return any(
        t["key"] == key and str(t["value"]) == str(code)
        for span in trace["spans"]
        for t in span["tags"]
    )


def jaeger_answer(doc, start, end, limit, key, code):
    """What Jaeger returns for one tag query: the traces in the window with a span whose tag `key`
    equals `code` as a string, newest first, at most `limit`."""
    hits = [t for t in doc["data"] if tag_matches(t, key, code) and start <= first_start(t) <= end]
    hits.sort(key=lambda t: -first_start(t))
    return {**doc, "data": hits[:limit]}


def stubs(now, since, limit, fail=None, replies=None, docs=None, pods=None, answer_all=None, **kw):
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
    for service in (*SERVICES, "x"):
        for key in traces.STATUS_KEYS:
            for code in CODES:
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
        table = stubs(now, since, inputs.get("limit", 100), **inputs_of(inputs), **kw)
        calls = stub_commands(tc, table)
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


def span_tags(status, **extra):
    tags = [{"key": "http.status_code", "type": "int64", "value": status}]
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
    assert " " not in path and "{" not in path  # percent-encoded: safe as a --raw argument


def test_the_window_is_microseconds_before_the_reference_time():
    assert micros(CAPTURED) == 1793748748000000
    env, calls = run()
    start, end = micros(CAPTURED - timedelta(hours=1)), micros(CAPTURED)
    literal = (
        f"/api/traces?service=checkout&start={start}&end={end}&limit=100"
        "&tags=%7B%22http.status_code%22%3A%22500%22%7D"
    )
    assert raw(literal) in calls
    assert env["outputs"]["httpErrors"]["window"] == {
        "since": 3600,
        "start": "2026-11-03T22:32:28Z",
        "end": "2026-11-03T23:32:28Z",
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
        ({"http.target": "/items/9?debug=1"}, "/items/9"),
        ({"url.path": "/api", "http.target": "/other?x"}, "/api"),
        ({"http.route": "/items/{id}"}, "/items/{id}"),
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
    for bad in (0, 1501, "5", True):
        with pytest.raises(RwError):
            http_errors.check_limit(bad)
    for text in ("", "soon", "0s", "1h30"):
        with pytest.raises(RwError):
            parse_duration(text)
    assert parse_duration("1h30m") == timedelta(minutes=90)


class FakeRun:
    def __init__(self, rc, out="", err=""):
        self.result = type("R", (), {"returncode": rc, "stdout": out, "stderr": err})

    def run(self, argv, check=True, **_):
        return self.result


def test_classify_and_proxy_get_tell_the_statuses_apart():
    assert k8s.classify(1, UNREACHABLE) == "unreachable"
    assert k8s.classify(1, FORBIDDEN.format(r="pods/proxy", v="get")) == "permission_denied"
    assert k8s.classify(1, 'Error from server (NotFound): pods "x" not found') == "not_found"
    assert proxy.proxy_get(FakeRun(0, "ok"), "/x") == proxy.Reply(200, "ok")
    assert proxy.proxy_get(FakeRun(1, "", NO_ROUTE), "/x").status == 404
    assert proxy.proxy_get(FakeRun(1, "", UNAVAILABLE), "/x").status == 503
    with pytest.raises(RwError) as exc:
        proxy.proxy_get(FakeRun(1, "", UNREACHABLE), "/x")
    assert exc.value.code == "unreachable"


def test_workload_and_pod_reads_are_projected_so_no_env_value_leaves_the_cluster():
    _, calls = run()
    reads = [c for c in calls if "--raw" not in c]
    assert reads and all(c[-2] == "-o" and c[-1].startswith("jsonpath=") for c in reads)
    for projection in (WORKLOAD_FIELDS, POD_FIELDS):
        template = projection.template(True)
        assert "env" not in template and "annotations" not in template
    # the container ports are the only part of spec.containers that is read
    assert POD_FIELDS.template(False).count("spec.containers") == 1


def test_projection_parses_what_kubectl_prints_and_refuses_anything_else():
    doc = POD_FIELDS.parse((DATA / "pods.txt").read_text(), True)
    [pod] = doc["items"]
    assert pod["metadata"] == {"name": POD}
    assert pod["status"]["phase"] == "Running"
    assert pod["spec"]["containers"][0]["ports"][0] == {"name": "query", "containerPort": 16686}
    workload = WORKLOAD_FIELDS.parse((DATA / "workload.txt").read_text(), False)
    assert workload["kind"] == "Deployment" and workload["spec"]["replicas"] == 1
    assert workload["spec"]["selector"]["matchLabels"]["app"] == "jaeger"
    assert Projection(strings=("a.b",)).parse("@\ns:a.b=\n", False) == {}  # absent: left out
    for text in ("", "x\n", "@\nj:a=not json\n", "@\np:x=notaport\n", "s:a=1\n"):
        with pytest.raises(RwError) as exc:
            Projection(strings=("a",), values=("a",), ports=True).parse(text, False)
        assert exc.value.code == "internal"


# ------------------------------------------------------------ script ----


def test_broken_reports_5xx_and_4xx_per_service_and_operation():
    env, calls = run()
    assert env["status"] == "ok" and env["errors"] == []
    check_schema(env)
    out = env["outputs"]["httpErrors"]
    assert (out["serverErrorSpans"], out["clientErrorSpans"]) == (8, 6)
    assert out["serviceCount"] == 4 and out["queriedCount"] == 4 and out["truncated"] is False
    assert [s["name"] for s in out["services"]] == [
        "checkout",
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
    # one query per service, status attribute key and code
    queries = [c for c in calls if "/api/traces" in c[3]]
    assert len(queries) == 4 * 2 * len(CODES)


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
    assert op["urlPaths"] == ["/items/9"]  # `?debug=1` is gone


def test_a_service_with_no_error_spans_has_zero_counts_and_no_operations():
    env, _ = run(answer_all=(0, json.dumps(EMPTY), ""))
    assert env["status"] == "ok"
    check_schema(env)
    out = env["outputs"]["httpErrors"]
    assert out["serverErrorSpans"] == 0 and out["clientErrorSpans"] == 0
    assert all(
        s["queried"] and s["operations"] == [] and s["tracesSeen"] == 0 for s in out["services"]
    )


def test_no_tag_value_or_query_string_leaves_the_task():
    env, _ = run()
    text = json.dumps(env)
    for leaked in (
        "not-a-real-token",
        "not-a-real-key",
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
        "byStatus",
        "urlPaths",
        "urlPathsTruncated",
        "sampleServerTraceIds",
        "sampleClientTraceIds",
        "truncated",
    }


def test_outside_the_window_nothing_counts_even_if_jaeger_returns_the_trace():
    """A trace that matched on one span may carry spans outside the window: only spans inside it
    count."""
    later = CAPTURED + timedelta(hours=2)
    replies = {}
    for service in SERVICES:
        doc = trace_doc(service)
        for key in traces.STATUS_KEYS:
            for code in CODES:
                hits = [t for t in doc["data"] if tag_matches(t, key, code)]
                replies[(service, key, code)] = (0, json.dumps({**doc, "data": hits}), "")
    env, _ = run(now=later, replies=replies)
    out = env["outputs"]["httpErrors"]
    assert out["window"]["startMicros"] == micros(later - timedelta(hours=1))
    assert out["serverErrorSpans"] == 0 and out["clientErrorSpans"] == 0
    assert service_row(env, "checkout")["tracesSeen"] > 0


def test_only_listed_codes_of_the_service_itself_are_counted():
    doc = load("traces-checkout.json")
    trace = doc["data"][0]
    span = trace["spans"][0]
    trace["processes"]["p2"] = {"serviceName": "other", "tags": []}
    other = {**span, "spanID": "ff01", "processID": "p2", "tags": span_tags(500)}
    teapot = {**span, "spanID": "ff02", "tags": span_tags(418)}
    as_text = {
        **span,
        "spanID": "ff03",
        "operationName": "textual",
        "tags": [{"key": "http.status_code", "type": "string", "value": "500"}],
    }
    trace["spans"] += [other, teapot, as_text]
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    row = service_row(env, "checkout")
    assert row["serverErrorSpans"] == 7  # 6 + the string "500"; not the other service's, not 418
    assert next(o for o in row["operations"] if o["operation"] == "textual")["byStatus"] == {
        "500": 1
    }


def test_a_span_found_by_two_queries_is_counted_once():
    doc = load("traces-checkout.json")
    both = next(t for t in doc["data"] if tag_matches(t, "http.status_code", 500))
    both["spans"][0]["tags"].append(
        {"key": "http.response.status_code", "type": "int64", "value": 500}
    )
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"]})
    assert operation(env, "checkout", "POST /pay")["serverErrorSpans"] == 6


def test_limit_reached_marks_the_counts_as_a_lower_bound():
    env, _ = run(inputs={"limit": 2, "services": ["checkout"]})
    row = service_row(env, "checkout")
    assert row["limitReached"] is True and env["outputs"]["httpErrors"]["limit"] == 2
    assert row["serverErrorSpans"] == 4  # 2 of the four 500s and both 503s
    env, _ = run(inputs={"limit": 100, "services": ["checkout"]})
    assert service_row(env, "checkout")["limitReached"] is False


def test_services_inputs_unknown_and_excluded_names_are_facts():
    env, _ = run(inputs={"services": ["checkout", "nope"], "exclude_services": ["checkout"]})
    out = env["outputs"]["httpErrors"]
    assert env["status"] == "ok" and out["services"] == []
    assert out["unknownServices"] == ["nope"] and out["excludedServices"] == ["checkout"]
    env, _ = run(inputs={"exclude_services": ["checkout", "gateway"]})
    assert [s["name"] for s in env["outputs"]["httpErrors"]["services"]] == [
        "catalog",
        "jaeger-all-in-one",
    ]
    check_schema(env)


def test_no_services_in_jaeger_is_a_fact():
    env, calls = run(replies={"/api/services": (0, '{"data":null,"total":0}', "")})
    assert env["status"] == "ok"
    out = env["outputs"]["httpErrors"]
    assert out["serviceCount"] == 0 and out["services"] == []
    assert not [c for c in calls if "/api/traces" in c[3]]


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
    env, _ = run(docs={"checkout": doc}, inputs={"services": ["checkout"], "limit": 1500})
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
        t["spans"][0]["tags"] = span_tags(500, **{"http.target": f"/x/{i}"})
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
    env, _ = run(replies={"/api/services": (1, "", NO_ROUTE)})
    assert env["status"] == "failed" and codes_of(env) == [("dependency", None)]


def test_one_service_failing_is_one_error_and_the_others_still_report():
    env, _ = run(replies={("catalog", "http.status_code", 404): (0, "<html>login</html>", "")})
    assert env["status"] == "partial" and codes_of(env) == [("internal", "service/catalog")]
    check_schema(env)
    catalog = service_row(env, "catalog")
    assert catalog["queried"] is False and catalog["serverErrorSpans"] is None
    assert catalog["operations"] == []
    assert env["outputs"]["httpErrors"]["queriedCount"] == 3
    assert [s["name"] for s in env["outputs"]["httpErrors"]["services"]][-1] == "catalog"
    assert service_row(env, "checkout")["serverErrorSpans"] == 6


def test_jaeger_errors_in_a_200_body_are_an_error_never_no_errors():
    body = json.dumps({"data": None, "errors": [{"code": 400, "msg": "invalid tags"}]})
    env, _ = run(answer_all=(0, body, ""))
    assert env["status"] == "failed" and codes_of(env) == [("internal", "service/catalog")]
    assert "invalid tags" in env["errors"][0]["message"]


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
    assert not [c for c in calls if "--raw" in c]
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


WORKLOAD = {"kind": "Deployment", "metadata": {"name": "jaeger", "namespace": NS}}
PODS = [{"metadata": {"name": POD}, "status": {"phase": "Running"}, "spec": {}}]
PARAMS = {"statusCodes": [500], "limit": 100, "services": [], "excludeServices": []}
TIMEOUT = "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)"


def test_the_deadline_leaves_the_services_not_asked_a_timeout_error():
    names = json.dumps({"data": ["a", "b", "c", "d"]})
    ctx = Clocked(lambda i, argv: (0, names, "") if i == 0 else (0, json.dumps(EMPTY), ""), step=30)
    budget = fetch.Budget(clock=ctx.clock)
    out = http_errors.view(
        ctx, WORKLOAD, PODS, "http", None, timedelta(hours=1), CAPTURED, PARAMS, 200, budget
    )
    # /api/services at 0s, then two asks (one per attribute key) per service at 30s each: the
    # 100s deadline falls inside service b
    assert [s["queried"] for s in out["services"]] == [True, False, False, False]
    assert [s for _, s, _ in ctx.errors] == ["service/b", "service/c", "service/d"]
    assert all(code == "timeout" and "deadline" in msg for code, _, msg in ctx.errors)
    assert out["queriedCount"] == 1


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
    assert [s["queried"] for s in out["services"]] == [True, False, False, False, False]
    assert [c for c, _, _ in ctx.errors] == ["timeout"] * 4
    assert "consecutive" in ctx.errors[3][2]
    assert len(ctx.calls) == 6  # services, a (two queries), then b, c and d timing out; e not asked


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
    path = "/api/v1/namespaces/n/pods/http:p:16686/proxy/api/services"
    assert rbac_needs(["kubectl", "get", "--raw", path]) == {("", "pods/proxy", "get")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "-n", "x", "delete", "pods", "p"])
