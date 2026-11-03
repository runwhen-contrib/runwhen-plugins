"""Unit tests for k8s-ingress-health's lib/ and scripts against JSON captured from
the kind-rw-plugins scenarios (plugins/kubernetes/tests/kind). The cluster
state was captured at one moment; `NOW` is a fixed point just after it.

kind runs no ingress controller, so the Ingresses there have no address and no
Warning events, and carry no GKE annotation. Those inputs are synthetic in
these tests: small edits of a captured Ingress or Service, or hand-written
events with the shape the API returns."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from k8s_ingress_health import ingress as ing
from k8s_ingress_health import k8s
from k8s_ingress_health.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-ingress-health"
DATA = Path(__file__).parent / "data" / "k8s_ingress_health"
NOW = datetime(2026, 11, 3, 18, 40, tzinfo=timezone.utc)
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def ingress(ns, name):
    return named(load(f"{ns}_ingresses"), name)


def service(ns, name):
    return named(load(f"{ns}_services"), name)


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert ing.parse_duration("90s").total_seconds() == 90
    assert ing.parse_duration("1h30m").total_seconds() == 5400
    assert ing.parse_duration("500ms").total_seconds() == 0.5
    assert ing.parse_duration("1m500ms").total_seconds() == 60.5
    for bad in ("", "soon", "10", "5x"):
        with pytest.raises(RwError) as exc:
            ing.parse_duration(bad)
        assert exc.value.code == "invalid_input"


def test_trunc_flags_text_cut_at_1024():
    assert ing.trunc("short") == ("short", False)
    cut, truncated = ing.trunc("x" * 3000)
    assert len(cut) == 1024 and truncated
    assert ing.trunc(None) == (None, False)


# ----------------------------------------------------------- backends ----


def test_backend_refs_of_the_healthy_ingress():
    refs = ing.backend_refs(ingress("healthy", "web"))
    assert len(refs) == 1
    assert refs[0]["source"] == "rule" and refs[0]["host"] == "web.rw-healthy.local"
    assert refs[0]["service"] == {"name": "web", "portNumber": 80, "portName": None}
    assert ing.service_names(refs) == ["web"]


def test_default_backend_resource_backend_and_repeated_service():
    doc = {
        "spec": {
            "defaultBackend": {"service": {"name": "fallback", "port": {"name": "http"}}},
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "path": "/a",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "a", "port": {"number": 1}}},
                            },
                            {
                                "path": "/b",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "a", "port": {"number": 2}}},
                            },
                            {
                                "path": "/img",
                                "pathType": "ImplementationSpecific",
                                "backend": {
                                    "resource": {
                                        "apiGroup": "k8s.example.com",
                                        "kind": "StorageBucket",
                                        "name": "static",
                                    }
                                },
                            },
                        ]
                    }
                }
            ],
        }
    }
    refs = ing.backend_refs(doc)
    assert [r["source"] for r in refs] == ["default", "rule", "rule", "rule"]
    assert refs[0]["host"] is None and refs[0]["service"]["portName"] == "http"
    assert refs[3]["kind"] == "resource" and refs[3]["service"] is None
    assert refs[3]["resource"] == {
        "apiGroup": "k8s.example.com",
        "kind": "StorageBucket",
        "name": "static",
    }
    assert ing.service_names(refs) == ["fallback", "a"]
    rows = ing.backend_entries(doc, {"a": None}, {})
    # a resource backend is reported as such: nothing about a Service is claimed for it
    assert rows[3]["serviceExists"] is None and rows[3]["endpoints"] is None
    assert rows[1]["serviceExists"] is False
    # "fallback" was not looked up: unknown, not missing
    assert rows[0]["serviceExists"] is None


def test_endpoint_counts_ready_unset_counts_as_ready_and_dual_stack_counts_once():
    def ep(addr, ready, pod):
        conditions = {} if ready is None else {"ready": ready}
        return {
            "addresses": [addr],
            "conditions": conditions,
            "targetRef": {"kind": "Pod", "namespace": "n", "name": pod},
        }

    v4 = {
        "endpoints": [
            ep("10.0.0.1", True, "a"),
            ep("10.0.0.2", False, "b"),
            ep("10.0.0.3", None, "c"),
        ]
    }
    v6 = {"endpoints": [ep("fd00::1", True, "a")]}
    assert ing.endpoint_counts([v4, v6]) == {"ready": 2, "notReady": 1, "slices": 2}
    assert ing.endpoint_counts([]) == {"ready": 0, "notReady": 0, "slices": 0}
    assert ing.endpoint_counts([{"endpoints": None}]) == {"ready": 0, "notReady": 0, "slices": 1}
    # no targetRef (an Endpoints written by hand): identified by address
    bare = {"endpoints": [{"addresses": ["1.1.1.1"]}, {"addresses": ["1.1.1.1"]}]}
    assert ing.endpoint_counts([bare])["ready"] == 1


def test_port_matches_by_number_or_by_name():
    svc = service("broken", "crashloop")
    assert ing.match_port(svc, 80, None)["name"] == "http"
    assert ing.match_port(svc, None, "http")["port"] == 80
    assert ing.match_port(svc, 8080, None) is None
    assert ing.match_port(svc, None, "grpc") is None


def test_backend_entries_are_capped_at_max_items():
    paths = [
        {
            "path": f"/{i}",
            "pathType": "Prefix",
            "backend": {"service": {"name": "s", "port": {"number": 80}}},
        }
        for i in range(5)
    ]
    doc = {"metadata": {"name": "i"}, "spec": {"rules": [{"http": {"paths": paths}}]}}
    assert len(ing.cap(ing.backend_entries(doc, {}, {}), 2)) == 2


# ------------------------------------------------------------ GKE annotation ----

GKE = json.dumps(
    {
        "k8s-be-30080--0123456789abcdef": "HEALTHY",
        "k8s-be-31234--0123456789abcdef": "UNHEALTHY",
        "k8s-be-32000--0123456789abcdef": "UNKNOWN",
        "k8s-be-30443--0123456789abcdef": "HEALTHY",
    }
)


def with_annotation(value):
    doc = copy.deepcopy(ingress("healthy", "web"))
    doc["metadata"].setdefault("annotations", {})[ing.GKE_BACKENDS_ANNOTATION] = value
    return doc


def test_gke_backends_annotation_reports_unhealthy_and_unknown_first():
    view, problem = ing.gke_backends(with_annotation(GKE), 200)
    assert problem is None and view["present"] is True and view["notHealthy"] == 2
    health = [(b["name"][7:12], b["health"], b["healthy"]) for b in view["backends"]]
    assert health[:2] == [("31234", "UNHEALTHY", False), ("32000", "UNKNOWN", False)]
    assert all(h[2] for h in health[2:])
    assert view["annotation"] == "ingress.kubernetes.io/backends"


def test_gke_backends_cut_keeps_the_unhealthy_ones():
    view, _ = ing.gke_backends(with_annotation(GKE), 1)
    assert len(view["backends"]) == 1 and view["backends"][0]["healthy"] is False
    assert view["notHealthy"] == 2  # counted before the cut


def test_absent_gke_annotation_is_present_false_not_healthy():
    # the classic task read an absent annotation as "nothing unhealthy"
    view, problem = ing.gke_backends(ingress("healthy", "web"), 200)
    assert problem is None
    assert view["present"] is False and view["backends"] == [] and view["notHealthy"] == 0


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"HEALTHY"', ""])
def test_malformed_gke_annotation_is_unknown_with_a_problem(raw):
    view, problem = ing.gke_backends(with_annotation(raw), 200)
    assert view["present"] is True and view["backends"] is None and view["notHealthy"] is None
    assert problem and ing.GKE_BACKENDS_ANNOTATION in problem


# --------------------------------------------------------------- address ----


def lb(*addrs):
    return {
        "loadBalancer": {
            "ingress": [{"ip": a} if a[0].isdigit() else {"hostname": a} for a in addrs]
        }
    }


def lb_service(status):
    doc = copy.deepcopy(service("healthy", "web"))
    doc["spec"]["type"] = "LoadBalancer"
    doc["status"] = status
    return doc


def addressed(*addrs):
    doc = copy.deepcopy(ingress("healthy", "web"))
    doc["status"] = lb(*addrs)
    return doc


def test_ingress_without_a_controller_has_no_address():
    view = ing.ingress_view(ingress("healthy", "web"))
    assert view["addresses"] == [] and view["assigned"] is False
    assert view["ingressClassName"] is None and view["name"] == "web"


def test_ingress_address_ip_or_hostname():
    view = ing.ingress_view(addressed("34.1.1.1", "lb.example.com"))
    assert view["assigned"] is True
    assert view["addresses"] == [
        {"ip": "34.1.1.1", "hostname": None},
        {"ip": None, "hostname": "lb.example.com"},
    ]


@pytest.mark.parametrize(
    "ingress_addr,svc_status,expected",
    [
        ("10.0.0.1", lb("10.9.9.9"), True),  # the classic conflict
        ("10.0.0.1", lb("10.0.0.1"), False),
        ("lb.example.com", lb("other.example.com"), True),
        ("10.0.0.1", {"loadBalancer": {}}, None),  # Service address still pending
    ],
)
def test_service_load_balancer_address_against_the_ingress(ingress_addr, svc_status, expected):
    rows = ing.service_addresses(addressed(ingress_addr), {"web": lb_service(svc_status)}, 200)
    assert rows[0]["service"] == "web" and rows[0]["type"] == "LoadBalancer"
    assert rows[0]["differsFromIngress"] is expected


def test_service_address_unknowns():
    cluster_ip = {"web": service("healthy", "web")}
    row = ing.service_addresses(addressed("10.0.0.1"), cluster_ip, 200)[0]
    assert row["type"] == "ClusterIP" and row["differsFromIngress"] is None
    # the Ingress has no address yet
    row = ing.service_addresses(
        ingress("healthy", "web"), {"web": lb_service(lb("10.9.9.9"))}, 200
    )[0]
    assert row["differsFromIngress"] is None and row["addresses"][0]["ip"] == "10.9.9.9"
    # missing and not-looked-up Services
    assert ing.service_addresses(addressed("1.1.1.1"), {"web": None}, 200)[0]["exists"] is False
    assert ing.service_addresses(addressed("1.1.1.1"), {}, 200)[0]["exists"] is None


def test_class_view():
    assert ing.class_view(None, None, True)["exists"] is None
    assert ing.class_view("nginx", None, True) == {
        "name": "nginx",
        "exists": False,
        "controller": None,
        "isDefault": None,
    }
    assert ing.class_view("nginx", None, False)["exists"] is None
    ic = {
        "metadata": {"annotations": {ing.DEFAULT_CLASS_ANNOTATION: "true"}},
        "spec": {"controller": "k8s.io/ingress-nginx"},
    }
    assert ing.class_view("nginx", ic, True) == {
        "name": "nginx",
        "exists": True,
        "controller": "k8s.io/ingress-nginx",
        "isDefault": True,
    }


# ---------------------------------------------------------------- events ----


def event(kind, name, uid, reason, message, minutes_ago, typ="Warning", count=1):
    last = datetime(2026, 11, 3, 18, 40 - minutes_ago, tzinfo=timezone.utc)
    return {
        "metadata": {"name": f"{name}.{reason}.{minutes_ago}"},
        "type": typ,
        "reason": reason,
        "message": message,
        "count": count,
        "firstTimestamp": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastTimestamp": last.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"component": "ingress-gce"},
        "involvedObject": {"kind": kind, "name": name, "uid": uid},
    }


ING_UID = "01e72069-124f-4ec9-9571-25eb7d9e0f3d"
SUBJECTS = [("Ingress", "web", ING_UID), ("Service", "web", "svc-uid")]


def test_warning_events_on_the_ingress_and_its_services():
    events = [
        event("Ingress", "web", ING_UID, "Translate", "invalid", 5),
        event("Ingress", "web", ING_UID, "Translate", "invalid again", 2, count=3),
        event("Service", "web", "svc-uid", "SyncLoadBalancerFailed", "quota", 10),
        event("Ingress", "web", "uid-of-a-deleted-ingress", "Translate", "old object", 1),
        event("Ingress", "web", ING_UID, "Sync", "scheduled", 1, typ="Normal"),
        event("Ingress", "other", "x", "Translate", "another ingress", 1),
        event("Service", "unrelated", "y", "Failed", "another service", 1),
        event("Ingress", "web", ING_UID, "TooOld", "stale", 30),
    ]
    rows = ing.warning_events(events, SUBJECTS, "20m", NOW, 200)
    assert [(r["kind"], r["name"], r["reason"], r["count"]) for r in rows] == [
        ("Ingress", "web", "Translate", 4),
        ("Service", "web", "SyncLoadBalancerFailed", 1),
    ]
    assert rows[0]["message"] == "invalid again" and rows[0]["lastSeen"] == "2026-11-03T18:38:00Z"
    assert rows[0]["firstSeen"] == "2026-11-03T18:35:00Z"
    assert rows[0]["reportingComponent"] == "ingress-gce"


def test_warning_events_window_cap_and_unknown_uid():
    events = [event("Ingress", "web", ING_UID, f"R{i}", "m", i + 1) for i in range(5)]
    assert len(ing.warning_events(events, SUBJECTS, "1h", NOW, 2)) == 2
    assert ing.warning_events(events, SUBJECTS, "2m", NOW, 200)[0]["reason"] == "R0"
    assert len(ing.warning_events(events, SUBJECTS, "2m", NOW, 200)) == 2
    # a Service that no longer exists (uid None) is matched by name
    gone = [event("Service", "web", "old-uid", "Failed", "m", 1)]
    assert len(ing.warning_events(gone, [("Service", "web", None)], "1h", NOW, 200)) == 1
    with pytest.raises(RwError):
        ing.warning_events(events, SUBJECTS, "soon", NOW, 200)


def test_warning_event_message_is_cut_and_flagged():
    long = [event("Ingress", "web", ING_UID, "Big", "m" * 5000, 1)]
    row = ing.warning_events(long, SUBJECTS, "1h", NOW, 200)[0]
    assert len(row["message"]) == 1024 and row["truncated"] is True


# --------------------------------------------------------- error handling ----


@pytest.mark.parametrize(
    "rc,text,code",
    [
        (
            1,
            'Error from server (Forbidden): services "web" is forbidden: User "x" cannot get resource',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): ingresses.networking.k8s.io "x" not found', "not_found"),
        (
            1,
            "Unable to connect to the server: dial tcp 10.0.0.1:443: connect: connection refused",
            "unreachable",
        ),
        (1, "Unable to connect to the server: dial tcp: i/o timeout", "unreachable"),
        (
            1,
            "Unable to connect to the server: x509: certificate signed by unknown authority",
            "unreachable",
        ),
        (1, "dial tcp: lookup api.example: no such host", "unreachable"),
        (1, "context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "endpointslices"', "dependency"),
        (1, "boom", "internal"),
    ],
)
def test_classify(rc, text, code):
    assert k8s.classify(rc, text) == code


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class RunCtx:
    """Like the installed SDK's ctx.run: returns the CompletedProcess, never raises on a non-zero exit."""

    def __init__(self, done):
        self.done = done

    def run(self, argv):
        return self.done


def test_kubectl_json_raises_rw_error_for_a_non_zero_exit_that_ctx_run_did_not_raise():
    forbidden = (
        'Error from server (Forbidden): ingresses is forbidden: cannot get resource "ingresses"'
    )
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(RunCtx(Done(1, "", forbidden)), "get", "ingresses")
    assert exc.value.code == "permission_denied"


def test_kubectl_json_rejects_output_that_is_not_json():
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(RunCtx(Done(0, "not json")), "get", "ingresses")
    assert exc.value.code == "internal"


def test_run_checked_message_is_bounded():
    with pytest.raises(RwError) as exc:
        k8s.run_checked(RunCtx(Done(1, "", "x" * 5000)), ["kubectl", "get"])
    assert len(exc.value.message) < 1100


# ----------------------------------------------------------- scripts ----


class FakeCtx:
    """Replays captured JSON for kubectl calls.

    `fail` maps a resource word (ingresses.networking.k8s.io, service,
    endpointslices.discovery.k8s.io, ingressclasses.networking.k8s.io, events)
    to the stderr kubectl would print. `docs` overrides what a (resource, name)
    read returns; `events` is the list `get events` returns.
    """

    def __init__(
        self, ns, name, fail=None, docs=None, events=None, rtype="kubernetes/ingress", path=None
    ):
        self.resource = SimpleNamespace(namespace=ns, name=name, type=rtype, path=path)
        self.settings = SimpleNamespace(max_items=200)
        self.fail = fail or {}
        self.docs = docs or {}
        self.events = events or []
        self.errors = []
        self.calls = []

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message})

    def run(self, argv):
        self.calls.append(argv)
        words, it = [], iter(argv[1:])
        for a in it:
            if a in ("-n", "-o", "-l", "--field-selector"):
                value = next(it)
                if a == "-l":
                    selector = value
            elif not a.startswith("-"):
                words.append(a)
        resource = words[1]
        for word, stderr in self.fail.items():
            if word == resource:
                return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        tag = self.resource.namespace.split("-")[1]
        if resource == "ingresses.networking.k8s.io":
            doc = self.docs.get(("ingress", words[2])) or ingress(tag, words[2])
        elif resource == "service":
            if ("service", words[2]) in self.docs:
                doc = self.docs[("service", words[2])]
            else:
                try:
                    doc = service(tag, words[2])
                except StopIteration:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr=f'Error from server (NotFound): services "{words[2]}" not found',
                    )
        elif resource == "endpointslices.discovery.k8s.io":
            svc = selector.split("=", 1)[1]
            slices = load(f"{tag}_endpointslices")
            doc = {
                "items": [
                    s
                    for s in slices["items"]
                    if s["metadata"]["labels"].get("kubernetes.io/service-name") == svc
                ]
            }
        elif resource == "ingressclasses.networking.k8s.io":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f'Error from server (NotFound): ingressclasses.networking.k8s.io "{words[2]}" not found',
            )
        elif resource == "events":
            doc = {"items": self.events}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(doc), stderr="")


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"ingress_{name}", SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


TASK_SCRIPT = {"backends": "backends", "address": "address", "warning-events": "warning_events"}


def run_task(task, ctx, **inputs):
    """Run a task's script and validate every output against its declared schema."""
    out = script(TASK_SCRIPT[task])(ctx, **inputs)
    assert set(out) == set(TASKS[task]["outputs"])
    for key, value in out.items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        jsonschema.validate(value, schema, cls=jsonschema.Draft202012Validator)
    return out


FORBIDDEN = 'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot get resource'
DOWN = "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused"


def test_healthy_ingress_backend_is_clean():
    ctx = FakeCtx("rw-healthy", "web")
    out = run_task("backends", ctx)
    [b] = out["backends"]
    assert b["service"]["name"] == "web" and b["serviceExists"] is True and b["portExists"] is True
    assert b["serviceType"] == "ClusterIP" and b["targetPort"] == 80
    assert b["endpoints"] == {"ready": 2, "notReady": 0, "slices": 1}
    assert out["tls"] == [] and out["gkeBackends"]["present"] is False
    assert ctx.errors == []


def test_ingress_to_a_missing_service_is_reported_as_missing():
    ctx = FakeCtx("rw-broken", "orphan")
    [b] = run_task("backends", ctx)["backends"]
    assert b["service"]["name"] == "missing" and b["serviceExists"] is False
    assert b["portExists"] is None and b["endpoints"] is None
    assert ctx.errors == []


def test_wrong_port_is_reported_even_though_the_service_exists():
    [b] = run_task("backends", FakeCtx("rw-broken", "wrong-port"))["backends"]
    assert b["serviceExists"] is True and b["portExists"] is False
    assert b["service"]["portNumber"] == 8080 and b["targetPort"] is None


def test_service_without_ready_endpoints_and_a_port_by_name():
    ctx = FakeCtx("rw-broken", "no-endpoints")
    out = run_task("backends", ctx)
    [b] = out["backends"]
    assert b["service"]["portName"] == "http" and b["portExists"] is True
    # the pod exists (an endpoint) but is not ready: not-ready is counted, not dropped
    assert b["endpoints"] == {"ready": 0, "notReady": 1, "slices": 1}
    assert out["tls"] == [
        {"hosts": ["no-endpoints.rw-broken.local"], "secretName": "no-such-secret"}
    ]
    assert all("secrets" not in c and "secret" not in c for c in ctx.calls)


def test_backends_script_reports_gke_annotation_and_a_malformed_one_is_an_error_not_healthy():
    doc = with_annotation(GKE)
    out = run_task("backends", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc}))
    assert out["gkeBackends"]["notHealthy"] == 2
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): with_annotation("{oops")})
    out = run_task("backends", ctx)
    assert out["gkeBackends"]["backends"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("internal", "annotations")]
    assert out["backends"][0]["serviceExists"] is True  # the rest is still returned


def test_forbidden_services_leave_service_facts_null_with_an_error_and_keep_the_rest():
    ctx = FakeCtx("rw-healthy", "web", fail={"service": FORBIDDEN % "services"})
    out = run_task("backends", ctx)
    [b] = out["backends"]
    assert b["service"]["name"] == "web"
    assert b["serviceExists"] is None and b["portExists"] is None and b["endpoints"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "services")]


def test_forbidden_endpointslices_leave_endpoints_null_not_zero():
    ctx = FakeCtx(
        "rw-healthy", "web", fail={"endpointslices.discovery.k8s.io": FORBIDDEN % "endpointslices"}
    )
    [b] = run_task("backends", ctx)["backends"]
    assert b["serviceExists"] is True and b["portExists"] is True
    assert b["endpoints"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "endpointslices")
    ]


def test_forbidden_service_read_is_reported_once_not_per_service():
    doc = copy.deepcopy(ingress("healthy", "web"))
    doc["spec"]["rules"][0]["http"]["paths"] += [
        {
            "path": f"/{n}",
            "pathType": "Prefix",
            "backend": {"service": {"name": n, "port": {"number": 80}}},
        }
        for n in ("a", "b", "c")
    ]
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={("ingress", "web"): doc},
        fail={"service": FORBIDDEN % "services"},
    )
    out = run_task("backends", ctx)
    assert len(out["backends"]) == 4 and len(ctx.errors) == 1


def test_address_of_a_healthy_ingress_on_a_cluster_without_a_controller():
    ctx = FakeCtx("rw-healthy", "web")
    out = run_task("address", ctx)
    assert out["ingress"]["assigned"] is False and out["ingress"]["addresses"] == []
    assert out["ingressClass"]["exists"] is None
    assert out["serviceAddresses"][0]["type"] == "ClusterIP"
    assert ctx.errors == []


def test_address_script_with_a_missing_ingress_class_and_a_load_balancer_conflict():
    doc = addressed("34.1.1.1")
    doc["spec"]["ingressClassName"] = "nginx"
    svc = lb_service(lb("35.2.2.2"))
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc, ("service", "web"): svc})
    out = run_task("address", ctx)
    assert out["ingress"]["ingressClassName"] == "nginx" and out["ingress"]["assigned"] is True
    assert out["ingressClass"]["exists"] is False
    assert out["serviceAddresses"][0]["differsFromIngress"] is True
    assert ctx.errors == []


def test_forbidden_ingress_class_keeps_the_address_and_services():
    doc = addressed("34.1.1.1")
    doc["spec"]["ingressClassName"] = "nginx"
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={("ingress", "web"): doc},
        fail={"ingressclasses.networking.k8s.io": FORBIDDEN % "ingressclasses"},
    )
    out = run_task("address", ctx)
    assert out["ingressClass"] == {
        "name": "nginx",
        "exists": None,
        "controller": None,
        "isDefault": None,
    }
    assert out["ingress"]["assigned"] is True and out["serviceAddresses"][0]["exists"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "ingressclasses")
    ]


def test_warning_events_script_matches_by_uid_and_reports_what_it_inspected():
    svc_uid = service("broken", "crashloop")["metadata"]["uid"]
    ing_uid = ingress("broken", "wrong-port")["metadata"]["uid"]
    events = [
        event("Ingress", "wrong-port", ing_uid, "Translate", "port 8080 not found", 3),
        event("Service", "crashloop", svc_uid, "SyncFailed", "boom", 4),
        event("Service", "crashloop", "uid-of-an-older-service", "SyncFailed", "ghost", 2),
    ]
    out = run_task("warning-events", FakeCtx("rw-broken", "wrong-port", events=events), since="20m")
    assert [(e["kind"], e["reason"]) for e in out["events"]] == [
        ("Ingress", "Translate"),
        ("Service", "SyncFailed"),
    ]
    assert out["events"][1]["message"] == "boom"
    assert out["inspected"] == {
        "ingress": "wrong-port",
        "services": ["crashloop"],
        "servicesLookedUp": True,
    }


def test_warning_events_with_forbidden_services_match_by_name_and_say_so():
    events = [event("Service", "web", "any-uid", "SyncFailed", "boom", 3)]
    ctx = FakeCtx("rw-healthy", "web", events=events, fail={"service": FORBIDDEN % "services"})
    out = run_task("warning-events", ctx, since="20m")
    assert len(out["events"]) == 1 and out["inspected"]["servicesLookedUp"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "services")]


def test_no_warning_events_is_an_empty_list_when_events_could_be_read():
    out = run_task("warning-events", FakeCtx("rw-healthy", "web"))
    assert out["events"] == [] and out["inspected"]["servicesLookedUp"] is True


def test_forbidden_events_raise_instead_of_returning_an_empty_list():
    ctx = FakeCtx("rw-healthy", "web", fail={"events": FORBIDDEN % "events"})
    with pytest.raises(RwError) as exc:
        script("warning_events")(ctx)
    assert exc.value.code == "permission_denied"


def test_bad_since_is_refused_before_any_kubectl_call():
    ctx = FakeCtx("rw-healthy", "web")
    with pytest.raises(RwError) as exc:
        script("warning_events")(ctx, since="soon")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_forbidden_ingress_raises_in_every_task():
    for task, name in TASK_SCRIPT.items():
        ctx = FakeCtx(
            "rw-broken", "orphan", fail={"ingresses.networking.k8s.io": FORBIDDEN % "ingresses"}
        )
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "permission_denied", task


def test_unreachable_api_server_raises_in_every_task():
    for name in TASK_SCRIPT.values():
        ctx = FakeCtx("rw-healthy", "web", fail={"ingresses.networking.k8s.io": DOWN})
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "unreachable"


def test_unreachable_service_read_raises_instead_of_degrading():
    ctx = FakeCtx("rw-healthy", "web", fail={"service": DOWN})
    with pytest.raises(RwError) as exc:
        script("backends")(ctx)
    assert exc.value.code == "unreachable" and ctx.errors == []


def test_missing_ingress_raises_not_found_in_every_task():
    gone = 'Error from server (NotFound): ingresses.networking.k8s.io "gone" not found'
    for name in TASK_SCRIPT.values():
        ctx = FakeCtx("rw-healthy", "gone", fail={"ingresses.networking.k8s.io": gone})
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "not_found"


def test_wrong_target_type_is_refused_before_any_kubectl_call():
    for name in TASK_SCRIPT.values():
        ctx = FakeCtx("rw-healthy", "web", rtype="kubernetes/service")
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_target_type_falls_back_to_the_plural_segment_of_the_path():
    ctx = FakeCtx(
        "rw-healthy", "web", rtype=None, path="clusters/c/namespaces/rw-healthy/ingresses/web"
    )
    assert run_task("backends", ctx)["backends"][0]["serviceExists"] is True
