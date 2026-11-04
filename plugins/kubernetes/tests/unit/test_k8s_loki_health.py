"""Unit tests for k8s-loki-health's lib/ and scripts. Captured JSON comes from the `rw-loki`
namespace of the kind-rw-plugins scenario (plugins/kubernetes/tests/kind/scenarios/
30-k8s-loki-health.yaml): a healthy single-binary Loki StatefulSet and `loki-stale`, a
Deployment whose ingester never heartbeats (its ring member is UNHEALTHY and /ready is 503).
Ring token lists in the data are cut to three entries. The scripts run through the SDK's test
context (`ctx.run` raises on a non-zero exit like the real runner; `stub_commands` matches the
exact kubectl argv, so an unexpected call fails). The pod proxy reads go through
`ctx.k8s_proxy_get`, stood in for by `proxy_fake.ProxyFake`."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_loki_health import fetch, k8s, proxy, ready, ring
from k8s_loki_health.errors import RwError
from proxy_fake import ProxyFake, api_error, cross_host_redirect, reply
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-loki-health"
DATA = Path(__file__).parent / "data" / "k8s_loki_health"
UTC = timezone.utc
NS = "rw-loki"
CAPTURED = datetime(2026, 11, 3, 22, 44, 50, tzinfo=UTC)  # just after the data was captured
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
SCRIPTS = {"ready": "ready", "ring": "ring"}
SETTINGS = {"maxItems": 200, "ringPaths": ["/ring", "/distributor/ring", "/compactor/ring"]}
FORBIDDEN = (
    'Error from server (Forbidden): {r} is forbidden: User "u" cannot {v} resource "{r}" '
    'in API group "" in the namespace "rw-loki"'
)
UNREACHABLE = "Error from server (ServiceUnavailable): error trying to reach service: dial tcp 10.244.0.5:3100: connect: connection refused"
NOT_READY = "Ingester not ready: waiting for 15s after being ready"
# what a test writes as (rc, stdout, stderr): a backend 503 / 404, or an API-server error
UNAVAILABLE = (1, "", "backend 503")
NO_ROUTE = (1, "", "backend 404")
RING_PATHS = ("/ring", "/distributor/ring", "/compactor/ring")


def load(name):
    text = (DATA / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"loki_{name}", SKILL / "scripts" / f"{SCRIPTS[name]}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def served_as(answer):
    """The ctx.k8s_proxy_get answer for a test's (rc, stdout, stderr): a body for 0, a backend
    503 / 404 for UNAVAILABLE / NO_ROUTE, else the API-server error the stderr names."""
    if isinstance(answer, (dict, str)):
        return answer
    rc, out, err = answer
    if rc == 0:
        return out
    if answer == UNAVAILABLE:
        return reply(503, NOT_READY)
    if answer == NO_ROUTE:
        return reply(404, "404 page not found")
    return api_error(err, k8s.classify)


TARGETS = {
    "loki": (
        "statefulset",
        "statefulsets.apps",
        "app.kubernetes.io/component=single-binary,app.kubernetes.io/name=loki",
        "pods_loki.json",
        "statefulset_loki.json",
    ),
    "loki-stale": (
        "deployment",
        "deployments.apps",
        "app.kubernetes.io/name=loki-stale",
        "pods_loki-stale.json",
        "deployment_loki-stale.json",
    ),
}
HEALTHY_REPLIES = {
    "/ready": (0, load("ready.txt"), ""),
    "/ring": (0, json.dumps(load("ring.json")), ""),
    "/distributor/ring": (0, json.dumps(load("distributor-ring.json")), ""),
    "/compactor/ring": (0, json.dumps(load("compactor-ring.json")), ""),
}
STALE_REPLIES = {
    "/ready": UNAVAILABLE,
    "/ring": (0, json.dumps(load("ring-stale.json")), ""),
    "/distributor/ring": (0, json.dumps(load("ring-stale.json")), ""),
    "/compactor/ring": (0, json.dumps(load("ring-stale.json")), ""),
}


def stubs(name="loki", fail=None, replies=None, pods=None, scheme="http", port=3100):
    """(kubectl answers, proxied answers) for a run on workload `name`. `fail` maps 'workload' or 'pods' to stderr;
    `replies` overrides a path's (rc, stdout, stderr) for every pod, or for one pod as
    (pod, path)."""
    fail, replies = fail or {}, replies or {}
    rtype, noun, selector, pods_file, workload_file = TARGETS[name]
    out, served = {}, {}
    workload_argv = ("kubectl", "-n", NS, "get", noun, name, "-o", "json")
    out[workload_argv] = (
        (1, "", fail["workload"])
        if "workload" in fail
        else (0, json.dumps(load(workload_file)), "")
    )
    pods_doc = load(pods_file) if pods is None else {"items": pods}
    pods_argv = ("kubectl", "-n", NS, "get", "pods", "-l", selector, "-o", "json")
    out[pods_argv] = (1, "", fail["pods"]) if "pods" in fail else (0, json.dumps(pods_doc), "")
    base = HEALTHY_REPLIES if name == "loki" else STALE_REPLIES
    for pod in pods_doc["items"]:
        pod_name = pod["metadata"]["name"]
        for path, default in base.items():
            reply = replies.get((pod_name, path), replies.get(path, default))
            answer = (1, "", fail["proxy"]) if "proxy" in fail else reply
            served[(pod_name, scheme, port, path)] = served_as(answer)
    return out, served


LAST = {}  # the proxied requests of the last run()


def run(task, name="loki", now=CAPTURED, res=None, settings=None, inputs=None, **kw):
    """(envelope, calls) of one task on workload `name`."""
    rtype, noun = TARGETS[name][0], TARGETS[name][1]
    resource = res or {
        "cluster": "kind",
        "namespace": NS,
        "name": name,
        "type": f"kubernetes/{rtype}",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/{noun.split('.')[0]}/{name}",
    }
    inputs = inputs or {}
    with make_context(resource=resource, settings=settings or SETTINGS, now=now) as tc:
        table, served = stubs(
            name, scheme=inputs.get("scheme", "http"), port=inputs.get("port", 3100), **kw
        )
        fake = ProxyFake(lambda r: served[(r["name"], r["scheme"], r["port"], r["path"])])
        calls = stub_commands(tc, table)
        tc.ctx.k8s_proxy_get = fake
        env = run_main(script(task), tc, **inputs)
    LAST["proxy"] = fake.requests
    return env, calls


def schema_for(task, output):
    return json.loads((SKILL / TASKS[task]["outputs"][output]["schema"]).read_text())


def check_schemas(task, env):
    for name, value in env["outputs"].items():
        jsonschema.validate(value, schema_for(task, name), cls=jsonschema.Draft202012Validator)


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def ring_of(env, path):
    return next(r for r in env["outputs"]["rings"]["rings"] if r["path"] == path)


# ------------------------------------------------------------ lib ----


def test_ready_reply_is_never_true_for_unexpected_text():
    assert ready.ready_reply(proxy.Reply(200, "ready\n"), "w") == (True, "ready")
    assert ready.ready_reply(proxy.Reply(503, ""), "w") == (False, None)
    assert ready.ready_reply(proxy.Reply(503, NOT_READY + "\n"), "w") == (False, NOT_READY)
    for bad in (
        proxy.Reply(200, "<html>Sign in</html>"),
        proxy.Reply(200, "Ingester not ready"),
        proxy.Reply(200, ""),
        proxy.Reply(404, ""),
    ):
        with pytest.raises(RwError):
            ready.ready_reply(bad, "w")


def test_ring_members_rejects_what_is_not_ring_json():
    shards, server_now = ring.ring_members(proxy.Reply(200, json.dumps(load("ring.json"))), "w")
    assert shards[0]["state"] == "ACTIVE" and server_now == load("ring.json")["now"]
    for body in ("<html><body><h1>Cortex Ruler Status</h1></body></html>", "[]", '{"x":1}', ""):
        with pytest.raises(RwError) as exc:
            ring.ring_members(proxy.Reply(200, body), "w")
        assert exc.value.code == "internal"
    with pytest.raises(RwError) as exc:
        ring.ring_members(proxy.Reply(503, ""), "w")
    assert exc.value.code == "dependency"


def test_selector_string_and_port():
    assert fetch.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    sel = {
        "matchExpressions": [
            {"key": "k", "operator": "In", "values": ["x", "y"]},
            {"key": "n", "operator": "NotIn", "values": ["z"]},
            {"key": "e", "operator": "Exists"},
            {"key": "d", "operator": "DoesNotExist"},
        ]
    }
    assert fetch.selector_string(sel) == "k in (x,y),n notin (z),e,!d"
    with pytest.raises(RwError):
        fetch.selector_string({})
    pod = load("pods_loki.json")["items"][0]
    assert fetch.pod_port(pod) == 3100 and fetch.pod_port(pod, 8080) == 8080
    pod["spec"]["containers"][0]["ports"] = [{"name": "http-metrics", "containerPort": 4000}]
    assert fetch.pod_port(pod) == 4000


def test_check_inputs_before_any_call():
    for kw in ({"scheme": "ftp"}, {"scheme": "http", "port": 0}, {"scheme": "http", "port": "80"}):
        with pytest.raises(RwError) as exc:
            fetch.check_inputs(kw["scheme"], kw.get("port"))
        assert exc.value.code == "invalid_input"
    for path in ("ring", "/ring?x=1", "/a/../b", "/ring;cat"):
        with pytest.raises(RwError):
            fetch.check_inputs("http", None, [path])
    fetch.check_inputs("https", 3100, list(RING_PATHS))


def test_classify():
    assert k8s.classify(1, UNREACHABLE) == "unreachable"
    assert (
        k8s.classify(
            1,
            "Unable to connect to the server: net/http: request canceled "
            "(Client.Timeout exceeded while awaiting headers)",
        )
        == "timeout"
    )
    assert k8s.classify(1, FORBIDDEN.format(r="pods/proxy", v="get")) == "permission_denied"
    assert k8s.classify(1, 'Error from server (NotFound): pods "x" not found') == "not_found"


class FakeProxyCtx:
    def __init__(self, answer):
        self.k8s_proxy_get = ProxyFake(answer)


def ask(answer, path="/x"):
    return proxy.proxy_get(FakeProxyCtx(answer), "ns", "loki-0", "http", 3100, path)


def test_proxy_get_returns_the_backends_own_status_and_body():
    assert ask("ok") == proxy.Reply(200, "ok")
    assert ask(reply(404, "404 page not found")) == proxy.Reply(404, "404 page not found")
    assert ask(reply(503, NOT_READY)) == proxy.Reply(503, NOT_READY)


def test_proxy_get_asks_the_pod_proxy_with_scheme_port_path_and_timeout():
    ctx = FakeProxyCtx("ok")
    proxy.proxy_get(ctx, "ns", "loki-0", "https", 8080, "/ready", 7)
    assert ctx.k8s_proxy_get.requests == [
        {
            "namespace": "ns",
            "kind": "pods",
            "name": "loki-0",
            "port": 8080,
            "path": "/ready",
            "scheme": "https",
            "timeout": 7,
        }
    ]


def test_proxy_get_raises_what_the_api_server_answers_itself():
    for stderr, code in (
        (UNREACHABLE, "unreachable"),
        (FORBIDDEN.format(r="pods/proxy", v="get"), "permission_denied"),
        ('Error from server (NotFound): pods "x" not found', "not_found"),
    ):
        with pytest.raises(RwError) as exc:
            ask(api_error(stderr, k8s.classify))
        assert exc.value.code == code


def test_a_cross_host_redirect_is_reported_and_never_followed():
    """S17: the helper drops the Location; the skill reports the host and asks nothing more."""
    ctx = FakeProxyCtx(cross_host_redirect("sso.example.invalid"))
    got = proxy.proxy_get(ctx, "ns", "loki-0", "http", 3100, "/ready")
    assert got.status == 302 and got.redirect_dropped == "sso.example.invalid"
    assert len(ctx.k8s_proxy_get.requests) == 1
    for call in (lambda: ready.ready_reply(got, "/ready"), lambda: ring.ring_members(got, "/ring")):
        with pytest.raises(RwError) as exc:
            call()
        assert exc.value.code == "dependency"
        assert "off cluster to sso.example.invalid (not followed)" in exc.value.message


def test_a_same_pod_redirect_is_followed_once_and_no_more():
    ctx = FakeProxyCtx(
        lambda r: reply(301, "", location="/loki/ready") if r["path"] == "/ready" else "ready"
    )
    assert proxy.proxy_get(ctx, "ns", "loki-0", "http", 3100, "/ready").body == "ready"
    assert ctx.k8s_proxy_get.paths() == ["/ready", "/loki/ready"]
    loop = FakeProxyCtx(lambda r: reply(302, "", location="/again"))
    assert proxy.proxy_get(loop, "ns", "loki-0", "http", 3100, "/a").status == 302
    assert len(loop.k8s_proxy_get.requests) == 2


def test_a_cross_host_redirect_through_the_ready_task_is_an_error_and_asks_once():
    env, _ = run("ready", replies={"/ready": cross_host_redirect("sso.example.invalid")})
    assert env["status"] == "failed" and codes(env) == [("dependency", "pod/loki-0")]
    assert "sso.example.invalid" in env["errors"][0]["message"]
    assert len(LAST["proxy"]) == 1


def test_a_cut_body_is_internal_never_a_partial_parse():
    with pytest.raises(RwError) as exc:
        ask(reply(200, '{"shards": [', truncated=True))
    assert exc.value.code == "internal" and exc.value.hint


# ------------------------------------------------------------ scripts: ready ----


def test_ready_healthy_statefulset():
    env, calls = run("ready")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("ready", env)
    out = env["outputs"]["ready"]
    assert out["workload"] == {
        "kind": "StatefulSet",
        "name": "loki",
        "replicas": 1,
        "readyReplicas": 1,
    }
    assert (out["readyCount"], out["notReadyCount"]) == (1, 0)
    [pod] = out["pods"]
    assert (pod["ready"], pod["httpStatus"], pod["body"], pod["port"]) == (True, 200, "ready", 3100)
    assert [(r["name"], r["path"], r["scheme"], r["port"]) for r in LAST["proxy"]] == [
        ("loki-0", "/ready", "http", 3100)
    ]


def test_ready_503_is_not_ready_not_an_error_even_though_the_pod_is_ready_for_kubernetes():
    env, _ = run("ready", name="loki-stale")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("ready", env)
    [pod] = env["outputs"]["ready"]["pods"]
    assert pod["ready"] is False and pod["httpStatus"] == 503 and pod["podReady"] is True
    assert pod["body"] == NOT_READY  # the reason Loki gives, which `kubectl get --raw` dropped
    assert env["outputs"]["ready"]["notReadyCount"] == 1


def test_ready_a_reply_that_is_not_ready_text_is_an_error_never_ready():
    env, _ = run("ready", replies={"/ready": (0, "<html>login</html>", "")})
    assert env["status"] == "failed" and codes(env) == [("internal", "pod/loki-0")]
    assert env["outputs"] == {}


def test_ready_one_dead_pod_is_one_error_and_the_other_still_reports():
    second = json.loads(json.dumps(load("pods_loki.json")["items"][0]))
    second["metadata"]["name"] = "loki-1"
    pods = [load("pods_loki.json")["items"][0], second]
    env, _ = run("ready", pods=pods, replies={("loki-1", "/ready"): (1, "", UNREACHABLE)})
    assert env["status"] == "partial" and codes(env) == [("unreachable", "pod/loki-1")]
    rows = {p["name"]: p for p in env["outputs"]["ready"]["pods"]}
    assert rows["loki-0"]["ready"] is True and rows["loki-1"]["ready"] is None
    assert env["outputs"]["ready"]["readyCount"] == 1


def test_ready_every_pod_failing_fails_the_task():
    env, _ = run("ready", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env) == [("unreachable", "pod/loki-0")]


def test_ready_pending_pod_is_listed_and_not_asked_and_no_pods_is_a_fact():
    pod = load("pods_loki.json")["items"][0]
    pod["status"]["phase"] = "Pending"
    env, calls = run("ready", pods=[pod])
    [row] = env["outputs"]["ready"]["pods"]
    assert row["queried"] is False and row["ready"] is None
    assert not [c for c in calls if "--raw" in c] and LAST["proxy"] == []
    env, _ = run("ready", pods=[])
    assert env["status"] == "ok"
    assert (
        env["outputs"]["ready"]["podCount"] == 0
        and env["outputs"]["ready"]["workload"]["replicas"] == 1
    )


def test_ready_inputs_choose_the_port_and_scheme():
    env, calls = run("ready", inputs={"port": 8080, "scheme": "https"})
    assert env["status"] == "ok"
    assert [(r["scheme"], r["port"]) for r in LAST["proxy"]] == [("https", 8080)]


# ------------------------------------------------------------ scripts: ring ----


def test_ring_healthy_single_binary_serves_all_three_rings():
    env, calls = run("ring")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("ring", env)
    for path in RING_PATHS:
        r = ring_of(env, path)
        assert r["served"] is True and r["viewedFrom"] == "loki-0" and r["unservedBy"] == []
        assert r["byState"] == {"ACTIVE": 1} and r["notActiveCount"] == 0
    member = ring_of(env, "/ring")["members"][0]
    assert member["id"] == "loki-0" and member["tokenCount"] == 3 and member["zone"] is None
    assert member["lastHeartbeat"] == "2026-11-03T22:44:40Z"
    assert member["heartbeatAgeSeconds"] == 5.0  # against the ring page's own `now`
    assert member["registeredAt"] == "2026-11-03T22:44:10Z" and member["readOnly"] is False


def test_ring_unhealthy_member_with_an_old_heartbeat():
    env, _ = run("ring", name="loki-stale")
    assert env["status"] == "ok"
    r = ring_of(env, "/ring")
    assert r["byState"] == {"UNHEALTHY": 1} and r["notActiveCount"] == 1
    [member] = r["members"]
    assert member["state"] == "UNHEALTHY" and member["lastHeartbeat"] == member["registeredAt"]
    assert member["heartbeatAgeSeconds"] == 36.5  # the page's now (22:44:45.527) less 22:44:09


def without_now(path):
    doc = load(path)
    del doc["now"]
    return (0, json.dumps(doc), "")


def test_ring_age_follows_the_ring_now_and_falls_back_to_ctx_now():
    first, _ = run("ring", now=CAPTURED)
    later, _ = run("ring", now=at("2026-11-03T23:44:50Z"))
    # the ring page's own `now` wins: the run's reference time does not move the age
    assert ring_of(first, "/ring")["members"][0]["heartbeatAgeSeconds"] == 5.0
    assert ring_of(later, "/ring")["members"][0]["heartbeatAgeSeconds"] == 5.0
    replies = {"/ring": without_now("ring.json")}
    first, _ = run("ring", now=CAPTURED, replies=replies)
    later, _ = run("ring", now=at("2026-11-03T23:44:50Z"), replies=replies)
    assert first["errors"] == []
    a = ring_of(first, "/ring")["members"][0]["heartbeatAgeSeconds"]
    b = ring_of(later, "/ring")["members"][0]["heartbeatAgeSeconds"]
    assert b - a == 3600 and a == 10.0


def test_ring_unparseable_timestamps_are_reported_not_a_silent_null():
    doc = load("ring.json")
    doc["shards"][0]["timestamp"] = "yesterday"
    doc["shards"][0]["registered_timestamp"] = 12345
    env, _ = run("ring", replies={"/ring": (0, json.dumps(doc), "")})
    assert env["status"] == "partial" and codes(env) == [("internal", "pod/loki-0")]
    assert "2 member timestamp(s) are not RFC 3339" in env["errors"][0]["message"]
    member = ring_of(env, "/ring")["members"][0]
    assert member["lastHeartbeat"] is None and member["heartbeatAgeSeconds"] is None
    assert member["registeredAt"] is None
    check_schemas("ring", env)
    # an unparseable ring `now`: reported, and the run's reference time is used
    doc = load("ring.json")
    doc["now"] = "soon"
    env, _ = run("ring", replies={"/ring": (0, json.dumps(doc), "")})
    assert env["status"] == "partial" and codes(env) == [("internal", "pod/loki-0")]
    assert "now 'soon' is not RFC 3339" in env["errors"][0]["message"]
    assert ring_of(env, "/ring")["members"][0]["heartbeatAgeSeconds"] == 10.0


def test_ring_zero_and_absent_timestamps_are_not_errors():
    doc = load("ring.json")
    doc["shards"][0]["timestamp"] = "0001-01-01T00:00:00Z"
    del doc["shards"][0]["registered_timestamp"]
    env, _ = run("ring", replies={"/ring": (0, json.dumps(doc), "")})
    assert env["status"] == "ok" and env["errors"] == []
    assert ring_of(env, "/ring")["members"][0]["lastHeartbeat"] is None


def test_ring_members_by_state_and_ordering():
    doc = load("ring.json")
    base = doc["shards"][0]
    states = ["ACTIVE", "LEAVING", "JOINING", "UNHEALTHY", "ACTIVE"]
    doc["shards"] = [{**base, "id": f"m{i}", "state": s} for i, s in enumerate(states)]
    env, _ = run("ring", replies={"/ring": (0, json.dumps(doc), "")})
    r = ring_of(env, "/ring")
    assert r["byState"] == {"ACTIVE": 2, "LEAVING": 1, "JOINING": 1, "UNHEALTHY": 1}
    assert r["memberCount"] == 5 and r["notActiveCount"] == 3
    assert [m["state"] for m in r["members"][:3]] != ["ACTIVE"] * 3  # not-active members first
    assert all(m["state"] != "ACTIVE" for m in r["members"][:3])


def test_ring_unserved_paths_are_facts():
    """A pod of a microservices install serves its own component's ring; the others are 404."""
    replies = {
        "/distributor/ring": NO_ROUTE,
        "/compactor/ring": NO_ROUTE,
    }
    env, _ = run("ring", replies=replies)
    assert env["status"] == "ok" and env["errors"] == []
    assert ring_of(env, "/ring")["served"] is True
    for path in ("/distributor/ring", "/compactor/ring"):
        r = ring_of(env, path)
        assert r["served"] is False and r["unservedBy"] == ["loki-0"]
        assert r["members"] == [] and r["memberCount"] == 0 and r["viewedFrom"] is None
    check_schemas("ring", env)


def test_ring_no_path_served_by_any_pod_is_a_dependency_error():
    replies = {p: NO_ROUTE for p in RING_PATHS}
    env, _ = run("ring", replies=replies)
    assert env["status"] == "partial" and codes(env) == [("dependency", "rings")]
    assert (
        "no ring served; is this a Loki component that runs a ring?"
        in (env["errors"][0]["message"])
    )
    assert all(ring_of(env, p)["served"] is False for p in RING_PATHS)
    check_schemas("ring", env)


def test_ring_the_next_pod_serves_a_path_the_first_does_not():
    second = json.loads(json.dumps(load("pods_loki.json")["items"][0]))
    second["metadata"]["name"] = "loki-1"
    pods = [load("pods_loki.json")["items"][0], second]
    env, _ = run("ring", pods=pods, replies={("loki-0", "/compactor/ring"): NO_ROUTE})
    r = ring_of(env, "/compactor/ring")
    assert r["served"] is True and r["viewedFrom"] == "loki-1" and r["unservedBy"] == ["loki-0"]


def test_ring_html_page_is_an_error_never_an_empty_ring():
    html = "<!DOCTYPE html><html><body><h1>Cortex Ruler Status</h1></body></html>"
    replies = {p: (0, html, "") for p in RING_PATHS}
    env, _ = run("ring", replies=replies)
    assert env["status"] == "failed" and codes(env) == [("internal", "pod/loki-0")]
    assert env["outputs"] == {}
    assert "HTML" in env["errors"][0]["message"]


def test_ring_html_page_of_a_real_loki_is_read_into_the_same_members():
    """`ctx.k8s_proxy_get` sends `Accept: */*`, and Loki then serves the ring as HTML (the helper
    cannot ask for JSON): the table is read, heartbeat placed from its "(01:27:30)" on the day of
    the page's own clock."""
    got = ring.ring_members(proxy.Reply(200, load("ring.html")), "w")
    members, server_now = got
    assert server_now == "2026-11-04T01:27:38Z"
    [m] = members
    assert (m["id"], m["state"], m["address"], m["token_count"]) == (
        "loki-0",
        "ACTIVE",
        "10.244.0.5:9095",
        128,
    )
    assert (m["timestamp"], m["registered_timestamp"]) == (
        "2026-11-04T01:27:30Z",
        "2026-11-04T01:24:00Z",
    )
    unparsed = []
    view = ring.member_view(m, at(server_now), unparsed)
    assert unparsed == [] and view["heartbeatAgeSeconds"] == 8.0
    assert (view["tokenCount"], view["zone"], view["readOnly"]) == (128, None, False)


def test_ring_html_page_with_an_unhealthy_member_keeps_the_state_and_the_old_heartbeat():
    members, server_now = ring.ring_members(proxy.Reply(200, load("ring-stale.html")), "w")
    view = ring.member_view(members[0], at(server_now), [])
    assert view["state"] == "UNHEALTHY" and view["heartbeatAgeSeconds"] == 218.0


def test_ring_html_heartbeat_before_midnight_belongs_to_the_day_before():
    page = (
        load("ring.html")
        .replace("2026-11-04 01:27:38", "2026-11-04 00:00:05")
        .replace("(01:27:30)", "(23:59:58)")
    )
    members, _ = ring.ring_members(proxy.Reply(200, page), "w")
    assert members[0]["timestamp"] == "2026-11-03T23:59:58Z"


def test_ring_html_in_another_zone_is_placed_in_utc():
    page = (
        load("ring.html")
        .replace("&#43;0000", "&#43;0200")
        .replace("01:27:38", "03:27:38")
        .replace("(01:27:30)", "(03:27:30)")
    )
    members, server_now = ring.ring_members(proxy.Reply(200, page), "w")
    assert (
        members[0]["timestamp"] == "2026-11-04T01:27:30Z" and server_now == "2026-11-04T01:27:38Z"
    )


def test_ring_html_unparseable_heartbeat_is_reported_through_the_unparsed_list():
    page = load("ring.html").replace("8s ago (01:27:30)", "8s ago")
    members, server_now = ring.ring_members(proxy.Reply(200, page), "w")
    unparsed = []
    view = ring.member_view(members[0], at(server_now), unparsed)
    assert view["lastHeartbeat"] is None and unparsed[0][:2] == ("loki-0", "timestamp")


def test_ring_html_that_is_not_a_ring_table_is_an_error():
    for body in ("<html><table><tr><th>Name</th></tr><tr><td>x</td></tr></table></html>", "<html>"):
        with pytest.raises(RwError) as exc:
            ring.ring_members(proxy.Reply(200, body), "w")
        assert exc.value.code == "internal"


def test_ring_task_reads_the_html_pages_the_helper_gets():
    page = load("ring.html")
    env, _ = run("ring", replies={p: page for p in RING_PATHS})
    assert env["status"] == "ok" and env["errors"] == [], env["errors"]
    check_schemas("ring", env)
    r = ring_of(env, "/ring")
    assert r["served"] is True and r["byState"] == {"ACTIVE": 1} and r["memberCount"] == 1


def test_ring_a_failing_path_is_served_null_and_the_rest_survives():
    env, _ = run("ring", replies={"/compactor/ring": (0, "<html></html>", "")})
    assert env["status"] == "partial" and codes(env) == [("internal", "pod/loki-0")]
    assert ring_of(env, "/compactor/ring")["served"] is None
    assert ring_of(env, "/ring")["served"] is True
    check_schemas("ring", env)


def test_ring_errors_are_one_per_pod_and_code():
    second = json.loads(json.dumps(load("pods_loki.json")["items"][0]))
    second["metadata"]["name"] = "loki-1"
    pods = [load("pods_loki.json")["items"][0], second]
    replies = {("loki-1", p): (1, "", UNREACHABLE) for p in RING_PATHS}
    replies.update({("loki-0", p): NO_ROUTE for p in RING_PATHS})
    env, _ = run("ring", pods=pods, replies=replies)
    assert env["status"] == "partial" and codes(env) == [("unreachable", "pod/loki-1")]
    assert all(ring_of(env, p)["served"] is None for p in RING_PATHS)


def test_ring_all_calls_failing_fails_and_no_running_pod_is_unreachable():
    env, _ = run("ring", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env) == [("unreachable", "pod/loki-0")]
    pod = load("pods_loki.json")["items"][0]
    pod["status"]["phase"] = "Pending"
    env, _ = run("ring", pods=[pod])
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


def test_ring_ring_paths_setting_is_validated_before_any_call():
    env, calls = run("ring", settings={**SETTINGS, "ringPaths": ["/ring", "ring?x=1"]})
    assert env["status"] == "failed" and codes(env) == [("invalid_input", None)]
    assert calls == []


@pytest.mark.parametrize("task", list(SCRIPTS))
@pytest.mark.parametrize("name", list(TARGETS))
def test_not_found_forbidden_and_unreachable_workload(task, name):
    r = TARGETS[name][1]
    env, _ = run(
        task, name, fail={"workload": f'Error from server (NotFound): {r} "{name}" not found'}
    )
    assert env["status"] == "failed" and codes(env) == [("not_found", None)]
    env, _ = run(task, name, fail={"workload": FORBIDDEN.format(r=r, v="get")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(task, name, fail={"pods": FORBIDDEN.format(r="pods", v="list")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(
        task, name, fail={"workload": "Unable to connect to the server: dial tcp: i/o timeout"}
    )
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


@pytest.mark.parametrize("task", list(SCRIPTS))
def test_forbidden_proxy_fails_with_permission_denied(task):
    env, _ = run(task, fail={"proxy": FORBIDDEN.format(r="pods/proxy", v="get")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]


def test_a_target_of_another_type_is_invalid_input():
    res = {"cluster": "kind", "namespace": NS, "name": "x", "type": "kubernetes/daemonset"}
    env, _ = run("ready", res=res)
    assert env["status"] == "failed" and codes(env) == [("invalid_input", None)]


def test_the_type_falls_back_to_the_plural_of_the_path():
    res = {
        "cluster": "kind",
        "namespace": NS,
        "name": "loki",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/statefulsets/loki",
    }
    env, _ = run("ready", res=res)
    assert env["status"] == "ok"


# ------------------------------------------------------------ deadline ----


class Clocked:
    """A ctx whose `k8s_proxy_get` calls each take `step` seconds of a fake clock and answer
    through `answer(index, request)` -> a test's (returncode, stdout, stderr)."""

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


CLIENT_TIMEOUT = (1, "", "net/http: request canceled (Client.Timeout exceeded)")
WORKLOAD = {
    "kind": "StatefulSet",
    "metadata": {"name": "loki", "namespace": NS},
    "spec": {"replicas": 6},
    "status": {"readyReplicas": 6},
}


def loki_pods(n):
    return [
        {"metadata": {"name": f"loki-{i}"}, "status": {"phase": "Running"}, "spec": {}}
        for i in range(n)
    ]


def test_ready_the_task_deadline_leaves_the_remaining_pods_a_timeout_error():
    ctx = Clocked(lambda i, argv: (0, "ready\n", ""))
    budget = fetch.Budget(clock=ctx.clock)
    out = ready.view(ctx, WORKLOAD, loki_pods(6), "http", None, 200, budget)
    # 0s, 30s, 60s, 90s: four asks; the fourth is limited to what is left of the 100s
    assert out["readyCount"] == 4
    assert [c["timeout"] for c in ctx.calls] == [30, 30, 30, 10]
    assert [(c, s) for c, s, _ in ctx.errors] == [
        ("timeout", "pod/loki-4"),
        ("timeout", "pod/loki-5"),
    ]
    assert "100s deadline" in ctx.errors[0][2]
    assert [p["ready"] for p in out["pods"][4:]] == [None, None]


def test_ready_three_consecutive_timeouts_stop_asking():
    ctx = Clocked(lambda i, argv: (0, "ready\n", "") if i == 0 else CLIENT_TIMEOUT, step=1)
    budget = fetch.Budget(clock=ctx.clock)
    out = ready.view(ctx, WORKLOAD, loki_pods(6), "http", None, 200, budget)
    assert len(ctx.calls) == 4 and out["readyCount"] == 1
    assert [c for c, _, _ in ctx.errors] == ["timeout"] * 5
    assert "3 consecutive pods timed out" in ctx.errors[3][2]


RING_OK = (0, json.dumps({"shards": [], "now": "2026-11-03T22:44:45Z"}), "")


def test_ring_the_deadline_leaves_the_remaining_paths_served_null_with_a_timeout_error():
    ctx = Clocked(lambda i, argv: RING_OK)
    budget = fetch.Budget(clock=ctx.clock)
    paths = [f"/r{i}/ring" for i in range(5)]
    out = ring.collect(ctx, WORKLOAD, loki_pods(1), "http", None, paths, CAPTURED, 200, budget)
    assert [r["served"] for r in out["rings"]] == [True, True, True, True, None]
    assert [(c, s) for c, s, _ in ctx.errors] == [("timeout", "pod/loki-0")]
    assert "100s deadline" in ctx.errors[0][2]


def test_ring_three_consecutive_timeouts_stop_asking_the_path():
    ctx = Clocked(lambda i, argv: NO_ROUTE if i == 0 else CLIENT_TIMEOUT, step=1)
    budget = fetch.Budget(clock=ctx.clock)
    out = ring.collect(ctx, WORKLOAD, loki_pods(6), "http", None, ["/ring"], CAPTURED, 200, budget)
    assert len(ctx.calls) == 4  # a 404, three timeouts, then no more asks
    assert out["rings"][0]["served"] is None and out["rings"][0]["unservedBy"] == ["loki-0"]
    assert [(c, s) for c, s, _ in ctx.errors] == [("timeout", f"pod/loki-{i}") for i in range(1, 6)]


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
    assert "--raw" not in argv, argv  # pod proxy reads go through ctx.k8s_proxy_get (S17)
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
    for name in TARGETS:
        _, calls = run(task, name)
        for argv in calls:
            needed |= rbac_needs(list(argv))
        needed |= {("", f"{r['kind']}/proxy", "get") for r in LAST["proxy"]}
    assert needed == granted(task), (
        f"{task}: calls {sorted(needed)}, declares {sorted(granted(task))}"
    )


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "statefulsets.apps", "loki", "-o", "json"]) == {
        ("apps", "statefulsets", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    with pytest.raises(AssertionError):  # S17: a proxied read through kubectl get --raw
        rbac_needs(["kubectl", "get", "--raw", "/api/v1/namespaces/n/pods/p:3100/proxy/ready"])
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "delete", "pods", "p"])


def test_both_tasks_only_read():
    assert all(t["readOnly"] is True for t in TASKS.values())
    assert all(v in ("get", "list") for t in TASKS for (_, _, v) in granted(t))
