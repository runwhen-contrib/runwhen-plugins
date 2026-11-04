"""Unit tests for k8s-prometheus-health's lib/ and scripts. Captured JSON comes from the
`rw-prometheus` namespaces of the kind-rw-plugins scenario
(plugins/kubernetes/tests/kind/scenarios/30-k8s-prometheus-health.*): a plain prom/prometheus
Deployment standing in for the operator's StatefulSet, with synthetic monitoring.coreos.com
CRs. The scripts run through the SDK's test context (`ctx.run` raises on a non-zero exit like the
real runner; `stub_commands` matches the exact kubectl argv, so an unexpected call fails). The
proxied HTTP reads go through `ctx.k8s_proxy_get`, stood in for by `proxy_fake.ProxyFake`."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_prometheus_health import (
    common,
    coverage,
    fetch,
    k8s,
    proxy,
    rules,
    selectors,
    status,
    targets,
)
from k8s_prometheus_health.errors import RwError
from proxy_fake import ProxyFake, api_error, cross_host_redirect, reply
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-prometheus-health"
DATA = Path(__file__).parent / "data" / "k8s_prometheus_health"
UTC = timezone.utc
NS = "rw-prometheus"
CAPTURED = datetime(2026, 11, 3, 22, 30, 40, tzinfo=UTC)  # just after the data was captured
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
SCRIPTS = {
    "server-status": "server_status",
    "targets": "targets",
    "rules": "rules",
    "servicemonitor-coverage": "servicemonitor_coverage",
}
SETTINGS = {"maxItems": 200, "overdueIntervals": 3}


def load(name):
    text = (DATA / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


POD = load("pods.json")["items"][0]["metadata"]["name"]
FORBIDDEN = (
    'Error from server (Forbidden): {r} is forbidden: User "u" cannot {v} resource "{r}" '
    'in API group "" in the namespace "rw-prometheus"'
)
UNREACHABLE = "Error from server (ServiceUnavailable): error trying to reach service: dial tcp 10.244.0.198:9090: connect: connection refused"


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"prom_{name}", SKILL / "scripts" / f"{SCRIPTS[name]}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def review_output(argv_order, deny_namespace="rw-prometheus-apps"):
    """What `kubectl create -f <List of SubjectAccessReviews> -o json` prints: one document each."""
    docs = [
        {"kind": "SubjectAccessReview", "status": {"allowed": ns != deny_namespace}}
        for ns, *_ in argv_order
    ]
    return "\n".join(json.dumps(d) for d in docs)


def stubs(cr="rw-prom", fail=None, replies=None, pods=None, sms=None, namespaces=None):
    """(kubectl answers, proxied answers) for a run on Prometheus `cr`. `fail` maps a key to stderr
    (exit 1); `replies` overrides the body of a proxied path (a str is a 200 body, ('fail', stderr)
    is an API-server error, a dict is a whole reply)."""
    fail = fail or {}
    replies = replies or {}
    out, served = {}, {}
    cr_argv = ("kubectl", "-n", NS, "get", "prometheuses.monitoring.coreos.com", cr, "-o", "json")
    out[cr_argv] = (
        (1, "", fail["cr"]) if "cr" in fail else (0, json.dumps(load(f"prometheus_{cr}.json")), "")
    )
    pod_argv = (
        "kubectl",
        "-n",
        NS,
        "get",
        "pods",
        "-l",
        f"app.kubernetes.io/name=prometheus,prometheus={cr}",
        "-o",
        "json",
    )
    pod_doc = {"items": []} if cr == "rw-prom-idle" else load("pods.json")
    if pods is not None:
        pod_doc = {"items": pods}
    out[pod_argv] = (1, "", fail["pods"]) if "pods" in fail else (0, json.dumps(pod_doc), "")
    defaults = {
        "/-/healthy": load("healthy.txt"),
        "/-/ready": load("ready.txt"),
        "/api/v1/status/runtimeinfo": json.dumps(load("runtimeinfo.json")),
        "/api/v1/status/tsdb": json.dumps(load("tsdb.json")),
        "/api/v1/targets?state=active": json.dumps(load("targets.json")),
        "/api/v1/rules": json.dumps(load("rules.json")),
    }
    for item in pod_doc["items"]:
        name = item["metadata"]["name"]
        for path, body in defaults.items():
            reply = replies.get((name, path), replies.get(path, body))
            if isinstance(reply, tuple):
                served[(name, path)] = api_error(reply[1], k8s.classify)
            elif "proxy" in fail:
                served[(name, path)] = api_error(fail["proxy"], k8s.classify)
            else:
                served[(name, path)] = reply
    sm_doc = load("servicemonitors_all.json") if sms is None else sms
    all_argv = ("kubectl", "get", "servicemonitors.monitoring.coreos.com", "-A", "-o", "json")
    own_argv = ("kubectl", "-n", NS, "get", "servicemonitors.monitoring.coreos.com", "-o", "json")
    out[all_argv] = (1, "", fail["sm-all"]) if "sm-all" in fail else (0, json.dumps(sm_doc), "")
    own = {"items": [i for i in sm_doc["items"] if i["metadata"]["namespace"] == NS]}
    out[own_argv] = (1, "", fail["sm-own"]) if "sm-own" in fail else (0, json.dumps(own), "")
    ns_argv = ("kubectl", "get", "namespaces", "-o", "json")
    ns_doc = load("namespaces.json") if namespaces is None else namespaces
    out[ns_argv] = (
        (1, "", fail["namespaces"]) if "namespaces" in fail else (0, json.dumps(ns_doc), "")
    )
    return out, served


LAST = {}  # facts about the last run() that its return value does not carry


def run(task, cr="rw-prom", now=CAPTURED, res=None, settings=None, review=None, **kw):
    """(envelope, calls) of one task on Prometheus `cr`."""
    resource = res or {
        "cluster": "kind",
        "namespace": NS,
        "name": cr,
        "type": "kubernetes/prometheus",
        "path": f"kubernetes/clusters/kind/namespaces/{NS}/prometheuses/{cr}",
    }
    with make_context(resource=resource, settings=settings or SETTINGS, now=now) as tc:
        table, served = stubs(cr, **kw)
        fake = ProxyFake(lambda r: served[(r["name"], r["path"])])
        review_argv = ("kubectl", "create", "-f", "sar-0.json", "-o", "json")
        table[review_argv] = review or (0, "", "")
        calls = stub_commands(tc, table)
        tc.ctx.k8s_proxy_get = fake
        if review is None and task == "servicemonitor-coverage":
            # answer the review call from the List the script wrote (the file is in ctx.workdir)
            original = tc.ctx.run

            def run_with_reviews(argv, *a, **k):
                if tuple(argv) == review_argv:
                    document = json.loads((tc.ctx.workdir / "sar-0.json").read_text())
                    wanted = [
                        (i["spec"]["resourceAttributes"]["namespace"],) for i in document["items"]
                    ]
                    table[review_argv] = (0, review_output(wanted), "")
                    stub_commands(tc, {review_argv: table[review_argv]})
                return original(argv, *a, **k)

            tc.ctx.run = run_with_reviews
        env = run_main(script(task), tc)
        LAST["sar_left"] = (tc.ctx.workdir / "sar-0.json").exists()
        LAST["proxy"] = fake.requests
    return env, calls


def schema_for(task, output):
    return json.loads((SKILL / TASKS[task]["outputs"][output]["schema"]).read_text())


def check_schemas(task, env):
    for name, value in env["outputs"].items():
        jsonschema.validate(value, schema_for(task, name), cls=jsonschema.Draft202012Validator)


def codes(env):
    return [(e["code"], e.get("scope")) for e in env["errors"]]


def pod_of(env):
    return env["outputs"]["server"]["pods"][0]


# ------------------------------------------------------------ lib: common ----


def test_strip_urls_removes_query_fragment_and_userinfo():
    text = 'Get "http://u:p@host:9100/metrics?token=abc&x=1#f": dial tcp: refused'
    assert common.strip_urls(text) == 'Get "http://host:9100/metrics": dial tcp: refused'
    assert common.strip_urls("https://a/b?c=d and http://e/f") == "https://a/b and http://e/f"
    assert common.strip_urls(None) is None


def test_strip_urls_userinfo_is_greedy_to_the_last_at_sign_before_the_host():
    assert common.strip_urls("http://u:p@ss@h:9/m") == "http://h:9/m"
    assert (
        common.strip_urls('Get "https://tok@en:x@h/m?a=b": refused') == 'Get "https://h/m": refused'
    )
    # an @ in the path is not userinfo and a token in the path is not recognised
    assert common.strip_urls("http://h/a@b/c") == "http://h/a@b/c"
    assert common.strip_urls("http://h/tok123/metrics?x=1") == "http://h/tok123/metrics"


def test_parse_duration_and_time():
    assert common.parse_duration("15s") == 15
    assert common.parse_duration("1m30s") == 90
    assert common.parse_duration("500ms") == 0.5
    assert common.parse_duration("2h") == 7200
    assert common.parse_duration("soon") is None and common.parse_duration(None) is None
    assert common.parse_time("2026-11-03T22:30:32.289007597Z") == at("2026-11-03T22:30:32.289007Z")
    assert common.parse_time("0001-01-01T00:00:00Z") is None
    assert common.parse_time("not a time") is None
    assert common.age_seconds(at("2026-11-03T22:30:00Z"), CAPTURED) == 40.0


def test_truncate_flags_the_cut():
    assert common.truncate("x" * 2000) == ("x" * 1024, True)
    assert common.truncate("short") == ("short", False)
    assert common.truncate(None) == (None, False)


def test_classify():
    assert (
        k8s.classify(1, "Error from server (Forbidden): pods is forbidden") == "permission_denied"
    )
    assert k8s.classify(1, 'Error from server (NotFound): pods "x" not found') == "not_found"
    assert k8s.classify(1, UNREACHABLE) == "unreachable"
    assert (
        k8s.classify(1, "Unable to connect to the server: dial tcp: i/o timeout") == "unreachable"
    )
    assert k8s.classify(1, "net/http: request canceled (Client.Timeout exceeded)") == "timeout"
    assert k8s.classify(124, "") == "timeout"
    assert (
        k8s.classify(
            1,
            "Unable to connect to the server: net/http: request canceled "
            "(Client.Timeout exceeded while awaiting headers)",
        )
        == "timeout"
    )
    assert k8s.classify(1, 'the server doesn\'t have a resource type "x"') == "dependency"
    assert k8s.classify(1, "something odd") == "internal"


# ------------------------------------------------------------ lib: proxy ----


def ask(answer, path="/x"):
    return proxy.proxy_get(FakeProxyCtx(answer), "ns", "p-0", "http", 9090, path)


class FakeProxyCtx:
    def __init__(self, answer):
        self.k8s_proxy_get = ProxyFake(answer)


def test_proxy_get_returns_the_backends_own_status_and_body():
    assert ask("ok") == proxy.Reply(200, "ok")
    assert ask(reply(404, "page not found")) == proxy.Reply(404, "page not found")
    assert ask(reply(503, "Service Unavailable")) == proxy.Reply(503, "Service Unavailable")


def test_proxy_get_asks_the_pod_proxy_with_scheme_port_path_and_timeout():
    ctx = FakeProxyCtx("ok")
    proxy.proxy_get(ctx, "ns", "p-0", "https", 9091, "/-/ready?x=1", 7)
    assert ctx.k8s_proxy_get.requests == [
        {
            "namespace": "ns",
            "kind": "pods",
            "name": "p-0",
            "port": 9091,
            "path": "/-/ready?x=1",
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


def test_a_same_pod_redirect_is_followed_once_and_no_more():
    ctx = FakeProxyCtx(
        lambda r: reply(301, "", location="/prom/-/ready") if r["path"] == "/-/ready" else "ready"
    )
    assert proxy.proxy_get(ctx, "ns", "p-0", "http", 9090, "/-/ready") == proxy.Reply(200, "ready")
    assert ctx.k8s_proxy_get.paths() == ["/-/ready", "/prom/-/ready"]
    loop = FakeProxyCtx(lambda r: reply(302, "", location="/again"))
    assert proxy.proxy_get(loop, "ns", "p-0", "http", 9090, "/a").status == 302
    assert len(loop.k8s_proxy_get.requests) == 2  # one hop, then the 302 is the answer


def test_a_cross_host_redirect_is_reported_and_never_followed():
    """S17: the helper drops the Location; the skill reports the host and asks nothing more."""
    ctx = FakeProxyCtx(cross_host_redirect("sso.example.invalid"))
    got = proxy.proxy_get(ctx, "ns", "p-0", "http", 9090, "/-/ready")
    assert got.status == 302 and got.redirect_dropped == "sso.example.invalid"
    assert len(ctx.k8s_proxy_get.requests) == 1
    with pytest.raises(RwError) as exc:
        proxy.expect_200(got, "targets on pod p-0")
    assert exc.value.code == "dependency"
    assert "off cluster to sso.example.invalid (not followed)" in exc.value.message


def test_a_redirected_probe_is_an_error_never_ready():
    got = proxy.Reply(302, "", "sso.example.invalid")
    with pytest.raises(RwError) as exc:
        status.ready_reply(got, "/-/ready")
    assert "sso.example.invalid" in exc.value.message


def test_a_cross_host_redirect_through_a_task_is_an_error_and_asks_once_per_pod():
    redirect = cross_host_redirect("sso.example.invalid")
    env, _ = run("targets", replies={"/api/v1/targets?state=active": redirect})
    assert env["status"] == "failed" and codes(env)[0][0] == "dependency"
    assert "sso.example.invalid" in env["errors"][0]["message"]
    assert LAST["proxy"] and len(LAST["proxy"]) == len({r["name"] for r in LAST["proxy"]})


def test_a_cut_body_is_internal_never_a_partial_parse():
    with pytest.raises(RwError) as exc:
        ask(reply(200, '{"status":"succ', truncated=True))
    assert exc.value.code == "internal" and exc.value.hint


def test_api_json_rejects_what_is_not_prometheus_json():
    assert proxy.api_json(proxy.Reply(200, '{"status":"success","data":{"a":1}}'), "w") == {"a": 1}
    for body in ("<html>login</html>", '{"status":"error","error":"x"}', "[]", ""):
        with pytest.raises(RwError) as exc:
            proxy.api_json(proxy.Reply(200, body), "w")
        assert exc.value.code == "internal"
    with pytest.raises(RwError) as exc:
        proxy.api_json(proxy.Reply(404, "page not found"), "w")
    assert exc.value.code == "dependency" and "404" in exc.value.message
    assert "page not found" in exc.value.message  # the backend's body is shown


def test_probe_replies_are_never_true_for_unexpected_text():
    assert status.healthy_reply(proxy.Reply(200, "Prometheus Server is Healthy.\n"), "w") is True
    assert status.ready_reply(proxy.Reply(200, "Prometheus Server is Ready.\n"), "w") is True
    assert status.ready_reply(proxy.Reply(503, ""), "w") is False
    for bad in (
        proxy.Reply(200, "<html>sign in</html>"),
        proxy.Reply(200, "Prometheus Server is not Ready"),
        proxy.Reply(404, ""),
    ):
        with pytest.raises(RwError):
            status.ready_reply(bad, "w")


# ------------------------------------------------------------ lib: selectors ----


def test_selectors():
    labels = {"release": "rw", "tier": "a"}
    assert selectors.matches({}, labels)
    assert selectors.matches({"matchLabels": {"release": "rw"}}, labels)
    assert not selectors.matches({"matchLabels": {"release": "x"}}, labels)
    assert selectors.matches(
        {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["a", "b"]}]}, labels
    )
    assert not selectors.matches(
        {"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["a"]}]}, labels
    )
    assert selectors.matches(
        {"matchExpressions": [{"key": "release", "operator": "Exists"}]}, labels
    )
    assert selectors.matches(
        {"matchExpressions": [{"key": "zzz", "operator": "DoesNotExist"}]}, labels
    )
    with pytest.raises(RwError):
        selectors.matches({"matchExpressions": [{"key": "a", "operator": "Weird"}]}, labels)


def prom_with(**spec):
    return {"metadata": {"name": "p", "namespace": NS}, "spec": spec}


def sm_of(name, labels, namespace=NS):
    return {"metadata": {"name": name, "namespace": namespace, "labels": labels}, "spec": {}}


def test_an_unset_service_monitor_selector_selects_nothing_and_an_empty_one_everything():
    sm = sm_of("a", {"x": "y"})
    assert coverage.selection(prom_with(), sm, None)[0] is False
    assert coverage.selection(prom_with(serviceMonitorSelector={}), sm, None) == (True, None)
    wrong_ns = sm_of("a", {}, namespace="other")
    # unset namespace selector: only the Prometheus's own namespace
    assert coverage.selection(prom_with(serviceMonitorSelector={}), wrong_ns, None)[0] is False
    # {} : every namespace
    both = prom_with(serviceMonitorSelector={}, serviceMonitorNamespaceSelector={})
    assert coverage.selection(both, wrong_ns, None) == (True, None)
    # by label, labels unknown: undetermined, never selected
    by_label = prom_with(
        serviceMonitorSelector={}, serviceMonitorNamespaceSelector={"matchLabels": {"m": "1"}}
    )
    assert coverage.selection(by_label, wrong_ns, None)[0] is None
    assert coverage.selection(by_label, wrong_ns, {"other": {"m": "1"}}) == (True, None)
    assert coverage.selection(by_label, wrong_ns, {"other": {}})[0] is False


# ------------------------------------------------------------ lib: targets and rules ----


def target(pool, url, health, error="", scraped="2026-11-03T22:30:30Z", interval="15s", **labels):
    return {
        "scrapePool": pool,
        "scrapeUrl": url,
        "health": health,
        "lastError": error,
        "lastScrape": scraped,
        "scrapeInterval": interval,
        "labels": {"job": pool, "instance": url, **labels},
        "discoveredLabels": {"__param_token": "SECRET"},
    }


def test_targets_merge_replicas_worst_wins_and_secrets_are_not_read():
    a = target("p", "http://h/m?token=SECRET", "up")
    b = target("p", "http://h/m?token=SECRET", "down", 'Get "http://h/m?token=SECRET": refused')
    answers = [("pod-0", [a]), ("pod-1", [b])]
    out = targets.view(answers, CAPTURED, 200, 3)
    assert (out["total"], out["up"], out["down"]) == (1, 0, 1)
    entry = out["unhealthy"][0]
    assert entry["downOnPods"] == ["pod-1"] and entry["seenOnPods"] == 2
    assert entry["scrapeUrl"] == "http://h/m"
    assert "SECRET" not in json.dumps(out) and "token" not in json.dumps(out)


def test_targets_overdue_uses_the_scrape_interval_and_ctx_now():
    fresh = target("p", "http://a/m", "up", scraped="2026-11-03T22:30:30Z")
    stale = target(
        "p", "http://b/m", "up", scraped="2026-11-03T22:29:00Z"
    )  # 100s old, 15s interval
    never = target("p", "http://c/m", "unknown", scraped="0001-01-01T00:00:00Z")
    noint = target("p", "http://d/m", "up", scraped="2026-11-03T22:00:00Z", interval=None)
    out = targets.view([("pod", [fresh, stale, never, noint])], CAPTURED, 200, 3)
    by_url = {t["scrapeUrl"]: t for t in out["unhealthy"]}
    assert set(by_url) == {
        "http://b/m",
        "http://c/m",
    }  # fresh and the no-interval one are not listed
    assert (
        by_url["http://b/m"]["overdue"] is True
        and by_url["http://b/m"]["lastScrapeAgeSeconds"] == 100.0
    )
    assert by_url["http://c/m"]["lastScrape"] is None and by_url["http://c/m"]["overdue"] is None
    assert out["overdue"] == 1


def test_targets_error_is_cut_and_flagged_and_lists_are_capped():
    long = target("p", "http://a/m", "down", "e" * 3000)
    many = [target("p", f"http://h{i}/m", "down", "x") for i in range(5)]
    out = targets.view([("pod", [long, *many])], CAPTURED, 3, 3)
    assert len(out["unhealthy"]) == 3 and out["unhealthyTruncated"] and out["unhealthyCount"] == 6
    cut = [t for t in targets.view([("pod", [long])], CAPTURED, 3, 3)["unhealthy"]][0]
    assert len(cut["lastError"]) == 1024 and cut["lastErrorTruncated"] is True


def test_active_targets_requires_the_list():
    with pytest.raises(RwError):
        targets.active_targets({"droppedTargets": []}, "w")


def group(name, rules_, interval=15, took=0.1, last="2026-11-03T22:30:30Z"):
    return {
        "name": name,
        "file": "f.yml",
        "interval": interval,
        "evaluationTime": took,
        "lastEvaluation": last,
        "rules": rules_,
    }


def test_rules_flag_errors_slow_evaluation_and_overdue_groups():
    ok = {"name": "a", "type": "recording", "health": "ok", "lastError": ""}
    bad = {
        "name": "b",
        "type": "alerting",
        "health": "err",
        "lastError": "boom http://x/y?k=v",
        "query": "up",
    }
    unevaluated = {"name": "c", "type": "recording", "health": "unknown"}
    groups = [
        group("g1", [ok]),
        group("g2", [ok, bad, unevaluated], took=20),
        group("g3", [ok], last="2026-11-03T22:00:00Z"),
    ]
    out = rules.view([("pod", groups)], CAPTURED, 200, 3)
    by_name = {g["name"]: g for g in out["groups"]}
    assert (
        by_name["g1"]["unhealthyRuleCount"] == 0
        and by_name["g1"]["evaluationExceedsInterval"] is False
    )
    assert (
        by_name["g2"]["unhealthyRuleCount"] == 2
        and by_name["g2"]["evaluationExceedsInterval"] is True
    )
    assert (
        by_name["g3"]["evaluationOverdue"] is True and by_name["g1"]["evaluationOverdue"] is False
    )
    assert out["groups"][0]["name"] == "g2"  # failing groups first
    assert [r["name"] for r in out["failingRules"]] == ["b", "c"]
    assert out["failingRules"][0]["lastError"] == "boom http://x/y"
    assert (out["unhealthyGroupCount"], out["failingRuleCount"], out["ruleCount"]) == (1, 2, 5)


def test_rules_merge_replicas_by_group():
    bad = {"name": "b", "type": "recording", "health": "err", "lastError": "x"}
    out = rules.view([("p0", [group("g", [bad])]), ("p1", [group("g", [bad])])], CAPTURED, 200, 3)
    assert out["groupCount"] == 1 and out["failingRuleCount"] == 1
    with pytest.raises(RwError):
        rules.rule_groups({}, "w")


def test_parse_reviews_needs_a_boolean_per_item():
    assert coverage.parse_reviews(
        '{"status":{"allowed":true}}\n{"status":{"allowed":false,"evaluationError":"no opinion"}}',
        2,
    ) == [(True, None), (False, "no opinion")]
    for text, n in (('{"status":{"allowed":true}}', 2), ('{"status":{}}', 1), ("not json", 1)):
        with pytest.raises(RwError):
            coverage.parse_reviews(text, n)


def test_review_list_asks_list_and_watch_for_each_resource():
    prom = prom_with(serviceAccountName="sa1")
    document, order = coverage.review_list(prom, ["n1"])
    users = {i["spec"]["user"] for i in document["items"]}
    assert users == {f"system:serviceaccount:{NS}:sa1"}
    assert {(o[1], o[3]) for o in order} == {
        (r, v) for r in ("services", "pods", "endpoints") for v in ("list", "watch")
    }
    slice_prom = prom_with(serviceDiscoveryRole="EndpointSlice")
    _, order = coverage.review_list(slice_prom, ["n1"])
    assert ("endpointslices", "discovery.k8s.io") in {(o[1], o[2]) for o in order}
    assert coverage.service_account(prom_with()) == (NS, "default")


# ------------------------------------------------------------ scripts: server-status ----


def test_server_status_healthy():
    env, calls = run("server-status")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("server-status", env)
    pod = pod_of(env)
    assert (pod["healthy"], pod["ready"], pod["queried"], pod["port"]) == (True, True, True, 9090)
    assert pod["runtime"]["reloadConfigSuccess"] is True and pod["runtime"]["corruptionCount"] == 0
    assert pod["runtime"]["lastConfigAgeSeconds"] > 0  # from ctx.now(), not the wall clock
    assert pod["tsdb"]["headSeries"] == load("tsdb.json")["data"]["headStats"]["numSeries"]
    assert env["outputs"]["server"]["prometheus"]["routePrefix"] == "/"


def test_server_status_uses_ctx_now_for_ages():
    first, _ = run("server-status", now=CAPTURED)
    later, _ = run("server-status", now=at("2026-11-03T23:30:40Z"))
    a, b = pod_of(first)["runtime"], pod_of(later)["runtime"]
    assert b["uptimeSeconds"] - a["uptimeSeconds"] == 3600


def test_server_status_not_ready_is_false_not_an_error():
    env, _ = run("server-status", replies={"/-/ready": reply(503, "Service Unavailable")})
    assert (
        env["status"] == "ok" and pod_of(env)["ready"] is False and pod_of(env)["healthy"] is True
    )


def test_server_status_reload_failure_and_corruption_are_reported():
    info = load("runtimeinfo.json")
    info["data"].update(reloadConfigSuccess=False, corruptionCount=3)
    env, _ = run("server-status", replies={"/api/v1/status/runtimeinfo": json.dumps(info)})
    runtime = pod_of(env)["runtime"]
    assert runtime["reloadConfigSuccess"] is False and runtime["corruptionCount"] == 3


def test_server_status_a_reply_that_is_not_the_expected_text_is_an_error_never_healthy():
    env, _ = run("server-status", replies={"/-/healthy": "<html>Sign in</html>"})
    assert env["status"] == "failed" and codes(env) == [("internal", f"pod/{POD}")]
    assert env["outputs"] == {}


def test_server_status_one_dead_pod_is_one_error_and_the_other_still_reports():
    other = json.loads(json.dumps(load("pods.json")["items"][0]))
    other["metadata"]["name"] = "prometheus-rw-prom-zzz"
    pods = [load("pods.json")["items"][0], other]
    replies = {
        (other["metadata"]["name"], p): ("fail", UNREACHABLE)
        for p in ("/-/healthy", "/-/ready", "/api/v1/status/runtimeinfo", "/api/v1/status/tsdb")
    }
    env, _ = run("server-status", pods=pods, replies=replies)
    assert env["status"] == "partial"
    assert codes(env) == [("unreachable", "pod/prometheus-rw-prom-zzz")]
    rows = env["outputs"]["server"]["pods"]
    assert rows[0]["healthy"] is True
    assert rows[1]["healthy"] is None and rows[1]["ready"] is None and rows[1]["runtime"] is None


def test_server_status_nothing_answers_fails():
    env, _ = run("server-status", fail={"proxy": UNREACHABLE})
    assert env["status"] == "failed" and codes(env)[0][0] == "unreachable"


def test_server_status_no_queryable_pod_of_a_cr_that_expects_one_is_partial():
    env, _ = run("server-status", cr="rw-prom-idle")
    assert env["status"] == "partial" and codes(env) == [("unreachable", "pods")]
    server = env["outputs"]["server"]
    assert server["podCount"] == 0 and server["podsSkipped"] == 0 and server["pods"] == []
    assert server["prometheus"]["expectedReplicas"] == 1
    check_schemas("server-status", env)


class Errors:
    """A ctx that only records ctx.error (for functions that do no I/O when no pod is asked)."""

    def __init__(self):
        self.errors = []

    def error(self, code, message, scope=None, **_):
        self.errors.append((code, scope))


def test_server_status_expected_replicas_default_to_one_times_shards_and_zero_is_fine():
    ctx = Errors()
    out = status.collect(ctx, prom_with(replicas=0), [], CAPTURED, 200)
    assert out["prometheus"]["expectedReplicas"] == 0 and ctx.errors == []
    out = status.collect(ctx, prom_with(replicas=2, shards=3), [], CAPTURED, 200)
    assert out["prometheus"]["expectedReplicas"] == 6 and ctx.errors == [("unreachable", "pods")]
    ctx = Errors()
    assert (
        status.collect(ctx, prom_with(), [], CAPTURED, 200)["prometheus"]["expectedReplicas"] == 1
    )
    assert ctx.errors == [("unreachable", "pods")]
    # pods exist but none is Running: still nothing queryable
    pod = {"metadata": {"name": "p-0"}, "status": {"phase": "Pending"}}
    ctx = Errors()
    status.collect(ctx, prom_with(replicas=1), [pod], CAPTURED, 200)
    assert ctx.errors == [("unreachable", "pods")]


def test_server_status_reads_the_operator_conditions():
    prom = prom_with(replicas=1)
    prom["status"] = {
        "conditions": [
            {
                "type": "Available",
                "status": "True",
                "reason": "",
                "message": "",
                "lastTransitionTime": "2026-11-03T22:00:00Z",
            },
            {
                "type": "Reconciled",
                "status": "False",
                "reason": "ConfigRejected",
                "message": "m" * 2000,
                "lastTransitionTime": "2026-11-03T22:01:00Z",
            },
            {"type": "Other", "status": "True"},
        ]
    }
    conds = status.prometheus_view(prom)["conditions"]
    assert [(c["type"], c["status"]) for c in conds] == [
        ("Available", "True"),
        ("Reconciled", "False"),
    ]
    assert conds[0]["reason"] is None and conds[0]["message"] is None
    assert conds[1]["reason"] == "ConfigRejected" and len(conds[1]["message"]) == 1024
    assert conds[1]["messageTruncated"] is True
    assert status.prometheus_view(prom_with())["conditions"] == []


def test_server_status_a_pod_that_is_not_running_is_listed_and_not_asked():
    pods = load("pods.json")["items"]
    pods[0]["status"]["phase"] = "Pending"
    env, calls = run("server-status", pods=pods)
    assert pod_of(env)["queried"] is False and pod_of(env)["healthy"] is None
    assert not [c for c in calls if "--raw" in c] and LAST["proxy"] == []


def test_scheme_port_and_route_prefix_follow_the_cr():
    prom = json.loads(json.dumps(load("prometheus_rw-prom.json")))
    prom["spec"].update(portName="reloader", routePrefix="/prom/", web={"tlsConfig": {}})
    pod = json.loads(json.dumps(load("pods.json")["items"][0]))
    pod["spec"]["containers"][0]["ports"] = [{"name": "reloader", "containerPort": 8080}]
    from k8s_prometheus_health import fetch

    assert fetch.endpoint(prom, pod) == ("https", 8080, "/prom")
    assert fetch.endpoint(load("prometheus_rw-prom.json"), {}) == ("http", 9090, "")


# ------------------------------------------------------------ scripts: targets and rules ----


def test_targets_reports_the_down_target_and_the_pools():
    env, calls = run("targets")
    assert env["status"] == "ok"
    check_schemas("targets", env)
    out = env["outputs"]["targets"]
    assert (out["total"], out["up"], out["down"]) == (2, 1, 1)
    pools = {p["scrapePool"]: p for p in out["pools"]}
    assert (
        pools["rw-down"]["down"] == 1 and pools["serviceMonitor/rw-prometheus/sm-ok/0"]["up"] == 1
    )
    [down] = out["unhealthy"]
    assert down["job"] == "rw-down" and "no such host" in down["lastError"]
    assert "token" not in json.dumps(out)  # the relabelled __param_token is never read
    assert down["scrapeUrl"].endswith(":9100/metrics")


def test_targets_a_healthy_prometheus_lists_nothing_unhealthy():
    doc = load("targets.json")
    doc["data"]["activeTargets"] = [t for t in doc["data"]["activeTargets"] if t["health"] == "up"]
    env, _ = run("targets", replies={"/api/v1/targets?state=active": json.dumps(doc)})
    out = env["outputs"]["targets"]
    assert out["total"] == 1 and out["unhealthy"] == [] and out["down"] == 0


def test_targets_a_reply_that_is_not_json_fails_and_is_never_an_empty_list():
    env, _ = run("targets", replies={"/api/v1/targets?state=active": "<html>login</html>"})
    assert env["status"] == "failed" and codes(env)[0][0] == "internal" and env["outputs"] == {}


def test_targets_no_running_pod_is_unreachable_not_zero_targets():
    env, _ = run("targets", cr="rw-prom-idle")
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


def test_rules_reports_the_failing_rule():
    env, _ = run("rules")
    assert env["status"] == "ok"
    check_schemas("rules", env)
    out = env["outputs"]["rules"]
    assert (out["groupCount"], out["unhealthyGroupCount"], out["failingRuleCount"]) == (2, 1, 1)
    [bad] = out["failingRules"]
    assert (
        bad["group"] == "rw-broken"
        and bad["health"] == "err"
        and "same labelset" in bad["lastError"]
    )
    assert out["groups"][0]["name"] == "rw-broken"


def test_rules_no_groups_is_a_fact_but_a_missing_list_is_an_error():
    env, _ = run(
        "rules",
        replies={"/api/v1/rules": json.dumps({"status": "success", "data": {"groups": []}})},
    )
    assert env["outputs"]["rules"]["groupCount"] == 0 and env["status"] == "ok"
    env, _ = run("rules", replies={"/api/v1/rules": json.dumps({"status": "success", "data": {}})})
    assert env["status"] == "failed"


@pytest.mark.parametrize("task", ["server-status", "targets", "rules", "servicemonitor-coverage"])
def test_forbidden_proxy_fails_with_permission_denied(task):
    forbidden = FORBIDDEN.format(r="pods", v="get")
    env, _ = run(task, fail={"proxy": forbidden})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]


@pytest.mark.parametrize("task", list(SCRIPTS))
def test_not_found_and_forbidden_cr(task):
    env, _ = run(
        task,
        cr="rw-prom",
        fail={
            "cr": 'Error from server (NotFound): prometheuses.monitoring.coreos.com "rw-prom" not found'
        },
    )
    assert env["status"] == "failed" and codes(env) == [("not_found", None)]
    env, _ = run(
        task, fail={"cr": FORBIDDEN.format(r="prometheuses.monitoring.coreos.com", v="get")}
    )
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(task, fail={"pods": FORBIDDEN.format(r="pods", v="list")})
    assert env["status"] == "failed" and codes(env) == [("permission_denied", None)]
    env, _ = run(
        task,
        fail={
            "cr": UNREACHABLE.replace(
                "error trying to reach service", "Unable to connect to the server"
            )
        },
    )
    assert env["status"] == "failed" and codes(env) == [("unreachable", None)]


def test_a_target_of_another_type_is_invalid_input():
    res = {"cluster": "kind", "namespace": NS, "name": "x", "type": "kubernetes/deployment"}
    env, _ = run("server-status", res=res)
    assert env["status"] == "failed" and codes(env) == [("invalid_input", None)]


# ------------------------------------------------------------ scripts: servicemonitor-coverage ----


def test_coverage_selected_and_unselected_service_monitors():
    env, _ = run("servicemonitor-coverage")
    assert env["status"] == "ok" and env["errors"] == []
    check_schemas("servicemonitor-coverage", env)
    cov = env["outputs"]["coverage"]
    rows = {(r["namespace"], r["name"]): r for r in cov["serviceMonitors"]}
    assert cov["complete"] is True and cov["prometheus"]["namespaceScope"] == "selector"
    assert (cov["serviceMonitorCount"], cov["selectedCount"], cov["notSelectedCount"]) == (5, 3, 2)
    assert (
        rows[(NS, "sm-ok")]["activeTargets"] == 1
        and rows[(NS, "sm-ok")]["endpointsWithTargets"] == 1
    )
    assert rows[(NS, "sm-idle")]["selected"] is True and rows[(NS, "sm-idle")]["activeTargets"] == 0
    assert (
        rows[(NS, "sm-other")]["selected"] is False
        and rows[(NS, "sm-other")]["activeTargets"] is None
    )
    assert "labels do not match" in rows[(NS, "sm-other")]["notSelectedReason"]
    assert rows[("rw-prometheus-apps", "sm-apps")]["selected"] is True
    assert cov["selectedWithoutTargets"] == 2


def test_coverage_service_account_access_per_namespace():
    env, _ = run("servicemonitor-coverage")
    access = env["outputs"]["serviceAccountAccess"]
    assert access["serviceAccount"] == f"{NS}/prometheus-rw"
    assert access["namespaces"] == ["rw-prometheus", "rw-prometheus-apps"]
    by_ns = {}
    for check in access["checks"]:
        by_ns.setdefault(check["namespace"], []).append(check["list"] and check["watch"])
    assert by_ns == {"rw-prometheus": [True] * 3, "rw-prometheus-apps": [False] * 3}
    assert access["deniedCount"] == 6


def test_coverage_a_selected_service_monitor_with_a_target_is_not_counted_without_targets():
    doc = load("targets.json")
    sm_ok = next(t for t in doc["data"]["activeTargets"] if t["scrapePool"].endswith("sm-ok/0"))
    other = json.loads(json.dumps(sm_ok))
    other["scrapePool"] = "serviceMonitor/rw-prometheus-apps/sm-apps/0"
    other["scrapeUrl"] = "http://x:9/metrics"
    doc["data"]["activeTargets"].append(other)
    env, _ = run(
        "servicemonitor-coverage", replies={"/api/v1/targets?state=active": json.dumps(doc)}
    )
    cov = env["outputs"]["coverage"]
    assert cov["selectedWithoutTargets"] == 1


def test_coverage_forbidden_cluster_wide_list_falls_back_to_the_own_namespace_and_says_so():
    env, _ = run(
        "servicemonitor-coverage", fail={"sm-all": FORBIDDEN.format(r="servicemonitors", v="list")}
    )
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "servicemonitors")]
    cov = env["outputs"]["coverage"]
    assert cov["complete"] is False
    assert {r["namespace"] for r in cov["serviceMonitors"]} == {NS}
    assert (
        len(cov["notes"]) == 1 and "other than rw-prometheus were not considered" in cov["notes"][0]
    )


def test_coverage_forbidden_namespaces_leave_selection_undetermined_not_false():
    env, _ = run(
        "servicemonitor-coverage", fail={"namespaces": FORBIDDEN.format(r="namespaces", v="list")}
    )
    assert env["status"] == "partial" and codes(env) == [("permission_denied", "namespaces")]
    cov = env["outputs"]["coverage"]
    assert cov["complete"] is False and cov["undeterminedCount"] == 3 and cov["selectedCount"] == 0
    assert all(r["selected"] in (None, False) for r in cov["serviceMonitors"])


def test_coverage_forbidden_subject_access_reviews_is_scoped_and_the_rest_survives():
    env, _ = run(
        "servicemonitor-coverage",
        review=(1, "", FORBIDDEN.format(r="subjectaccessreviews.authorization.k8s.io", v="create")),
    )
    assert env["status"] == "partial" and codes(env) == [
        ("permission_denied", "subjectaccessreviews")
    ]
    assert env["outputs"]["serviceAccountAccess"] is None
    assert env["outputs"]["coverage"]["selectedCount"] == 3
    check_schemas("servicemonitor-coverage", env)


def test_coverage_unset_namespace_selector_lists_only_the_own_namespace():
    # rw-prom-idle has no selectors at all (both nil); give it the pods of rw-prom
    env, calls = run("servicemonitor-coverage", cr="rw-prom-idle", pods=load("pods.json")["items"])
    cov = env["outputs"]["coverage"]
    assert cov["prometheus"]["namespaceScope"] == "own" and cov["selectedCount"] == 0
    assert all(r["selected"] is False for r in cov["serviceMonitors"])
    assert {r["namespace"] for r in cov["serviceMonitors"]} == {NS}
    listing = ("kubectl", "-n", NS, "get", "servicemonitors.monitoring.coreos.com", "-o", "json")
    assert listing in calls and ("kubectl", "get", "namespaces", "-o", "json") not in calls
    assert env["outputs"]["serviceAccountAccess"]["namespaces"] == []


# ------------------------------------------------------------ review fixes ----


def test_targets_that_differ_only_by_query_string_are_three_targets():
    probes = [
        target("blackbox", f"http://exporter:9115/probe?target=site{i}&module=http_2xx", "up")
        for i in range(3)
    ]
    probes[1]["health"] = "down"
    out = targets.view([("pod-0", probes), ("pod-1", probes)], CAPTURED, 200, 3)
    assert (out["total"], out["up"], out["down"]) == (3, 2, 1)
    assert {t["scrapeUrl"] for t in out["unhealthy"]} == {"http://exporter:9115/probe"}
    assert out["pools"][0]["total"] == 3
    assert "site" not in json.dumps(out) and "module" not in json.dumps(out)


def test_coverage_counts_targets_that_differ_only_by_query_string():
    prefix = f"serviceMonitor/{NS}/sm-probe/0"
    probes = [target(prefix, f"http://exporter:9115/probe?target=site{i}", "up") for i in range(3)]
    probes[2]["health"] = "down"
    merged = targets.merge([("pod-0", probes)], CAPTURED, 3)
    prom = prom_with(serviceMonitorSelector={})
    out = coverage.view(prom, [sm_of("sm-probe", {})], None, merged, 200, True)
    row = out["serviceMonitors"][0]
    assert (row["activeTargets"], row["targetsDown"]) == (3, 1)


def test_targets_overdue_and_age_take_the_worst_pod():
    fresh = target("p", "http://a/m", "up", scraped="2026-11-03T22:30:30Z")
    stale = target("p", "http://a/m", "up", scraped="2026-11-03T22:29:00Z")  # 100s old
    for order in ((fresh, stale), (stale, fresh)):
        answers = [("pod-0", [order[0]]), ("pod-1", [order[1]])]
        out = targets.view(answers, CAPTURED, 200, 3)
        entry = out["unhealthy"][0]
        assert entry["overdue"] is True and entry["lastScrapeAgeSeconds"] == 100.0
        assert entry["lastScrape"] == "2026-11-03T22:29:00Z"
        assert out["overdue"] == 1


def test_rules_flags_and_age_take_the_worst_pod():
    ok = {"name": "a", "type": "recording", "health": "ok", "lastError": ""}
    good = group("g", [ok], took=0.1, last="2026-11-03T22:30:30Z")
    slow_stale = group("g", [ok], took=20, last="2026-11-03T22:00:00Z")
    for order in ((good, slow_stale), (slow_stale, good)):
        out = rules.view([("p0", [order[0]]), ("p1", [order[1]])], CAPTURED, 200, 3)
        row = out["groups"][0]
        assert row["evaluationExceedsInterval"] is True and row["evaluationOverdue"] is True
        assert row["evaluationTimeSeconds"] == 20
        assert row["lastEvaluationAgeSeconds"] == 1840.0


def live_pod(name, shard=None):
    labels = {fetch.SHARD_LABEL: shard} if shard is not None else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"phase": "Running"}}


def test_select_pods_takes_one_per_shard_first_then_replicas_up_to_ten():
    pods = [live_pod(f"p-{shard}-{i}", str(shard)) for shard in range(3) for i in range(5)]
    asked, skipped = fetch.select_pods(pods)
    names = [p["metadata"]["name"] for p in asked]
    assert len(asked) == 10 and skipped == 5
    assert {"p-0-0", "p-1-0", "p-2-0"} <= set(names)  # a pod of every shard
    assert names == sorted(names)
    # more shards than the cap: the first ten shards
    many = [live_pod(f"s{i:02d}-0", f"{i:02d}") for i in range(12)]
    asked, skipped = fetch.select_pods(many)
    assert [p["metadata"]["name"] for p in asked] == [f"s{i:02d}-0" for i in range(10)]
    assert skipped == 2
    # pods that are not Running are neither asked nor counted as skipped
    pending = {**live_pod("pending-0"), "status": {"phase": "Pending"}}
    assert fetch.select_pods([pending, live_pod("a-0")]) == ([live_pod("a-0")], 0)
    assert fetch.pod_stats([pending, live_pod("a-0")]) == {"podCount": 2, "podsSkipped": 0}


def test_tasks_report_pod_count_and_pods_skipped_beyond_ten():
    base = load("pods.json")["items"][0]
    pods = []
    for shard in range(2):
        for i in range(6):
            pod = json.loads(json.dumps(base))
            pod["metadata"]["name"] = f"prometheus-s{shard}-{i}"
            pod["metadata"]["labels"][fetch.SHARD_LABEL] = str(shard)
            pods.append(pod)
    for task, output, path in (
        ("targets", "targets", "/api/v1/targets?state=active"),
        ("rules", "rules", "/api/v1/rules"),
        ("servicemonitor-coverage", "coverage", "/api/v1/targets?state=active"),
    ):
        env, calls = run(task, pods=pods)
        assert env["status"] == "ok", env["errors"]
        check_schemas(task, env)
        out = env["outputs"][output]
        assert (out["podCount"], out["podsSkipped"], len(out["queriedPods"])) == (12, 2, 10)
        assert {"prometheus-s0-0", "prometheus-s1-0"} <= set(out["queriedPods"])
        assert len([r for r in LAST["proxy"] if r["path"] == path]) == 10
    env, _ = run("server-status", pods=pods)
    server = env["outputs"]["server"]
    assert (server["podCount"], server["podsSkipped"]) == (12, 2)
    assert sum(1 for p in server["pods"] if p["queried"]) == 10
    check_schemas("server-status", env)


class Clocked:
    """A ctx whose `k8s_proxy_get` calls each take `step` seconds of a fake clock and answer
    through `answer(index, request)` -> a reply dict, a body str or an exception."""

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
            out = self.answer(len(self.calls) - 1, request)
            self.now += self.step
            return out

        return ProxyFake(answer)(namespace, kind, name, port, path, scheme=scheme, timeout=timeout)

    def error(self, code, message, scope=None, **_):
        self.errors.append((code, scope, message))


OK_TARGETS = json.dumps({"status": "success", "data": {"activeTargets": []}})
CLIENT_TIMEOUT = RwError("timeout", "the proxy did not answer in time")


def pods_named(n):
    return [live_pod(f"prometheus-rw-prom-{i}") for i in range(n)]


def test_the_task_deadline_leaves_the_remaining_pods_a_timeout_error():
    ctx = Clocked(lambda i, request: OK_TARGETS)
    budget = fetch.Budget(clock=ctx.clock)
    answers = fetch.query_pods(
        ctx, prom_with(), pods_named(6), targets.PATH, "targets", targets.parse_reply, budget
    )
    # 0s, 30s, 60s, 90s: four asks; the fourth is limited to what is left of the 100s
    assert [a[0] for a in answers] == [f"prometheus-rw-prom-{i}" for i in range(4)]
    assert [c["timeout"] for c in ctx.calls] == [30, 30, 30, 10]
    assert [(c, s) for c, s, _ in ctx.errors] == [
        ("timeout", "pod/prometheus-rw-prom-4"),
        ("timeout", "pod/prometheus-rw-prom-5"),
    ]
    assert "100s deadline" in ctx.errors[0][2]


def test_three_consecutive_timeouts_stop_asking_the_path():
    def answer(i, request):
        return OK_TARGETS if i == 0 else CLIENT_TIMEOUT

    ctx = Clocked(answer, step=1)
    budget = fetch.Budget(clock=ctx.clock)
    answers = fetch.query_pods(
        ctx, prom_with(), pods_named(6), targets.PATH, "targets", targets.parse_reply, budget
    )
    assert [a[0] for a in answers] == ["prometheus-rw-prom-0"]
    assert len(ctx.calls) == 4  # one answer, three timeouts, then no more asks
    assert [(c, s) for c, s, _ in ctx.errors] == [
        ("timeout", "pod/prometheus-rw-prom-1"),
        ("timeout", "pod/prometheus-rw-prom-2"),
        ("timeout", "pod/prometheus-rw-prom-3"),
        ("timeout", "pod/prometheus-rw-prom-4"),
        ("timeout", "pod/prometheus-rw-prom-5"),
    ]
    assert "3 consecutive pods timed out" in ctx.errors[3][2]


def test_an_answer_resets_the_consecutive_timeout_count():
    replies = [CLIENT_TIMEOUT] * 2 + [OK_TARGETS] + [CLIENT_TIMEOUT] * 2
    ctx = Clocked(lambda i, request: replies[i], step=1)
    budget = fetch.Budget(clock=ctx.clock)
    answers = fetch.query_pods(
        ctx, prom_with(), pods_named(5), targets.PATH, "targets", targets.parse_reply, budget
    )
    assert len(ctx.calls) == 5 and len(answers) == 1


def test_the_review_file_is_removed_after_the_reviews_ran_or_failed():
    run("servicemonitor-coverage")
    assert LAST["sar_left"] is False
    run(
        "servicemonitor-coverage",
        review=(1, "", FORBIDDEN.format(r="subjectaccessreviews.authorization.k8s.io", v="create")),
    )
    assert LAST["sar_left"] is False
    env, _ = run("servicemonitor-coverage", review=(1, "", "something odd"))
    assert env["status"] == "failed" and LAST["sar_left"] is False


def test_namespace_selector_any_is_checked_cluster_wide():
    prom = prom_with(serviceMonitorSelector={})
    sm = sm_of("sm-any", {})
    sm["spec"] = {"namespaceSelector": {"any": True}}
    spaces, notes = coverage.target_namespaces(prom, [sm], {(NS, "sm-any"): True})
    assert spaces == [""] and "cluster-wide" in notes[0]
    document, order = coverage.review_list(prom, spaces)
    assert {i["spec"]["resourceAttributes"]["namespace"] for i in document["items"]} == {""}
    assert all(o[0] == "" for o in order)
    view = coverage.access_view(prom, spaces, notes, order, [(True, None)] * len(order))
    assert view["namespaces"] == [""] and view["notes"] == notes
    # a ServiceMonitor that names namespaces is checked in those, not cluster-wide
    sm["spec"] = {"namespaceSelector": {"matchNames": ["a", "b"]}}
    assert coverage.target_namespaces(prom, [sm], {(NS, "sm-any"): True})[0] == ["a", "b"]


def test_coverage_reports_the_review_evaluation_error():
    prom = prom_with()
    _, order = coverage.review_list(prom, ["n1"])
    results = [(True, None)] * len(order)
    results[0] = (False, "x" * 2000)
    view = coverage.access_view(prom, ["n1"], [], order, results)
    first = view["checks"][0]
    assert first["evaluationError"] == "x" * 1024 and first["evaluationErrorTruncated"] is True
    assert view["checks"][1]["evaluationError"] is None
    assert view["deniedCount"] == 1


def test_service_discovery_role_defaults_to_endpoints_and_is_honoured_when_set():
    assert coverage.discovery_resources(prom_with())[0] == "Endpoints"
    assert ("", "endpoints") in coverage.discovery_resources(prom_with())[1]
    role, resources = coverage.discovery_resources(prom_with(serviceDiscoveryRole="EndpointSlice"))
    assert role == "EndpointSlice" and ("discovery.k8s.io", "endpointslices") in resources
    assert ("", "endpoints") not in resources


# ------------------------------------------------------------ permissions ----

RESOURCES = {
    "prometheuses.monitoring.coreos.com": ("monitoring.coreos.com", "prometheuses"),
    "servicemonitors.monitoring.coreos.com": ("monitoring.coreos.com", "servicemonitors"),
    "pods": ("", "pods"),
    "namespaces": ("", "namespaces"),
}
VALUE_FLAGS = {"-n", "-o", "-l", "-f", "--request-timeout"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs. Raises on anything it
    does not know, so a script that issues a new call has to extend this."""
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif a.startswith("--request-timeout="):
            continue
        elif not a.startswith("-") or a == "-A":
            positional.append(a)
    assert "--raw" not in argv, argv  # pod proxy reads go through ctx.k8s_proxy_get (S17)
    if positional[0] == "create":
        assert flags["-f"].startswith("sar-"), argv
        return {("authorization.k8s.io", "subjectaccessreviews", "create")}
    if positional[0] != "get":
        raise AssertionError(f"no RBAC mapping for kubectl {positional[0]}: {argv}")
    group, plural = RESOURCES[positional[1]]
    named_object = len(positional) > 2 and "-A" not in positional and "-l" not in flags
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
    cases = [
        {},
        {"fail": {"sm-all": FORBIDDEN.format(r="servicemonitors", v="list")}},
        {"fail": {"namespaces": FORBIDDEN.format(r="namespaces", v="list")}},
    ]
    for kw in cases:
        for cr in ("rw-prom", "rw-prom-idle"):
            try:
                _, calls = run(task, cr=cr, **kw)
            except AssertionError:
                continue
            for argv in calls:
                needed |= rbac_needs(list(argv))
            needed |= {("", f"{r['kind']}/proxy", "get") for r in LAST["proxy"]}
    # the own-namespace servicemonitors list is made only for a CR with no namespace selector
    _, calls = run(task, cr="rw-prom-idle", pods=load("pods.json")["items"])
    for argv in calls:
        needed |= rbac_needs(list(argv))
    needed |= {("", f"{r['kind']}/proxy", "get") for r in LAST["proxy"]}
    assert needed == granted(task), (
        f"{task}: calls {sorted(needed)}, declares {sorted(granted(task))}"
    )


def test_rbac_needs_maps_the_verbs():
    base = ["kubectl", "-n", "x", "get"]
    assert rbac_needs([*base, "prometheuses.monitoring.coreos.com", "p", "-o", "json"]) == {
        ("monitoring.coreos.com", "prometheuses", "get")
    }
    assert rbac_needs([*base, "pods", "-l", "a=b", "-o", "json"]) == {("", "pods", "list")}
    assert rbac_needs(
        ["kubectl", "get", "servicemonitors.monitoring.coreos.com", "-A", "-o", "json"]
    ) == {("monitoring.coreos.com", "servicemonitors", "list")}
    with pytest.raises(AssertionError):  # S17: a proxied read through kubectl get --raw
        rbac_needs(["kubectl", "get", "--raw", "/api/v1/namespaces/n/pods/p:9090/proxy/-/ready"])
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "delete", "pods", "p"])


def test_all_tasks_only_read():
    assert all(t["readOnly"] is True for t in TASKS.values())
    for task in TASKS:
        assert all(v in ("get", "list", "create") for (_, _, v) in granted(task))
        creates = {(g, r) for (g, r, v) in granted(task) if v == "create"}
        assert creates <= {("authorization.k8s.io", "subjectaccessreviews")}
