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
    for bad in ("", "soon", "10", "5x", "1w"):
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
    assert ing.endpoint_counts([v4, v6]) == {
        "ready": 2,
        "notReady": 1,
        "serving": 3,
        "terminating": 0,
        "slices": 2,
    }
    zero = {"ready": 0, "notReady": 0, "serving": 0, "terminating": 0}
    assert ing.endpoint_counts([]) == {**zero, "slices": 0}
    assert ing.endpoint_counts([{"endpoints": None}]) == {**zero, "slices": 1}
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
    out = ing.wrap(ing.backend_entries(doc, {}, {}), 2)
    assert len(out["items"]) == 2 and out["total"] == 5 and out["truncated"] is True


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
    rows = ing.service_addresses(addressed(ingress_addr), {"web": lb_service(svc_status)}, 200)[
        "items"
    ]
    assert rows[0]["service"] == "web" and rows[0]["type"] == "LoadBalancer"
    assert rows[0]["differsFromIngress"] is expected


def test_service_address_unknowns():
    cluster_ip = {"web": service("healthy", "web")}
    row = ing.service_addresses(addressed("10.0.0.1"), cluster_ip, 200)["items"][0]
    assert row["type"] == "ClusterIP" and row["differsFromIngress"] is None
    # the Ingress has no address yet
    row = ing.service_addresses(
        ingress("healthy", "web"), {"web": lb_service(lb("10.9.9.9"))}, 200
    )["items"][0]
    assert row["differsFromIngress"] is None and row["addresses"][0]["ip"] == "10.9.9.9"
    # missing and not-looked-up Services
    assert (
        ing.service_addresses(addressed("1.1.1.1"), {"web": None}, 200)["items"][0]["exists"]
        is False
    )
    assert ing.service_addresses(addressed("1.1.1.1"), {}, 200)["items"][0]["exists"] is None


def ic(name, default=False, controller="k8s.io/ingress-nginx"):
    annotations = {ing.DEFAULT_CLASS_ANNOTATION: "true"} if default else {}
    return {
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"controller": controller},
    }


def test_class_view_of_a_named_class():
    view = ing.class_view("spec", "nginx", ic("nginx", default=True), "ok", None)
    assert view["classSource"] == "spec" and view["lookedUp"] is True
    assert view["exists"] is True and view["controller"] == "k8s.io/ingress-nginx"
    assert view["isDefault"] is True and view["effectiveClass"] == "nginx"
    assert view["defaultClasses"] is None  # not listed: unknown, not empty
    missing = ing.class_view("spec", "nginx", None, "not_found", None)
    assert missing["exists"] is False and missing["lookedUp"] is True
    denied = ing.class_view("spec", "nginx", None, "denied", None)
    assert denied["exists"] is None and denied["lookedUp"] is False
    assert denied["classSource"] == "spec"  # forbidden, not "no class named"


def test_class_view_of_a_legacy_annotation_looks_nothing_up():
    view = ing.class_view("annotation", "gce", None, "ok", None)
    assert view["classSource"] == "annotation" and view["lookedUp"] is False
    assert view["exists"] is None and view["effectiveClass"] == "gce"
    assert view["defaultClasses"] is None


def test_class_source_prefers_spec_then_annotation_then_none():
    doc = copy.deepcopy(ingress("healthy", "web"))
    assert ing.class_source(doc) == ("none", None)
    doc["metadata"].setdefault("annotations", {})[ing.CLASS_ANNOTATION] = "gce"
    assert ing.class_source(doc) == ("annotation", "gce")
    doc["spec"]["ingressClassName"] = "nginx"
    assert ing.class_source(doc) == ("spec", "nginx")


def test_no_class_named_and_no_default_class():
    view = ing.class_view("none", None, None, "ok", [ic("nginx")])
    assert view["classSource"] == "none" and view["lookedUp"] is False
    assert view["defaultClasses"] == [] and view["effectiveClass"] is None
    assert view["multipleDefaults"] is False
    assert ing.class_view("none", None, None, "ok", [])["defaultClasses"] == []


def test_no_class_named_and_one_default_class():
    view = ing.class_view("none", None, None, "ok", [ic("nginx"), ic("gce", default=True)])
    assert view["defaultClasses"] == ["gce"] and view["effectiveClass"] == "gce"
    assert view["multipleDefaults"] is False


def test_no_class_named_and_two_default_classes_has_no_effective_class():
    view = ing.class_view("none", None, None, "ok", [ic("b", default=True), ic("a", default=True)])
    assert view["defaultClasses"] == ["a", "b"] and view["multipleDefaults"] is True
    assert view["effectiveClass"] is None


def test_no_class_named_and_list_forbidden_is_unknown_not_no_default():
    view = ing.class_view("none", None, None, "ok", None)
    assert view["defaultClasses"] is None and view["multipleDefaults"] is None
    assert view["effectiveClass"] is None and view["classSource"] == "none"


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
    rows = ing.warning_events(events, SUBJECTS, "20m", NOW, 200)["items"]
    assert [(r["kind"], r["name"], r["reason"], r["count"]) for r in rows] == [
        ("Ingress", "web", "Translate", 4),
        ("Service", "web", "SyncLoadBalancerFailed", 1),
    ]
    assert rows[0]["message"] == "invalid again" and rows[0]["lastSeen"] == "2026-11-03T18:38:00Z"
    assert rows[0]["firstSeen"] == "2026-11-03T18:35:00Z"
    assert rows[0]["reportingComponent"] == "ingress-gce"


def test_warning_events_window_cap_and_unknown_uid():
    events = [event("Ingress", "web", ING_UID, f"R{i}", "m", i + 1) for i in range(5)]
    capped = ing.warning_events(events, SUBJECTS, "1h", NOW, 2)
    assert len(capped["items"]) == 2 and capped["total"] == 5 and capped["truncated"] is True
    assert ing.warning_events(events, SUBJECTS, "2m", NOW, 200)["items"][0]["reason"] == "R0"
    assert len(ing.warning_events(events, SUBJECTS, "2m", NOW, 200)["items"]) == 2
    # a Service that no longer exists (uid None) is matched by name
    gone = [event("Service", "web", "old-uid", "Failed", "m", 1)]
    assert len(ing.warning_events(gone, [("Service", "web", None)], "1h", NOW, 200)["items"]) == 1
    with pytest.raises(RwError):
        ing.warning_events(events, SUBJECTS, "soon", NOW, 200)


def test_warning_event_message_is_cut_and_flagged():
    long = [event("Ingress", "web", ING_UID, "Big", "m" * 5000, 1)]
    row = ing.warning_events(long, SUBJECTS, "1h", NOW, 200)["items"][0]
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

    `fail` maps a resource word (ingresses.networking.k8s.io, services,
    endpointslices.discovery.k8s.io, ingressclasses.networking.k8s.io, events)
    to the stderr kubectl would print. `docs` overrides what a read returns:
    ("ingress", name) is the Ingress, ("service", name) a Service in the
    namespace's Service list (None removes it). `slices` replaces the captured
    EndpointSlices, `classes` is the cluster's IngressClass objects, `events`
    the list `get events` returns.
    """

    def __init__(
        self,
        ns,
        name,
        fail=None,
        docs=None,
        events=None,
        slices=None,
        classes=None,
        rtype="kubernetes/ingress",
        path=None,
    ):
        self.resource = SimpleNamespace(namespace=ns, name=name, type=rtype, path=path)
        self.settings = SimpleNamespace(max_items=200)
        self.fail = fail or {}
        self.docs = docs or {}
        self.events = events or []
        self.slices = slices
        self.classes = classes or []
        self.errors = []
        self.calls = []

    def now(self):
        """The run's reference time: NOW, so the fixed event times are inside the window."""
        return NOW

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message})

    def run(self, argv):
        self.calls.append(argv)
        words, flags, it = [], {}, iter(argv[1:])
        for a in it:
            if a in ("-n", "-o", "-l", "--field-selector"):
                flags[a] = next(it)
            elif not a.startswith("-"):
                words.append(a)
        resource = words[1]
        for word, stderr in self.fail.items():
            if word == resource:
                return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        tag = self.resource.namespace.split("-")[1]
        if resource == "ingresses.networking.k8s.io":
            doc = self.docs.get(("ingress", words[2])) or ingress(tag, words[2])
        elif resource == "services":
            assert len(words) == 2, "services are read as one list"
            items = {i["metadata"]["name"]: i for i in load(f"{tag}_services")["items"]}
            for (kind, name), value in self.docs.items():
                if kind == "service":
                    items.pop(name, None)
                    if value is not None:
                        items[name] = value
            doc = {"items": list(items.values())}
        elif resource == "endpointslices.discovery.k8s.io":
            selector = flags["-l"]
            assert selector.startswith("kubernetes.io/service-name in (") and len(words) == 2
            wanted = selector.split("(", 1)[1].rstrip(")").split(",")
            slices = (
                self.slices if self.slices is not None else load(f"{tag}_endpointslices")["items"]
            )
            doc = {
                "items": [
                    s
                    for s in slices
                    if s["metadata"]["labels"].get("kubernetes.io/service-name") in wanted
                ]
            }
        elif resource == "ingressclasses.networking.k8s.io":
            if len(words) == 2:
                doc = {"items": self.classes}
            else:
                found = [c for c in self.classes if c["metadata"]["name"] == words[2]]
                if not found:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr=f'Error from server (NotFound): ingressclasses.networking.k8s.io "{words[2]}" not found',
                    )
                doc = found[0]
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


def web_with(*backends, **spec):
    """A copy of the captured healthy Ingress with these (service, port) backends."""
    doc = copy.deepcopy(ingress("healthy", "web"))
    paths = []
    for i, (svc, port) in enumerate(backends):
        ref = {"number": port} if isinstance(port, int) else {"name": port}
        paths.append(
            {
                "path": f"/{i}",
                "pathType": "Prefix",
                "backend": {"service": {"name": svc, "port": ref}},
            }
        )
    if paths:
        doc["spec"]["rules"] = [{"host": "h.example", "http": {"paths": paths}}]
    doc["spec"].update(spec)
    return doc


def svc_doc(name, ports, type="ClusterIP", selector=True, **spec):
    return {
        "metadata": {"name": name, "namespace": "rw-healthy", "uid": f"{name}-uid"},
        "spec": {
            "type": type,
            "ports": ports,
            **({"selector": {"app": name}} if selector else {}),
            **spec,
        },
        "status": {"loadBalancer": {}},
    }


def slice_doc(svc, ports, endpoints, name="s"):
    return {
        "metadata": {"name": f"{svc}-{name}", "labels": {"kubernetes.io/service-name": svc}},
        "ports": ports,
        "endpoints": endpoints,
    }


def ep(pod, ready=True, **conditions):
    return {
        "addresses": ["10.0.0.1"],
        "conditions": {"ready": ready, **conditions},
        "targetRef": {"kind": "Pod", "namespace": "rw-healthy", "name": pod},
    }


def test_healthy_ingress_backend_is_clean():
    ctx = FakeCtx("rw-healthy", "web")
    out = run_task("backends", ctx)
    [b] = out["backends"]["items"]
    assert out["backends"]["total"] == 1 and out["backends"]["truncated"] is False
    assert b["service"]["name"] == "web" and b["serviceExists"] is True and b["portExists"] is True
    assert b["serviceType"] == "ClusterIP" and b["targetPort"] == 80
    assert b["hasSelector"] is True and b["publishNotReady"] is False
    assert b["endpoints"] == {
        "ready": 2,
        "notReady": 0,
        "serving": 2,
        "terminating": 0,
        "slices": 1,
    }
    assert out["tls"]["items"] == [] and out["gkeBackends"]["present"] is False
    assert out["gkeLoadBalancer"]["present"] is False
    assert ctx.errors == []


def test_ingress_to_a_missing_service_is_reported_as_missing():
    ctx = FakeCtx("rw-broken", "orphan")
    [b] = run_task("backends", ctx)["backends"]["items"]
    assert b["service"]["name"] == "missing" and b["serviceExists"] is False
    assert b["portExists"] is None and b["endpoints"] is None
    assert ctx.errors == []


def test_wrong_port_is_reported_and_has_no_endpoint_counts_to_give():
    [b] = run_task("backends", FakeCtx("rw-broken", "wrong-port"))["backends"]["items"]
    assert b["serviceExists"] is True and b["portExists"] is False
    assert b["service"]["portNumber"] == 8080 and b["targetPort"] is None
    assert b["endpoints"] is None  # no such port: nothing to count, not a zero


def test_service_without_ready_endpoints_and_a_port_by_name():
    ctx = FakeCtx("rw-broken", "no-endpoints")
    out = run_task("backends", ctx)
    [b] = out["backends"]["items"]
    assert b["service"]["portName"] == "http" and b["portExists"] is True
    # the pod exists (an endpoint) but is not ready: not-ready is counted, not dropped
    assert b["endpoints"] == {
        "ready": 0,
        "notReady": 1,
        "serving": 0,
        "terminating": 0,
        "slices": 1,
    }
    assert out["tls"]["items"] == [
        {
            "hosts": ["no-endpoints.rw-broken.local"],
            "secretName": "no-such-secret",
            "truncated": False,
        }
    ]
    assert all("secrets" not in c and "secret" not in c for c in ctx.calls)


# ------------------------------------------------ endpoints per backend port ----

HTTP = {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 8080}
METRICS = {"name": "metrics", "port": 9090, "protocol": "TCP", "targetPort": 9090}


def multiport_ctx(slices, backends=(("mp", "http"), ("mp", "metrics"))):
    return FakeCtx(
        "rw-healthy",
        "web",
        docs={
            ("ingress", "web"): web_with(*backends),
            ("service", "mp"): svc_doc("mp", [HTTP, METRICS]),
        },
        slices=slices,
    )


def test_multi_port_service_counts_endpoints_only_from_slices_carrying_the_backend_port():
    # the pod exposes only the metrics port: the http backend has no endpoint
    slices = [slice_doc("mp", [{"name": "metrics", "port": 9090}], [ep("a")])]
    http, metrics = run_task("backends", multiport_ctx(slices))["backends"]["items"]
    assert http["service"]["portName"] == "http"
    assert http["endpoints"] == {
        "ready": 0,
        "notReady": 0,
        "serving": 0,
        "terminating": 0,
        "slices": 0,
    }
    assert metrics["endpoints"]["ready"] == 1 and metrics["endpoints"]["slices"] == 1


def test_multi_port_service_port_carried_by_its_own_slice():
    slices = [
        slice_doc("mp", [{"name": "metrics", "port": 9090}], [ep("a")], name="m"),
        slice_doc(
            "mp",
            [{"name": "http", "port": 8080}, {"name": "metrics", "port": 9090}],
            [ep("b"), ep("c", ready=False, serving=True)],
            name="hm",
        ),
    ]
    http, metrics = run_task("backends", multiport_ctx(slices))["backends"]["items"]
    assert http["endpoints"] == {
        "ready": 1,
        "notReady": 1,
        "serving": 2,
        "terminating": 0,
        "slices": 1,
    }
    assert metrics["endpoints"]["ready"] == 2 and metrics["endpoints"]["slices"] == 2


def test_backend_by_port_number_resolves_the_same_service_port():
    slices = [slice_doc("mp", [{"name": "metrics", "port": 9090}], [ep("a")])]
    [b] = run_task("backends", multiport_ctx(slices, backends=(("mp", 80),)))["backends"]["items"]
    assert b["portExists"] is True and b["endpoints"]["ready"] == 0


def test_slices_for_an_unnamed_single_port_match_on_the_target_port():
    sp = {"port": 80, "targetPort": 8080}
    yes = slice_doc("s", [{"name": "", "port": 8080}], [])
    no = slice_doc("s", [{"name": "", "port": 9999}], [])
    named = slice_doc("s", [{"name": "http", "port": 8080}], [])
    bare = slice_doc("s", [], [])
    assert ing.slices_for_port([yes, no, named, bare], sp) == [yes]
    # a container port name cannot be resolved from the Service: any unnamed port counts
    assert ing.slices_for_port([yes, no], {"port": 80, "targetPort": "web"}) == [yes, no]


def test_a_pod_is_ready_if_ready_in_any_counted_slice_and_counted_once():
    slices = [
        slice_doc("s", [{"name": "http", "port": 1}], [ep("a", ready=False)], name="1"),
        slice_doc("s", [{"name": "http", "port": 1}], [ep("a", ready=True)], name="2"),
    ]
    counts = ing.endpoint_counts(slices)
    assert counts["ready"] == 1 and counts["notReady"] == 0 and counts["slices"] == 2


def test_serving_and_terminating_endpoints_are_counted():
    sl = slice_doc(
        "s",
        [],
        [
            ep("a"),
            ep("b", ready=False, serving=True, terminating=True),
            ep("c", ready=False, serving=False, terminating=True),
            ep("d", ready=False, serving=False),
        ],
    )
    assert ing.endpoint_counts([sl]) == {
        "ready": 1,
        "notReady": 3,
        "serving": 2,
        "terminating": 2,
        "slices": 1,
    }


def test_service_without_selector_and_publish_not_ready_are_reported():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={
            ("ingress", "web"): web_with(("manual", 80)),
            ("service", "manual"): svc_doc(
                "manual",
                [{"port": 80, "targetPort": 80}],
                selector=False,
                publishNotReadyAddresses=True,
            ),
        },
    )
    [b] = run_task("backends", ctx)["backends"]["items"]
    assert b["hasSelector"] is False and b["publishNotReady"] is True
    # no slices at all: {0, 0} is the answer, and the flags say why that can be normal
    assert b["endpoints"] == {
        "ready": 0,
        "notReady": 0,
        "serving": 0,
        "terminating": 0,
        "slices": 0,
    }


def test_external_name_service_without_ports_has_no_authoritative_port_check():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={
            ("ingress", "web"): web_with(("ext", 443)),
            ("service", "ext"): svc_doc(
                "ext", [], type="ExternalName", selector=False, externalName="x.example"
            ),
        },
    )
    [b] = run_task("backends", ctx)["backends"]["items"]
    assert b["serviceType"] == "ExternalName" and b["serviceExists"] is True
    assert b["portExists"] is None and b["hasSelector"] is False
    assert b["endpoints"]["ready"] == 0
    # an ExternalName Service that does declare ports is matched like any other
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={
            ("ingress", "web"): web_with(("ext", 443)),
            ("service", "ext"): svc_doc("ext", [{"port": 80}], type="ExternalName", selector=False),
        },
    )
    assert run_task("backends", ctx)["backends"]["items"][0]["portExists"] is False


# ------------------------------------------------------------ fan-out ----


def kinds(ctx):
    return sorted(c[c.index("get") + 1] for c in ctx.calls)


def test_backends_reads_services_and_slices_once_each_whatever_the_backend_count():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={
            ("ingress", "web"): web_with(("a", 80), ("b", 80), ("web", 80), ("a", 81)),
            ("service", "a"): svc_doc("a", [{"port": 80}, {"port": 81}]),
            ("service", "b"): svc_doc("b", [{"port": 80}]),
        },
    )
    out = run_task("backends", ctx)
    assert len(out["backends"]["items"]) == 4
    assert kinds(ctx) == [
        "endpointslices.discovery.k8s.io",
        "ingresses.networking.k8s.io",
        "services",
    ]
    slice_call = next(c for c in ctx.calls if "endpointslices.discovery.k8s.io" in c)
    assert slice_call[slice_call.index("-l") + 1] == "kubernetes.io/service-name in (a,b,web)"


def test_address_and_warning_events_read_the_services_once():
    ctx = FakeCtx("rw-healthy", "web")
    run_task("address", ctx)
    assert kinds(ctx).count("services") == 1
    ctx = FakeCtx("rw-healthy", "web")
    run_task("warning-events", ctx)
    assert kinds(ctx) == ["events", "ingresses.networking.k8s.io", "services"]


def test_backend_services_looked_up_are_capped_at_100_and_the_cut_is_reported():
    many = [(f"svc{i:03d}", 80) for i in range(120)]
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): web_with(*many)})
    out = run_task("backends", ctx)
    items = out["backends"]["items"]
    assert out["backends"]["servicesTruncated"] is True and len(items) == 120
    assert items[99]["serviceExists"] is False  # looked up, not there
    assert items[100]["serviceExists"] is None  # past the cap: not looked up, not "missing"
    addr = run_task(
        "address", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): web_with(*many)})
    )
    assert addr["serviceAddresses"]["servicesTruncated"] is True
    ev = run_task(
        "warning-events", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): web_with(*many)})
    )
    assert ev["inspected"]["servicesTruncated"] is True and len(ev["inspected"]["services"]) == 100


def test_a_namespace_with_no_matching_services_makes_no_endpointslice_call():
    ctx = FakeCtx("rw-broken", "orphan")
    run_task("backends", ctx)
    assert "endpointslices.discovery.k8s.io" not in kinds(ctx)


def test_backends_script_reports_gke_annotation_and_a_malformed_one_is_an_error_not_healthy():
    doc = with_annotation(GKE)
    out = run_task("backends", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc}))
    assert out["gkeBackends"]["notHealthy"] == 2
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): with_annotation("{oops")})
    out = run_task("backends", ctx)
    assert out["gkeBackends"]["backends"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("internal", "annotations")]
    assert out["backends"]["items"][0]["serviceExists"] is True  # the rest is still returned


def test_forbidden_services_leave_service_facts_null_with_an_error_and_keep_the_rest():
    ctx = FakeCtx("rw-healthy", "web", fail={"services": FORBIDDEN % "services"})
    out = run_task("backends", ctx)
    [b] = out["backends"]["items"]
    assert b["service"]["name"] == "web"
    assert b["serviceExists"] is None and b["portExists"] is None and b["endpoints"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "services")]


def test_forbidden_endpointslices_leave_endpoints_null_not_zero():
    ctx = FakeCtx(
        "rw-healthy", "web", fail={"endpointslices.discovery.k8s.io": FORBIDDEN % "endpointslices"}
    )
    [b] = run_task("backends", ctx)["backends"]["items"]
    assert b["serviceExists"] is True and b["portExists"] is True
    assert b["endpoints"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "endpointslices")
    ]


def test_forbidden_service_read_is_reported_once_not_per_service():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={("ingress", "web"): web_with(("web", 80), ("a", 80), ("b", 80), ("c", 80))},
        fail={"services": FORBIDDEN % "services"},
    )
    out = run_task("backends", ctx)
    assert len(out["backends"]["items"]) == 4 and len(ctx.errors) == 1


# ----------------------------------------------------------------- GKE ----


def test_neg_style_backend_names_give_service_and_port():
    name = "k8s1-1a2b3c4d-rw-healthy-my-web-svc-8080-9f8e7d6c"
    assert ing.parse_neg_name(name, "rw-healthy") == ("my-web-svc", 8080)
    # the namespace is not the Ingress's: the split is ambiguous, so only the port is read
    assert ing.parse_neg_name(name, "other") == (None, 8080)
    assert ing.parse_neg_name(name, None) == (None, 8080)
    # instance-group and unrelated names do not parse
    assert ing.parse_neg_name("k8s-be-30080--0123456789abcdef", "rw-healthy") == (None, None)
    assert ing.parse_neg_name("something-else", "rw-healthy") == (None, None)
    assert ing.parse_neg_name("k8s1-1a2b3c4d-rw-healthy-80-9f8e7d6c", "rw-healthy") == (None, 80)


def test_gke_backends_carry_the_parsed_service_and_port():
    doc = with_annotation(
        json.dumps(
            {
                "k8s1-1a2b3c4d-rw-healthy-web-80-9f8e7d6c": "UNHEALTHY",
                "k8s-be-30080--0123456789abcdef": "HEALTHY",
            }
        )
    )
    view, _ = ing.gke_backends(doc, 200)
    unhealthy, healthy = view["backends"]
    assert (unhealthy["service"], unhealthy["port"]) == ("web", 80)
    assert (healthy["service"], healthy["port"]) == (None, None)


def test_gke_load_balancer_annotations_are_reported_and_bounded():
    doc = copy.deepcopy(ingress("healthy", "web"))
    assert ing.gke_load_balancer(doc) == {
        "present": False,
        "forwardingRule": None,
        "httpsForwardingRule": None,
        "urlMap": None,
        "targetProxy": None,
        "httpsTargetProxy": None,
        "staticIp": None,
    }
    doc["metadata"].setdefault("annotations", {}).update(
        {
            "ingress.kubernetes.io/forwarding-rule": "k8s-fw-ns-web--abc",
            "ingress.kubernetes.io/https-forwarding-rule": "k8s2-fs-ns-web--abc",
            "ingress.kubernetes.io/url-map": "k8s2-um-ns-web--abc",
            "ingress.kubernetes.io/target-proxy": "k8s-tp-ns-web--abc",
            "ingress.kubernetes.io/https-target-proxy": "k8s2-ts-ns-web--abc",
            "ingress.kubernetes.io/static-ip": "k8s-fw-ns-web--abc" + "x" * 3000,
        }
    )
    lbv = ing.gke_load_balancer(doc)
    assert lbv["present"] is True and lbv["urlMap"] == "k8s2-um-ns-web--abc"
    assert lbv["httpsTargetProxy"] == "k8s2-ts-ns-web--abc" and len(lbv["staticIp"]) == 1024
    out = run_task("backends", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc}))
    assert out["gkeLoadBalancer"]["forwardingRule"] == "k8s-fw-ns-web--abc"


# ------------------------------------------------------------- address ----


def test_address_of_a_healthy_ingress_on_a_cluster_without_a_controller():
    ctx = FakeCtx("rw-healthy", "web")
    out = run_task("address", ctx)
    assert out["ingress"]["assigned"] is False and out["ingress"]["addresses"] == []
    # no class named, no IngressClass objects: no default, and that is a fact, not unknown
    assert out["ingressClass"]["classSource"] == "none" and out["ingressClass"]["lookedUp"] is False
    assert out["ingressClass"]["defaultClasses"] == []
    assert out["serviceAddresses"]["items"][0]["type"] == "ClusterIP"
    assert ctx.errors == []


@pytest.mark.parametrize(
    "classes,defaults,effective,multiple",
    [
        ([], [], None, False),
        ([ic("nginx")], [], None, False),
        ([ic("nginx"), ic("gce", default=True)], ["gce"], "gce", False),
        ([ic("b", default=True), ic("a", default=True)], ["a", "b"], None, True),
    ],
)
def test_address_script_with_no_class_named_lists_the_default_classes(
    classes, defaults, effective, multiple
):
    ctx = FakeCtx("rw-healthy", "web", classes=classes)
    view = run_task("address", ctx)["ingressClass"]
    assert view["defaultClasses"] == defaults and view["effectiveClass"] == effective
    assert view["multipleDefaults"] is multiple and view["classSource"] == "none"
    assert ctx.errors == []


def test_forbidden_class_list_is_unknown_with_an_error_and_keeps_the_address():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={("ingress", "web"): addressed("34.1.1.1")},
        fail={"ingressclasses.networking.k8s.io": FORBIDDEN % "ingressclasses"},
    )
    out = run_task("address", ctx)
    assert out["ingressClass"]["defaultClasses"] is None
    assert out["ingressClass"]["multipleDefaults"] is None
    assert out["ingress"]["assigned"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "ingressclasses")
    ]


def test_legacy_class_annotation_looks_up_no_ingress_class():
    doc = copy.deepcopy(ingress("healthy", "web"))
    doc["metadata"].setdefault("annotations", {})[ing.CLASS_ANNOTATION] = "gce"
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc})
    view = run_task("address", ctx)["ingressClass"]
    assert view["classSource"] == "annotation" and view["effectiveClass"] == "gce"
    assert "ingressclasses.networking.k8s.io" not in kinds(ctx)


def test_address_script_with_a_missing_ingress_class_and_a_load_balancer_conflict():
    doc = addressed("34.1.1.1")
    doc["spec"]["ingressClassName"] = "nginx"
    svc = lb_service(lb("35.2.2.2"))
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc, ("service", "web"): svc})
    out = run_task("address", ctx)
    assert out["ingress"]["ingressClassName"] == "nginx" and out["ingress"]["assigned"] is True
    assert out["ingressClass"]["exists"] is False and out["ingressClass"]["lookedUp"] is True
    assert out["serviceAddresses"]["items"][0]["differsFromIngress"] is True
    assert ctx.errors == []


def test_named_class_that_exists_reports_its_controller():
    doc = web_with(("web", 80), ingressClassName="nginx")
    ctx = FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc}, classes=[ic("nginx", True)])
    view = run_task("address", ctx)["ingressClass"]
    assert view["exists"] is True and view["controller"] == "k8s.io/ingress-nginx"
    assert view["isDefault"] is True and view["defaultClasses"] is None


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
    view = out["ingressClass"]
    # forbidden is told apart from "no class named" by classSource and the error
    assert view["classSource"] == "spec" and view["exists"] is None and view["lookedUp"] is False
    assert (
        out["ingress"]["assigned"] is True and out["serviceAddresses"]["items"][0]["exists"] is True
    )
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "ingressclasses")
    ]


# -------------------------------------------------------------- events ----


def test_warning_events_script_matches_by_uid_and_reports_what_it_inspected():
    svc_uid = service("broken", "crashloop")["metadata"]["uid"]
    ing_uid = ingress("broken", "wrong-port")["metadata"]["uid"]
    events = [
        event("Ingress", "wrong-port", ing_uid, "Translate", "port 8080 not found", 3),
        event("Service", "crashloop", svc_uid, "SyncFailed", "boom", 4),
        event("Service", "crashloop", "uid-of-an-older-service", "SyncFailed", "ghost", 2),
    ]
    out = run_task("warning-events", FakeCtx("rw-broken", "wrong-port", events=events), since="20m")
    assert [(e["kind"], e["reason"]) for e in out["events"]["items"]] == [
        ("Ingress", "Translate"),
        ("Service", "SyncFailed"),
    ]
    assert out["events"]["items"][1]["message"] == "boom"
    assert out["inspected"] == {
        "ingress": "wrong-port",
        "services": ["crashloop"],
        "servicesLookedUp": True,
        "servicesTruncated": False,
    }


def test_warning_events_with_forbidden_services_match_by_name_and_say_so():
    events = [event("Service", "web", "any-uid", "SyncFailed", "boom", 3)]
    ctx = FakeCtx("rw-healthy", "web", events=events, fail={"services": FORBIDDEN % "services"})
    out = run_task("warning-events", ctx, since="20m")
    assert len(out["events"]["items"]) == 1 and out["inspected"]["servicesLookedUp"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "services")]


def test_no_warning_events_is_an_empty_list_when_events_could_be_read():
    out = run_task("warning-events", FakeCtx("rw-healthy", "web"))
    assert out["events"] == {"items": [], "total": 0, "truncated": False}
    assert out["inspected"]["servicesLookedUp"] is True


# -------------------------------------------------------------- bounds ----


def test_free_text_fields_are_cut_at_1024_and_validate_against_the_schemas():
    long = "x" * 3000
    doc = web_with(("web", 80))
    doc["spec"]["rules"][0]["host"] = long
    doc["spec"]["rules"][0]["http"]["paths"][0]["path"] = "/" + long
    doc["spec"]["tls"] = [{"hosts": [long] + [f"h{i}" for i in range(80)], "secretName": long}]
    doc["metadata"].setdefault("annotations", {})[ing.GKE_BACKENDS_ANNOTATION] = json.dumps(
        {long: long}
    )
    out = run_task("backends", FakeCtx("rw-healthy", "web", docs={("ingress", "web"): doc}))
    b = out["backends"]["items"][0]
    assert len(b["host"]) == 1024 and len(b["path"]) == 1024
    tls = out["tls"]["items"][0]
    assert len(tls["secretName"]) == 1024 and len(tls["hosts"]) == 50 and tls["truncated"] is True
    assert len(out["gkeBackends"]["backends"][0]["name"]) == 1024
    assert len(out["gkeBackends"]["backends"][0]["health"]) == 1024
    events = [
        {**event("Ingress", "web", ING_UID, "R", "m", 1), "reportingComponent": long},
    ]
    ev = run_task("warning-events", FakeCtx("rw-healthy", "web", events=events))
    assert len(ev["events"]["items"][0]["reportingComponent"]) == 1024


def test_lists_cut_at_max_items_report_total_and_truncated():
    ctx = FakeCtx(
        "rw-healthy",
        "web",
        docs={("ingress", "web"): web_with(*[("web", 80)] * 5)},
        events=[event("Ingress", "web", ING_UID, f"R{i}", "m", 1) for i in range(5)],
    )
    ctx.settings.max_items = 2
    out = run_task("backends", ctx)
    assert len(out["backends"]["items"]) == 2
    assert out["backends"]["total"] == 5 and out["backends"]["truncated"] is True
    ev = run_task("warning-events", ctx)
    assert len(ev["events"]["items"]) == 2 and ev["events"]["total"] == 5
    assert ev["events"]["truncated"] is True
    addr = run_task("address", ctx)
    assert addr["serviceAddresses"]["total"] == 1 and addr["serviceAddresses"]["truncated"] is False


def test_nested_address_list_is_cut_at_50_and_flagged():
    doc = addressed(*[f"10.0.{i // 250}.{i % 250 + 1}" for i in range(80)])
    view = ing.ingress_view(doc)
    assert len(view["addresses"]) == 50 and view["addressesTruncated"] is True
    assert view["assigned"] is True


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
    ctx = FakeCtx("rw-healthy", "web", fail={"services": DOWN})
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
    assert run_task("backends", ctx)["backends"]["items"][0]["serviceExists"] is True


# ------------------------------------------------------------------ RBAC ----

RESOURCES = {
    "ingresses.networking.k8s.io": ("networking.k8s.io", "ingresses"),
    "ingressclasses.networking.k8s.io": ("networking.k8s.io", "ingressclasses"),
    "services": ("", "services"),
    "events": ("", "events"),
    "endpointslices.discovery.k8s.io": ("discovery.k8s.io", "endpointslices"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-l", "--selector", "--field-selector", "--context"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list.
    Raises on anything else, so a skill that issues a new verb has to extend this mapping.
    """
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    if positional[0] != "get":
        raise AssertionError(f"no RBAC mapping for kubectl {positional[0]}: {argv}")
    group, plural = RESOURCES[positional[1]]
    named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
    return {(group, plural, "get" if named_object else "list")}


def declared(task):
    return {
        (g, r, v)
        for rule in TASKS[task]["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "services", "-o", "json"]) == {
        ("", "services", "list")
    }
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "ingresses.networking.k8s.io", "web", "-o", "json"]
    ) == {("networking.k8s.io", "ingresses", "get")}
    assert rbac_needs(
        [
            "kubectl",
            "-n",
            "x",
            "get",
            "endpointslices.discovery.k8s.io",
            "-l",
            "a in (b)",
            "-o",
            "json",
        ]
    ) == {("discovery.k8s.io", "endpointslices", "list")}
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "events", "--field-selector", "type=Warning", "-o", "json"]
    ) == {("", "events", "list")}
    with pytest.raises(AssertionError):
        rbac_needs(["kubectl", "delete", "services", "x"])


def _targets():
    named_class = web_with(("web", 80), ingressClassName="nginx")
    legacy = copy.deepcopy(ingress("healthy", "web"))
    legacy["metadata"].setdefault("annotations", {})[ing.CLASS_ANNOTATION] = "gce"
    return [
        FakeCtx("rw-healthy", "web"),
        FakeCtx("rw-broken", "orphan"),
        FakeCtx("rw-broken", "no-endpoints"),
        FakeCtx("rw-healthy", "web", docs={("ingress", "web"): named_class}, classes=[ic("nginx")]),
        FakeCtx("rw-healthy", "web", docs={("ingress", "web"): legacy}),
        FakeCtx("rw-healthy", "web", docs={("ingress", "web"): web_with()}),
    ]


@pytest.mark.parametrize("task", sorted(TASKS))
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task):
    needed = set()
    for ctx in _targets():
        run_task(task, ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    missing = needed - declared(task)
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"
    # and declares nothing it never asks for
    assert declared(task) <= needed | {("networking.k8s.io", "ingressclasses", "get")}, task
