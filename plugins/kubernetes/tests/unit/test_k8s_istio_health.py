"""Unit tests for k8s-istio-health's lib/ and scripts against JSON captured from the
kind-rw-plugins scenario (plugins/kubernetes/tests/kind, namespace rw-istio and rw-healthy).

No Istio runs there: the "sidecars" are busybox containers named istio-proxy printing
Envoy-like log lines, the istiod Deployment is a busybox container named discovery, and the
webhook configurations are named like Istio's but cannot match any object."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from k8s_istio_health import common, injection, k8s, proxy
from k8s_istio_health import control as control_lib
from k8s_istio_health.errors import RwError
from k8s_istio_health.sidecar import find_sidecar
from runwhen_capability.rwerrors import RwCommandError, classify_command_error

SKILL = Path(__file__).parents[2] / "skills" / "k8s-istio-health"
DATA = Path(__file__).parent / "data" / "k8s_istio_health"
SCRIPT_OF = {
    "injection-status": "injection_status",
    "control-plane-status": "control_plane_status",
    "proxy-errors": "proxy_errors",
}
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
NS, DEP = "kubernetes/namespace", "kubernetes/deployment"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def workload(view, name):
    return next(w for w in view["workloads"] if w["name"] == name)


def schema_of(task, output):
    return json.loads((SKILL / TASKS[task]["outputs"][output]["schema"]).read_text())


def check_outputs(task, out):
    for name, value in out.items():
        jsonschema.validate(value, schema_of(task, name), cls=jsonschema.Draft202012Validator)
    assert set(out) == set(TASKS[task]["outputs"])


FORBIDDEN = (
    'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" '
    'cannot list resource "x" in API group "" in the namespace "rw-istio"'
)
UNREACHABLE = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"


class FakeCtx:
    """Replays captured JSON for kubectl calls, raising like the SDK's ctx.run on a non-zero exit.

    `fail` maps a kubectl noun (or 'logs') to the stderr kubectl would print; `logs` maps a pod's
    app label to log text (default: the captured log for `meshed` / `meshed-quiet`).
    `overrides` replaces the captured document of a noun (a list of items)."""

    def __init__(
        self,
        ns,
        name,
        rtype,
        fail=None,
        overrides=None,
        logs=None,
        settings=None,
        input_ns="rw-istio",
    ):
        self.resource = SimpleNamespace(namespace=ns, name=name, type=rtype)
        self.settings = SimpleNamespace(
            **{
                "max_items": 200,
                "max_pods": 20,
                "max_scan_lines": 5000,
                "scan_budget_seconds": 90,
                **(settings or {}),
            }
        )
        self.fail = fail or {}
        self.overrides = overrides or {}
        self.logs = logs or {}
        self.data_ns = input_ns
        self.errors, self.messages, self.calls = [], [], []

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope})
        self.messages.append(message)

    @staticmethod
    def _done(argv, rc, out, err, check):
        if rc and check:
            raise RwCommandError(
                classify_command_error(argv, rc, err),
                f"{argv[0]} exited {rc}: {err}",
                argv=argv,
                returncode=rc,
                stderr=err,
            )
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def _items(self, noun):
        if noun in self.overrides:
            return {"items": self.overrides[noun]}
        data = {
            "deployments.apps": "deployments",
            "statefulsets.apps": "statefulsets",
            "daemonsets.apps": "daemonsets",
            "replicasets.apps": "replicasets",
            "pods": "pods",
        }
        if noun in data:
            return load(f"{self.data_ns}_{data[noun]}")
        if noun == "endpointslices.discovery.k8s.io":
            return load("rw-istio_endpointslices_istiod")
        if noun == "mutatingwebhookconfigurations.admissionregistration.k8s.io":
            return load("mutatingwebhookconfigurations")
        if noun == "validatingwebhookconfigurations.admissionregistration.k8s.io":
            return load("validatingwebhookconfigurations")
        raise AssertionError(noun)

    def run(self, argv, check=True, timeout=None):
        self.calls.append(argv)
        args = argv[1:]
        if "-n" in args:
            i = args.index("-n")
            args = args[:i] + args[i + 2 :]
        if "logs" in args:
            return self._logs(argv, args, check)
        words, selector, it = [], None, iter(args)
        for a in it:
            if a == "-o":
                next(it)
            elif a == "-l":
                selector = next(it)
            elif not a.startswith("-"):
                words.append(a)
        _get, noun, *rest = words
        if noun in self.fail:
            return self._done(argv, 1, "", self.fail[noun], check)
        if noun == "namespaces" and "namespace" in self.overrides:
            return self._done(argv, 0, json.dumps(self.overrides["namespace"]), "", check)
        if noun == "namespaces":
            doc = load(f"{rest[0]}_namespace") if rest[0] in ("rw-istio", "rw-healthy") else None
            if doc is None:
                return self._done(
                    argv,
                    1,
                    "",
                    f'Error from server (NotFound): namespaces "{rest[0]}" not found',
                    check,
                )
            return self._done(argv, 0, json.dumps(doc), "", check)
        doc = self._items(noun)
        if rest:
            match = [i for i in doc["items"] if i["metadata"]["name"] == rest[0]]
            if not match:
                err = f'Error from server (NotFound): {noun.split(".")[0]} "{rest[0]}" not found'
                return self._done(argv, 1, "", err, check)
            doc = match[0]
        elif selector:
            doc = {"items": [i for i in doc["items"] if selected(i, selector)]}
        return self._done(argv, 0, json.dumps(doc), "", check)

    def _logs(self, argv, args, check):
        if "logs" in self.fail:
            return self._done(argv, 1, "", self.fail["logs"], check)
        pod = args[args.index("logs") + 1]
        pods = self.overrides.get("pods") or load(f"{self.data_ns}_pods")["items"]
        app = named({"items": pods}, pod)["metadata"]["labels"]["app"]
        if isinstance(self.logs.get(app), BaseException):
            raise self.logs[app]
        if app in self.logs:
            text = self.logs[app]
        else:
            text = (DATA / f"{app}_istio-proxy.log").read_text()
        return self._done(argv, 0, text, "", check)


def selected(item, selector):
    labels = item["metadata"].get("labels") or {}
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if key.startswith("kubernetes.io/") and key not in labels:
            continue
        if labels.get(key) != value:
            return False
    return True


def script(name):
    spec = importlib.util.spec_from_file_location(f"istio_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def injection_status(ctx):
    return script("injection_status")(ctx)


def ns_ctx(name="rw-istio", **kw):
    return FakeCtx(None, name, NS, input_ns=name, **kw)


def dep_ctx(name, ns="rw-istio", **kw):
    return FakeCtx(ns, name, DEP, input_ns=ns, **kw)


# ------------------------------------------------------------ lib: sidecar and labels ----


def pod(
    name="p",
    containers=("app", "istio-proxy"),
    init=(),
    app="x",
    uid="u-",
    status=None,
    labels=None,
):
    return {
        "metadata": {"name": name, "labels": labels or {"app": app}, "uid": uid + name},
        "spec": {
            "containers": [{"name": c, "image": f"img/{c}:1.0"} for c in containers],
            "initContainers": [
                {"name": c, "image": f"img/{c}:1.0", "restartPolicy": "Always"} for c in init
            ],
        },
        "status": status or {},
    }


def test_namespace_injection_modes():
    ni = injection.namespace_injection
    ns = lambda labels: {"metadata": {"labels": labels}}  # noqa: E731
    assert ni(ns({"istio-injection": "enabled"}))["mode"] == "label"
    assert ni(ns({"istio.io/rev": "1-20"})) == {
        "label": None,
        "revision": "1-20",
        "dataplaneMode": None,
        "excluded": False,
        "enabled": True,
        "mode": "revision",
    }
    # istio-injection wins: the revisioned webhooks require that the label does not exist
    assert ni(ns({"istio-injection": "disabled", "istio.io/rev": "1-20"}))["enabled"] is False
    assert ni(ns({"istio-injection": "true"}))["mode"] == "disabled"
    assert ni(ns({}))["mode"] == "unlabelled" and ni(ns({}))["enabled"] is False
    assert ni(None) == {
        "label": None,
        "revision": None,
        "dataplaneMode": None,
        "excluded": False,
        "enabled": None,
        "mode": None,
    }


def test_injection_expected_follows_namespace_mode_template_override_and_host_network():
    on = injection.namespace_injection({"metadata": {"labels": {"istio-injection": "enabled"}}})
    off = injection.namespace_injection({"metadata": {}})
    unknown = injection.namespace_injection(None)
    e = injection.injection_expected
    assert e(on, None, None, False) is True
    assert e(on, "false", "label", False) is False  # explicit opt-out
    assert e(on, None, None, True) is False  # host network is never injected
    assert e(off, None, None, False) is False
    assert e(off, "true", "label", False) is True  # the pod label works in an unlabelled namespace
    assert e(off, "true", "annotation", False) is False  # the annotation does not
    assert e(unknown, None, None, False) is None  # nothing says
    assert e(unknown, "true", "label", False) is True
    assert e(unknown, "false", "annotation", False) is False


def test_inject_override_prefers_the_label_to_the_annotation():
    t = {
        "metadata": {
            "labels": {"sidecar.istio.io/inject": "True"},
            "annotations": {"sidecar.istio.io/inject": "false"},
        }
    }
    assert injection.inject_override(t) == ("true", "label")
    t = {"metadata": {"annotations": {"sidecar.istio.io/inject": "false"}}}
    assert injection.inject_override(t) == ("false", "annotation")
    assert injection.inject_override({}) == (None, None)


def test_native_sidecar_init_container_counts():
    native = pod(containers=("app",), init=("istio-proxy",))
    container, is_native = find_sidecar(native)
    assert container["name"] == "istio-proxy" and is_native is True
    assert find_sidecar(pod(containers=("app",))) == (None, False)
    assert find_sidecar(pod())[1] is False


# ------------------------------------------------------------ injection-status ----


def test_mesh_namespace_reports_labels_and_who_has_the_sidecar():
    ctx = ns_ctx("rw-istio")
    out = injection_status(ctx)
    check_outputs("injection-status", out)
    assert out["injection"] == {
        "label": "enabled",
        "revision": None,
        "dataplaneMode": None,
        "excluded": False,
        "enabled": True,
        "mode": "label",
    }
    assert ctx.errors == []
    meshed = workload(out, "meshed")
    assert (meshed["pods"], meshed["podsWithSidecar"], meshed["podsWithoutSidecar"]) == (1, 1, 0)
    assert meshed["missingWhileExpected"] is False and meshed["sidecarNotReady"] == 0
    assert meshed["sidecarInTemplate"] is True
    assert meshed["sidecarResources"] == [
        {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": None, "memory": "64Mi"},
            "pods": 1,
        }
    ]
    bare = workload(out, "bare")
    assert bare["injectionExpected"] is True and bare["podsWithoutSidecar"] == 1
    assert bare["missingWhileExpected"] is True and len(bare["podsMissingSidecar"]) == 1
    assert bare["podsMissingSidecar"][0].startswith("bare-")
    assert bare["sidecarResources"] == []
    # missing-while-expected workloads come first
    assert out["workloads"][0]["missingWhileExpected"] is True


def test_opt_out_workload_without_sidecar_is_not_reported_as_missing():
    out = injection_status(ns_ctx("rw-istio"))
    optout = workload(out, "bare-optout")
    assert optout["injectOverride"] == "false" and optout["injectOverrideSource"] == "label"
    assert optout["injectionExpected"] is False
    assert optout["podsWithoutSidecar"] == 1 and optout["missingWhileExpected"] is False


def test_unready_proxy_is_counted():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    gateway = next(p for p in pods if p["metadata"]["labels"]["app"] == "istio-ingressgateway")
    out = injection_status(ns_ctx("rw-istio", overrides={"pods": pods}))
    w = workload(out, "istio-ingressgateway")
    assert w["podsWithSidecar"] == 1 and w["sidecarNotReady"] == 1
    assert gateway["status"]["containerStatuses"][0]["ready"] is False


def test_plain_namespace_expects_nothing_and_reports_nothing_missing():
    out = injection_status(ns_ctx("rw-healthy"))
    check_outputs("injection-status", out)
    assert out["injection"]["mode"] == "unlabelled" and out["injection"]["enabled"] is False
    assert out["workloads"]
    assert all(
        w["injectionExpected"] is False and w["missingWhileExpected"] is False
        for w in out["workloads"]
    )
    assert all(w["podsWithSidecar"] == 0 for w in out["workloads"])
    assert {w["kind"] for w in out["workloads"]} == {"Deployment", "StatefulSet", "DaemonSet"}


def test_template_inject_label_with_injected_pods_is_not_a_false_fail():
    # Classic looked for istio-proxy in the Deployment template, where injected sidecars never
    # appear, and reported a correctly injected workload as missing it.
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    meshed = named({"items": deployments}, "meshed")
    meshed["spec"]["template"]["spec"]["containers"] = [
        c for c in meshed["spec"]["template"]["spec"]["containers"] if c["name"] != "istio-proxy"
    ]
    meshed["spec"]["template"]["metadata"]["labels"]["sidecar.istio.io/inject"] = "true"
    out = injection_status(ns_ctx("rw-istio", overrides={"deployments.apps": deployments}))
    w = workload(out, "meshed")
    assert w["sidecarInTemplate"] is False
    assert w["podsWithSidecar"] == 1 and w["missingWhileExpected"] is False


def test_namespace_label_removed_but_pods_without_sidecar_expect_nothing():
    ns_doc = copy.deepcopy(load("rw-istio_namespace"))
    ns_doc["metadata"]["labels"].pop("istio-injection")
    ctx = ns_ctx("rw-istio")
    ctx.overrides["x"] = None  # keep the standard replay, only the Namespace differs

    class Ctx(FakeCtx):
        def run(self, argv, check=True, timeout=None):
            if argv[1:4] == ["get", "namespaces", "rw-istio"]:
                return self._done(argv, 0, json.dumps(ns_doc), "", check)
            return super().run(argv, check, timeout)

    out = injection_status(Ctx(None, "rw-istio", NS, input_ns="rw-istio"))
    assert out["injection"]["mode"] == "unlabelled"
    assert workload(out, "bare")["missingWhileExpected"] is False


def test_unowned_pods_are_counted_not_attributed():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    stray = copy.deepcopy(pods[0])
    stray["metadata"]["name"] = "stray"
    stray["metadata"]["ownerReferences"] = [
        {"kind": "Job", "name": "j", "uid": "j-1", "controller": True}
    ]
    stray["metadata"]["labels"] = {"app": "stray"}
    out = injection_status(ns_ctx("rw-istio", overrides={"pods": pods + [stray]}))
    assert out["inspected"]["pods"] == len(pods) + 1
    assert out["inspected"]["podsOutsideWorkloads"] == {"pods": 1, "withSidecar": 0}


def test_a_pod_with_the_right_labels_but_another_owner_is_not_the_workloads():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    impostor = copy.deepcopy(next(p for p in pods if p["metadata"]["labels"]["app"] == "bare"))
    impostor["metadata"]["name"] = "impostor"
    impostor["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "x", "uid": "other", "controller": True}
    ]
    out = injection_status(ns_ctx("rw-istio", overrides={"pods": pods + [impostor]}))
    assert workload(out, "bare")["pods"] == 1
    assert out["inspected"]["podsOutsideWorkloads"]["pods"] == 1


def test_workload_list_is_capped_by_max_items():
    out = injection_status(ns_ctx("rw-istio", settings={"max_items": 2}))
    assert len(out["workloads"]) == 2
    assert (
        out["inspected"]["total"]["workloads"] == 6
        and out["inspected"]["omitted"]["workloads"] == 4
    )


def test_nested_pod_names_are_capped_at_50():
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    rs = load("rw-istio_replicasets")["items"]
    bare_pod = next(p for p in pods if p["metadata"]["labels"]["app"] == "bare")
    for i in range(70):
        c = copy.deepcopy(bare_pod)
        c["metadata"]["name"] = f"bare-extra-{i}"
        pods.append(c)
    out = injection_status(
        ns_ctx(
            "rw-istio",
            overrides={"pods": pods, "deployments.apps": deployments, "replicasets.apps": rs},
        )
    )
    bare = workload(out, "bare")
    assert bare["podsWithoutSidecar"] == 71 and len(bare["podsMissingSidecar"]) == 50
    assert bare["truncated"] is True


def test_forbidden_namespace_read_still_reports_workloads_and_leaves_enabled_unknown():
    ctx = ns_ctx("rw-istio", fail={"namespaces": FORBIDDEN % "namespaces"})
    out = injection_status(ctx)
    check_outputs("injection-status", out)
    assert out["injection"]["enabled"] is None and out["injection"]["mode"] is None
    assert out["inspected"]["namespaceRead"] is False
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]
    bare = workload(out, "bare")
    assert bare["injectionExpected"] is None and bare["missingWhileExpected"] is None
    assert bare["podsWithoutSidecar"] == 1  # the pod facts survive
    # a template that decides still decides
    assert workload(out, "bare-optout")["injectionExpected"] is False


def test_forbidden_workload_lists_fail_and_never_return_empty():
    fail = {k: FORBIDDEN % k for k in ("deployments.apps", "statefulsets.apps", "daemonsets.apps")}
    with pytest.raises(RwError) as exc:
        injection_status(ns_ctx("rw-istio", fail=fail))
    assert exc.value.code == "permission_denied"


def test_one_forbidden_workload_list_is_partial():
    ctx = ns_ctx("rw-healthy", fail={"statefulsets.apps": FORBIDDEN % "statefulsets"})
    out = injection_status(ctx)
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "statefulsets")]
    assert out["inspected"]["statefulsets"] is None and out["inspected"]["deployments"] == len(
        load("rw-healthy_deployments")["items"]
    )
    assert {w["kind"] for w in out["workloads"]} == {"Deployment", "DaemonSet"}


def test_forbidden_pods_leave_the_pod_facts_null_but_keep_the_workloads():
    ctx = ns_ctx("rw-istio", fail={"pods": FORBIDDEN % "pods"})
    out = injection_status(ctx)
    check_outputs("injection-status", out)
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]
    bare = workload(out, "bare")
    assert (
        bare["pods"] is None
        and bare["podsWithoutSidecar"] is None
        and bare["missingWhileExpected"] is None
    )
    assert bare["injectionExpected"] is True and bare["sidecarInTemplate"] is False
    assert out["inspected"]["pods"] is None and out["inspected"]["podsOutsideWorkloads"] is None


def test_forbidden_replicasets_fall_back_to_the_selector():
    ctx = ns_ctx("rw-istio", fail={"replicasets.apps": FORBIDDEN % "replicasets"})
    out = injection_status(ctx)
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "replicasets")]
    assert workload(out, "bare")["podsWithoutSidecar"] == 1


def test_missing_namespace_is_not_found():
    with pytest.raises(RwError) as exc:
        injection_status(ns_ctx("nope"))
    assert exc.value.code == "not_found"


def test_unreachable_api_server_is_not_swallowed_by_the_scoped_reads():
    for noun in ("namespaces", "deployments.apps", "pods", "replicasets.apps"):
        with pytest.raises(RwError) as exc:
            injection_status(ns_ctx("rw-istio", fail={noun: UNREACHABLE}))
        assert exc.value.code == "unreachable", noun


# ------------------------------------------------------------ control-plane-status ----


def control(ctx):
    return script("control_plane_status")(ctx)


def test_istiod_reports_replicas_version_pods_and_webhooks():
    ctx = dep_ctx("istiod")
    out = control(ctx)
    check_outputs("control-plane-status", out)
    assert ctx.errors == []
    d = out["deployment"]
    assert (d["name"], d["replicas"], d["readyReplicas"], d["unavailableReplicas"]) == (
        "istiod",
        1,
        1,
        0,
    )
    assert {c["type"]: c["status"] for c in d["conditions"]} == {
        "Available": "True",
        "Progressing": "True",
    }
    assert out["version"] == {
        "container": "discovery",
        "source": "pods",
        "tags": [{"tag": "1.36", "count": 1}],
    }
    assert (
        len(out["pods"]) == 1
        and out["pods"][0]["ready"] is True
        and out["pods"][0]["restartCount"] == 0
    )
    hooks = {w["kind"]: w for w in out["webhooks"]}
    assert set(hooks) == {"mutating", "validating"}
    m = hooks["mutating"]
    assert m["configuration"] == "istio-sidecar-injector" and m["failurePolicy"] == "Ignore"
    assert m["service"] == {
        "namespace": "rw-istio",
        "name": "istiod",
        "port": 443,
        "path": "/inject",
    }
    assert m["pointsAtTarget"] is True and m["caBundlePresent"] is False
    assert m["endpoints"] == {"ready": 1, "notReady": 0}
    assert hooks["validating"]["configuration"] == "istio-validator-rw-istio"
    assert (
        out["inspected"]["mutatingConfigurations"] == 1
        and out["inspected"]["validatingConfigurations"] == 1
    )


def test_broken_gateway_shows_unavailable_replicas_waiting_container_and_version_from_the_pod():
    out = control(dep_ctx("istio-ingressgateway"))
    check_outputs("control-plane-status", out)
    d = out["deployment"]
    assert d["readyReplicas"] == 0 and d["unavailableReplicas"] == 1
    p = out["pods"][0]
    assert p["ready"] is False
    c = p["containers"][0]
    assert (c["name"], c["state"], c["ready"]) == ("istio-proxy", "waiting", False)
    assert c["reason"] in ("ErrImagePull", "ImagePullBackOff")
    assert out["version"]["container"] == "istio-proxy"
    assert out["version"]["tags"] == [{"tag": "does-not-exist-1.20.3", "count": 1}]
    # the webhooks are those of istiod: none points at this Deployment
    assert out["webhooks"] and all(w["pointsAtTarget"] is False for w in out["webhooks"])


def test_webhooks_can_be_left_out_and_then_no_cluster_scoped_call_is_made():
    ctx = dep_ctx("istio-ingressgateway")
    out = script("control_plane_status")(ctx, include_webhooks=False)
    check_outputs("control-plane-status", out)
    assert out["webhooks"] is None and out["inspected"]["mutatingConfigurations"] is None
    assert not any("webhookconfigurations" in " ".join(a) for a in ctx.calls)


def test_forbidden_webhook_configurations_keep_the_deployment_and_report_each_scope():
    fail = {
        "mutatingwebhookconfigurations.admissionregistration.k8s.io": FORBIDDEN
        % "mutatingwebhookconfigurations",
        "validatingwebhookconfigurations.admissionregistration.k8s.io": FORBIDDEN
        % "validatingwebhookconfigurations",
    }
    ctx = dep_ctx("istiod", fail=fail)
    out = control(ctx)
    check_outputs("control-plane-status", out)
    assert out["webhooks"] is None and out["deployment"]["readyReplicas"] == 1 and out["pods"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "mutatingwebhookconfigurations"),
        ("permission_denied", "validatingwebhookconfigurations"),
    ]


def test_one_forbidden_webhook_kind_leaves_the_other_with_counts_saying_which():
    fail = {
        "validatingwebhookconfigurations.admissionregistration.k8s.io": FORBIDDEN
        % "validatingwebhookconfigurations"
    }
    ctx = dep_ctx("istiod", fail=fail)
    out = control(ctx)
    assert {w["kind"] for w in out["webhooks"]} == {"mutating"}
    assert (
        out["inspected"]["mutatingConfigurations"] == 1
        and out["inspected"]["validatingConfigurations"] is None
    )


def test_forbidden_endpointslices_leave_endpoints_null_and_are_reported_once():
    ctx = dep_ctx("istiod", fail={"endpointslices.discovery.k8s.io": FORBIDDEN % "endpointslices"})
    out = control(ctx)
    check_outputs("control-plane-status", out)
    assert all(w["endpoints"] is None for w in out["webhooks"])
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "endpointslices")
    ]


def test_webhook_service_in_another_namespace_is_not_looked_up():
    mutating = copy.deepcopy(load("mutatingwebhookconfigurations")["items"])
    mutating[0]["webhooks"][0]["clientConfig"]["service"]["namespace"] = "istio-system"
    ctx = dep_ctx(
        "istiod", overrides={"mutatingwebhookconfigurations.admissionregistration.k8s.io": mutating}
    )
    out = control(ctx)
    m = next(w for w in out["webhooks"] if w["kind"] == "mutating")
    assert m["endpoints"] is None and m["pointsAtTarget"] is False
    v = next(w for w in out["webhooks"] if w["kind"] == "validating")
    assert v["endpoints"] == {"ready": 1, "notReady": 0}
    # one EndpointSlice read, for the in-namespace Service
    assert sum("endpointslices.discovery.k8s.io" in " ".join(a) for a in ctx.calls) == 1


def test_a_webhook_service_with_no_ready_endpoints_is_a_fact():
    slices = copy.deepcopy(load("rw-istio_endpointslices_istiod")["items"])
    slices[0]["endpoints"][0]["conditions"]["ready"] = False
    out = control(dep_ctx("istiod", overrides={"endpointslices.discovery.k8s.io": slices}))
    assert all(w["endpoints"] == {"ready": 0, "notReady": 1} for w in out["webhooks"])
    out = control(dep_ctx("istiod", overrides={"endpointslices.discovery.k8s.io": []}))
    assert all(w["endpoints"] == {"ready": 0, "notReady": 0} for w in out["webhooks"])


def test_webhook_configurations_without_an_istio_name_are_not_listed_but_are_counted():
    mutating = copy.deepcopy(load("mutatingwebhookconfigurations")["items"])
    other = copy.deepcopy(mutating[0])
    other["metadata"]["name"] = "cert-manager-webhook"
    revision = copy.deepcopy(mutating[0])
    revision["metadata"]["name"] = "istio-sidecar-injector-1-20"
    revision["webhooks"][0]["clientConfig"]["caBundle"] = "Zm9v"
    ctx = dep_ctx(
        "istiod",
        overrides={
            "mutatingwebhookconfigurations.admissionregistration.k8s.io": mutating
            + [other, revision]
        },
    )
    out = control(ctx)
    names = [w["configuration"] for w in out["webhooks"] if w["kind"] == "mutating"]
    assert names == ["istio-sidecar-injector", "istio-sidecar-injector-1-20"]
    assert out["inspected"]["mutatingConfigurations"] == 3
    assert (
        next(w for w in out["webhooks"] if w["configuration"].endswith("1-20"))["caBundlePresent"]
        is True
    )


def test_forbidden_pods_keep_the_replica_counts_and_take_the_version_from_the_template():
    ctx = dep_ctx("istiod", fail={"pods": FORBIDDEN % "pods"})
    out = control(ctx)
    check_outputs("control-plane-status", out)
    assert out["pods"] is None and out["inspected"]["pods"] is None
    assert out["deployment"]["readyReplicas"] == 1
    assert out["version"]["source"] == "template" and out["version"]["tags"][0]["tag"] == "1.36"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "pods")]


def test_restarts_and_the_last_termination_reason_are_per_container():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    p = next(x for x in pods if x["metadata"]["labels"]["app"] == "istiod")
    cs = p["status"]["containerStatuses"][0]
    cs["restartCount"] = 5
    cs["lastState"] = {
        "terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-03T20:00:00Z"}
    }
    out = control(dep_ctx("istiod", overrides={"pods": pods}))
    row = out["pods"][0]
    assert row["restartCount"] == 5
    assert row["containers"][0]["lastTerminated"] == {
        "reason": "OOMKilled",
        "exitCode": 137,
        "finishedAt": "2026-11-03T20:00:00Z",
    }


def test_native_sidecar_restarts_count_and_are_marked_init():
    p = pod("g-1", containers=("app",), init=("istio-proxy",))
    p["status"] = {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [
            {"name": "app", "ready": True, "restartCount": 0, "state": {"running": {}}}
        ],
        "initContainerStatuses": [
            {"name": "istio-proxy", "ready": True, "restartCount": 3, "state": {"running": {}}}
        ],
    }
    view = control_lib.pod_view(p)
    assert view["restartCount"] == 3
    assert [(c["name"], c["init"], c["restartCount"]) for c in view["containers"]] == [
        ("app", False, 0),
        ("istio-proxy", True, 3),
    ]


def test_version_reads_tags_digests_and_registry_ports():
    assert control_lib.parse_image("docker.io/istio/pilot:1.20.3") == ("1.20.3", False)
    assert control_lib.parse_image("localhost:5000/istio/proxyv2:1.20.3-distroless") == (
        "1.20.3-distroless",
        False,
    )
    assert control_lib.parse_image("localhost:5000/istio/proxyv2") == (None, False)
    assert control_lib.parse_image("istio/proxyv2:1.20.3@sha256:abcd") == ("1.20.3", True)
    assert control_lib.parse_image("auto") == (None, False)
    assert control_lib.parse_image(None) == (None, False)


def test_two_versions_during_a_rollout_are_both_listed():
    pods = [pod(f"p{i}", containers=("discovery",)) for i in range(3)]
    pods[0]["spec"]["containers"][0]["image"] = "docker.io/istio/pilot:1.19.0"
    for p in pods[1:]:
        p["spec"]["containers"][0]["image"] = "docker.io/istio/pilot:1.20.3"
    v = control_lib.versions({"spec": {}}, pods)
    assert v == {
        "container": "discovery",
        "source": "pods",
        "tags": [{"tag": "1.20.3", "count": 2}, {"tag": "1.19.0", "count": 1}],
    }
    assert control_lib.versions({"spec": {}}, []) == {
        "container": None,
        "source": "template",
        "tags": [],
    }


def test_gateway_placeholder_image_is_not_a_version():
    template = {
        "spec": {"template": {"spec": {"containers": [{"name": "istio-proxy", "image": "auto"}]}}}
    }
    assert control_lib.versions(template, None)["tags"] == [{"tag": None, "count": 1}]


def test_pod_and_condition_text_is_bounded():
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    named({"items": deployments}, "istiod")["status"]["conditions"][0]["message"] = "m" * 5000
    out = control(dep_ctx("istiod", overrides={"deployments.apps": deployments}))
    cond = out["deployment"]["conditions"][0]
    assert len(cond["message"]) == 1024 and cond["truncated"] is True


def test_control_missing_and_forbidden_deployment_fail():
    with pytest.raises(RwError) as exc:
        control(dep_ctx("nope"))
    assert exc.value.code == "not_found"
    with pytest.raises(RwError) as exc:
        control(dep_ctx("istiod", fail={"deployments.apps": FORBIDDEN % "deployments"}))
    assert exc.value.code == "permission_denied"


def test_unreachable_is_not_swallowed_by_the_scoped_reads():
    for noun in (
        "deployments.apps",
        "pods",
        "replicasets.apps",
        "mutatingwebhookconfigurations.admissionregistration.k8s.io",
        "endpointslices.discovery.k8s.io",
    ):
        with pytest.raises(RwError) as exc:
            control(dep_ctx("istiod", fail={noun: UNREACHABLE}))
        assert exc.value.code == "unreachable", noun


# ------------------------------------------------------------ proxy-errors ----


def proxy_errors(ctx, **kw):
    return script("proxy_errors")(ctx, **kw)


def test_noisy_proxy_counts_levels_5xx_and_response_flags():
    ctx = dep_ctx("meshed")
    out = proxy_errors(ctx)
    check_outputs("proxy-errors", out)
    assert ctx.errors == [] and out["unavailable"] == []
    (row,) = out["proxies"]
    assert row["container"] == "istio-proxy" and row["native"] is False
    assert (row["errorLines"], row["warnLines"], row["accessLines"], row["status5xx"]) == (
        1,
        1,
        4,
        3,
    )
    assert {f["flag"]: f["count"] for f in row["responseFlags"]} == {"UF": 1, "NR": 1, "URX": 1}
    cats = {s["category"] for s in row["samples"]}
    assert cats == {"error", "warn", "http5xx"}
    assert row["truncated"] is False and row["bytesCut"] is False
    assert out["inspected"]["pods"] == 1 and out["inspected"]["podsRead"] == 1
    call = next(a for a in ctx.calls if "logs" in a)
    assert call[:4] == ["kubectl", "-n", "rw-istio", "logs"] and "--since=3600s" in call
    assert "-c" in call and call[call.index("-c") + 1] == "istio-proxy" and "--previous" not in call


def test_quiet_proxy_has_zero_counts_and_still_reads_the_access_log():
    out = proxy_errors(dep_ctx("meshed-quiet"))
    (row,) = out["proxies"]
    assert (row["errorLines"], row["warnLines"], row["status5xx"], row["responseFlags"]) == (
        0,
        0,
        0,
        [],
    )
    assert row["accessLines"] > 0 and row["samples"] == []


def test_pods_without_a_sidecar_are_listed_as_unavailable_not_as_clean():
    out = proxy_errors(dep_ctx("bare"))
    check_outputs("proxy-errors", out)
    assert out["proxies"] == []
    assert [(u["container"], u["reason"]) for u in out["unavailable"]] == [
        ("istio-proxy", "pod has no istio-proxy container")
    ]


def test_proxy_that_has_not_started_is_unavailable_with_its_waiting_reason():
    ctx = dep_ctx("istio-ingressgateway")
    out = proxy_errors(ctx)
    assert out["proxies"] == []
    assert out["unavailable"][0]["reason"].startswith("container is waiting: ")
    assert not any("logs" in a for a in ctx.calls)


def test_samples_are_redacted_and_bounded():
    long_line = "2026-11-03T20:00:00Z\terror\tx\t" + "y" * 3000
    text = "\n".join(
        [
            '[2026-11-03T20:00:04Z] "GET /x?token=abc123 HTTP/1.1" 503 UF - - "-" 0 0 1 - "-" "c" "r" "h" "-" - - - - -',
            "2026-11-03T20:00:00Z\twarning\tx\tAuthorization: Bearer abc.def.ghi",
            long_line,
        ]
    )
    out = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))
    row = out["proxies"][0]
    joined = json.dumps(row["samples"])
    assert "abc123" not in joined and "abc.def.ghi" not in joined and "[REDACTED]" in joined
    long = next(s for s in row["samples"] if s["category"] == "error")
    assert len(long["line"]) <= 1024 and long["truncated"] is True


def test_samples_are_limited_per_category():
    text = "\n".join(f"2026-11-03T20:00:{i:02d}Z\terror\tx\tboom {i}" for i in range(10))
    row = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))["proxies"][0]
    assert row["errorLines"] == 10 and len(row["samples"]) == 3


def test_an_info_line_that_mentions_an_error_is_not_counted():
    text = "2026-11-03T20:00:00Z\tinfo\tads\tno error budget left\n[2026-11-03 20:00:00.000][1][info][main] error handling on\n"
    row = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))["proxies"][0]
    assert row["errorLines"] == 0 and row["warnLines"] == 0 and row["linesRead"] == 2


def test_other_log_formats_are_understood():
    assert proxy.classify_level("[2026-11-03 20:00:00.000][14][warning][config] x") == "warn"
    assert proxy.classify_level('{"level":"error","msg":"x"}') == "error"
    assert proxy.classify_level("2026-11-03T20:00:00.000000Z\tcritical\tx\ty") == "error"
    assert proxy.classify_level("2026-11-03T20:00:00.000000Z\tinfo\tx\ty") is None
    json_line = '{"response_code":503,"response_flags":"UF,URX","path":"/x"}'
    assert proxy.parse_access(json_line) == (503, ["UF", "URX"])
    assert proxy.parse_access('{"response_code":"200","response_flags":"-"}') == (200, [])
    assert proxy.parse_access('[t] "- - -" 0 - - - "-" 5 5 1 - ...') == (0, [])
    assert proxy.parse_access('[t] "GET / HTTP/1.1" 503 UF,URX x') == (503, ["UF", "URX"])
    assert proxy.parse_access("plain text") is None


def test_a_flagged_request_that_is_not_a_5xx_is_sampled_apart():
    text = '[t] "GET / HTTP/1.1" 0 DC downstream_remote_disconnect - "-" 0 0 1 - "-" "c" "r" "h" "-" - - - - -'
    row = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))["proxies"][0]
    assert row["status5xx"] == 0 and [f["flag"] for f in row["responseFlags"]] == ["DC"]
    assert [s["category"] for s in row["samples"]] == ["flagged"]


def test_reaching_max_scan_lines_marks_the_pod_truncated():
    row = proxy_errors(dep_ctx("meshed", settings={"max_scan_lines": 5}))["proxies"][0]
    assert row["truncated"] is True and row["linesRead"] == 7
    ctx = dep_ctx("meshed", settings={"max_scan_lines": 5000})
    assert proxy_errors(ctx)["proxies"][0]["truncated"] is False
    call = next(a for a in ctx.calls if "logs" in a)
    assert "--tail=5000" in call


def test_since_is_validated_before_any_kubectl_call():
    ctx = dep_ctx("meshed")
    with pytest.raises(RwError) as exc:
        proxy_errors(ctx, since="soon")
    assert exc.value.code == "invalid_input" and ctx.calls == []
    row_ctx = dep_ctx("meshed")
    proxy_errors(row_ctx, since="90m")
    assert "--since=5400s" in next(a for a in row_ctx.calls if "logs" in a)


def many_pods(n):
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    rs = copy.deepcopy(load("rw-istio_replicasets")["items"])
    base = next(p for p in pods if p["metadata"]["labels"]["app"] == "meshed")
    out = [base]
    for i in range(1, n):
        c = copy.deepcopy(base)
        c["metadata"]["name"] = f"meshed-extra-{i}"
        c["metadata"]["uid"] = f"extra-{i}"
        c["status"]["containerStatuses"][1]["restartCount"] = (
            i  # istio-proxy is the second container
        )
        out.append(c)
    return [p for p in pods if p["metadata"]["labels"]["app"] != "meshed"] + out, rs


def test_max_pods_caps_the_reads_most_restarts_first_and_counts_the_rest():
    pods, rs = many_pods(5)
    ctx = dep_ctx(
        "meshed", overrides={"pods": pods, "replicasets.apps": rs}, settings={"max_pods": 2}
    )
    out = proxy_errors(ctx)
    assert out["inspected"]["pods"] == 5 and out["inspected"]["podsRead"] == 2
    assert out["inspected"]["podsNotRead"] == 3
    assert {r["pod"] for r in out["proxies"]} == {"meshed-extra-4", "meshed-extra-3"}


def test_proxies_are_sorted_by_failed_requests_then_errors():
    rows = [
        {"pod": "a", "status5xx": 0, "errorLines": 9, "warnLines": 0},
        {"pod": "b", "status5xx": 2, "errorLines": 0, "warnLines": 0},
        {"pod": "c", "status5xx": 2, "errorLines": 1, "warnLines": 0},
    ]
    assert [r["pod"] for r in proxy.sort_results(rows)] == ["c", "b", "a"]


def test_proxy_results_are_capped_by_max_items():
    pods, rs = many_pods(4)
    out = proxy_errors(
        dep_ctx(
            "meshed", overrides={"pods": pods, "replicasets.apps": rs}, settings={"max_items": 1}
        )
    )
    assert len(out["proxies"]) == 1 and out["inspected"]["total"]["proxies"] == 4
    assert out["inspected"]["omitted"]["proxies"] == 3


def test_when_no_log_can_be_read_the_task_fails_with_the_cause():
    with pytest.raises(RwError) as exc:
        proxy_errors(dep_ctx("meshed", fail={"logs": FORBIDDEN % "pods/log"}))
    assert exc.value.code == "permission_denied"


def test_one_unreadable_log_is_an_error_and_the_others_are_returned():
    pods, rs = many_pods(2)

    class Ctx(FakeCtx):
        def _logs(self, argv, args, check):
            if args[args.index("logs") + 1] == "meshed-extra-1":
                return self._done(argv, 1, "", FORBIDDEN % "pods/log", check)
            return super()._logs(argv, args, check)

    ctx = Ctx("rw-istio", "meshed", DEP, overrides={"pods": pods, "replicasets.apps": rs})
    out = proxy_errors(ctx)
    assert [r["pod"] for r in out["proxies"]] == ["meshed-9f4c5b8c7-tbg87"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "meshed-extra-1")
    ]
    assert all(len(m) <= 1100 for m in ctx.messages)


def test_a_log_call_that_times_out_is_a_timeout_error():
    ctx = dep_ctx("meshed", logs={"meshed": subprocess.TimeoutExpired(["kubectl"], 20)})
    with pytest.raises(RwError) as exc:
        proxy_errors(ctx)
    assert exc.value.code == "timeout"


def test_a_log_file_the_node_no_longer_has_is_unavailable_not_empty():
    text = "unable to retrieve container logs for containerd://abc"
    out = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))
    assert out["proxies"] == []
    assert out["unavailable"][0]["reason"] == "container log file no longer exists on the node"


def test_a_deployment_without_pods_is_an_empty_result_with_zero_pods():
    out = proxy_errors(dep_ctx("meshed", overrides={"pods": []}))
    assert out["proxies"] == [] and out["inspected"]["pods"] == 0


def test_proxy_missing_deployment_and_forbidden_pods_fail():
    with pytest.raises(RwError) as exc:
        proxy_errors(dep_ctx("nope"))
    assert exc.value.code == "not_found"
    with pytest.raises(RwError) as exc:
        proxy_errors(dep_ctx("meshed", fail={"pods": FORBIDDEN % "pods"}))
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        proxy_errors(dep_ctx("meshed", fail={"replicasets.apps": UNREACHABLE}))
    assert exc.value.code == "unreachable"


# ------------------------------------------------------------ shared ----


def test_other_resource_types_are_refused_before_any_kubectl_call():
    for name, rtype in (
        ("injection_status", DEP),
        ("control_plane_status", NS),
        ("proxy_errors", NS),
        ("proxy_errors", "kubernetes/statefulset"),
    ):
        ctx = FakeCtx("rw-istio", "x", rtype)
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_type_falls_back_to_the_path_plural():
    ctx = FakeCtx("rw-istio", "istiod", None, input_ns="rw-istio")
    ctx.resource.path = "kubernetes/clusters/kind/namespaces/rw-istio/deployments/istiod"
    assert control(ctx)["deployment"]["name"] == "istiod"
    ctx = FakeCtx(None, "rw-istio", None, input_ns="rw-istio")
    ctx.resource.path = "kubernetes/clusters/kind/namespaces/rw-istio"
    assert injection_status(ctx)["injection"]["mode"] == "label"


def test_bad_settings_fail_as_invalid_input_before_any_call():
    for name, kw, setting in (
        ("injection_status", {"max_items": 0}, None),
        ("control_plane_status", {"max_items": "x"}, None),
        ("proxy_errors", {"max_pods": 0}, None),
        ("proxy_errors", {"max_scan_lines": True}, None),
    ):
        ctx = FakeCtx("rw-istio", "meshed", NS if name == "injection_status" else DEP, settings=kw)
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_no_call_reads_a_secret_or_an_environment_and_json_reads_are_json():
    runs = [
        (injection_status, ns_ctx("rw-istio")),
        (control, dep_ctx("istiod")),
        (proxy_errors, dep_ctx("meshed")),
    ]
    for fn, ctx in runs:
        fn(ctx)
        for argv in ctx.calls:
            assert not any("secret" in a.lower() for a in argv), argv
            if "logs" not in argv:
                assert argv[-2:] == ["-o", "json"]


def test_outputs_never_carry_environment_values_or_commands():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    for p in pods:
        for c in p["spec"]["containers"]:
            c["env"] = [{"name": "K", "value": "hunter2-sentinel"}]
            c["command"] = ["sh", "-c", "hunter2-sentinel"]
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    outs = [
        injection_status(ns_ctx("rw-istio", overrides={"pods": pods})),
        control(dep_ctx("istiod", overrides={"pods": pods})),
        proxy_errors(dep_ctx("meshed", overrides={"pods": pods, "deployments.apps": deployments})),
    ]
    assert all("hunter2-sentinel" not in json.dumps(o) for o in outs)


# ------------------------------------------------------------ permissions ----

# kubectl noun -> (apiGroup, plural)
RESOURCES = {
    "namespaces": ("", "namespaces"),
    "deployments.apps": ("apps", "deployments"),
    "statefulsets.apps": ("apps", "statefulsets"),
    "daemonsets.apps": ("apps", "daemonsets"),
    "replicasets.apps": ("apps", "replicasets"),
    "pods": ("", "pods"),
    "mutatingwebhookconfigurations.admissionregistration.k8s.io": (
        "admissionregistration.k8s.io",
        "mutatingwebhookconfigurations",
    ),
    "validatingwebhookconfigurations.admissionregistration.k8s.io": (
        "admissionregistration.k8s.io",
        "validatingwebhookconfigurations",
    ),
    "endpointslices.discovery.k8s.io": ("discovery.k8s.io", "endpointslices"),
}
VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-o",
    "-c",
    "--container",
    "--field-selector",
    "-l",
    "--selector",
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list;
    logs -> pods get + pods/log get. Raises on anything else, so a skill that issues a new
    verb has to extend this mapping."""
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a == "--":
            break
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
        return {(group, plural, "get" if named_object else "list")}
    if verb == "logs":
        return {("", "pods", "get"), ("", "pods/log", "get")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def every_path(task):
    """FakeCtx runs of the task that together reach each kubectl call it can make."""
    if task == "injection-status":
        return [(injection_status, ns_ctx(n)) for n in ("rw-istio", "rw-healthy")]
    if task == "control-plane-status":
        return [(control, dep_ctx("istiod")), (control, dep_ctx("istio-ingressgateway"))]
    return [(proxy_errors, dep_ctx("meshed")), (proxy_errors, dep_ctx("bare"))]


@pytest.mark.parametrize("task", sorted(TASKS))
def test_declared_permissions_are_exactly_what_the_script_calls(task):
    needed = set()
    for fn, ctx in every_path(task):
        fn(ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    assert needed - declared(TASKS[task]) == set(), f"{task} calls what it does not declare"
    assert declared(TASKS[task]) - needed == set(), f"{task} declares what it never calls"


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "get", "namespaces", "x", "-o", "json"]) == {
        ("", "namespaces", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "logs", "p", "-c", "istio-proxy"]) == {
        ("", "pods", "get"),
        ("", "pods/log", "get"),
    }


# ------------------------------------------------------------ k8s.py / errors ----


def test_classify_maps_stderr_to_d64_codes():
    cases = {
        (1, "Error from server (Forbidden): x"): "permission_denied",
        (1, 'Error from server (NotFound): x "y" not found'): "not_found",
        (1, "Unable to connect to the server: connection refused"): "unreachable",
        (1, "context deadline exceeded"): "timeout",
        (1, "Too Many Requests"): "rate_limited",
        (1, 'error: the server doesn\'t have a resource type "orders"'): "dependency",
        (127, ""): "dependency",
        (124, ""): "timeout",
        (1, "something odd"): "internal",
    }
    for (rc, text), code in cases.items():
        assert k8s.classify(rc, text) == code


def test_a_ctx_run_that_returns_nonzero_is_checked_too():
    class Legacy(FakeCtx):
        def run(self, argv, check=True, timeout=None):
            return super().run(argv, check=False, timeout=timeout)

    with pytest.raises(RwError) as exc:
        injection_status(
            Legacy(
                None,
                "rw-istio",
                NS,
                input_ns="rw-istio",
                fail={
                    "deployments.apps": FORBIDDEN % "d",
                    "statefulsets.apps": FORBIDDEN % "s",
                    "daemonsets.apps": FORBIDDEN % "x",
                },
            )
        )
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        control(Legacy("rw-istio", "nope", DEP, input_ns="rw-istio"))
    assert exc.value.code == "not_found"
    with pytest.raises(RwError) as exc:
        proxy_errors(
            Legacy("rw-istio", "meshed", DEP, input_ns="rw-istio", fail={"logs": FORBIDDEN % "x"})
        )
    assert exc.value.code == "permission_denied"


def test_kubectl_output_that_is_not_json_is_an_internal_error():
    class Ctx:
        def run(self, argv, check=True, timeout=None):
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "internal"


def test_ctx_error_text_is_bounded():
    ctx = dep_ctx("istiod", fail={"pods": FORBIDDEN % ("x" * 4000)})
    control(ctx)
    assert ctx.messages and all(len(m) <= 1100 for m in ctx.messages)


def test_durations_accept_unit_combinations_and_reject_the_rest():
    assert common.since_seconds("1h30m") == 5400 and common.since_seconds("500ms") == 1
    assert common.since_seconds("2d") == 172800
    for bad in ("", "1x", "h", "-5m", "1h 30"):
        with pytest.raises(RwError) as exc:
            common.since_seconds(bad)
        assert exc.value.code == "invalid_input"


# ------------------------------------------------------------ review fixes ----

import pytest as _pytest  # noqa: E402

PROBES = {
    "json authorization": ('{"authorization":"Bearer sekret1","a":1}', "sekret1"),
    "json authorization no scheme": ('{"authorization": "sekret1b"}', "sekret1b"),
    "bare bearer": ("calling upstream with Bearer sekret2 failed", "sekret2"),
    "bearer one char": ("Bearer x", "Bearer x"),
    "cookie header": ("Cookie: sid=sekret3; other=1", "sekret3"),
    "set-cookie header": ("Set-Cookie: sid=sekret3b; Path=/; HttpOnly", "sekret3b"),
    "json cookie": ('{"cookie":"sid=sekret3c; a=b","x":1}', "sekret3c"),
    "query code": ("GET /cb?code=sekret4&state=ok done", "sekret4"),
    "amz signature": ("PUT /b?X-Amz-Signature=sekret5&X-Amz-Date=1 x", "sekret5"),
    "jwt": ("token is eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig-nature_x here", "eyJhbGci"),
    "jwt empty signature": ("jwt eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0. end", "eyJhbGci"),
    "url userinfo": ("dial postgres://dbuser:sekret6@db.rw:5432/x failed", "sekret6"),
    "url userinfo no scheme": ("proxy //dbuser:sekret6b@host:80 refused", "sekret6b"),
    "token pair (existing)": ("failed token=sekret7 retry", "sekret7"),
}


@_pytest.mark.parametrize("name", sorted(PROBES))
def test_redaction_probe(name):
    line, secret = PROBES[name]
    out = common.redact(line)
    assert secret not in out, out
    clipped, _ = common.clip(line)
    assert secret not in clipped


def test_redaction_keeps_the_rest_of_the_line_readable():
    out = common.redact('{"authorization":"Bearer sekret1","status":503}')
    assert '"status":503' in out and "[REDACTED]" in out
    assert common.redact("plain basic auth discussion") == "plain basic auth discussion"


def test_access_log_samples_drop_the_query_string_of_the_request_path():
    text = (
        '[2026-11-03T20:00:04Z] "GET /order?id=17&x=1 HTTP/1.1" 503 UF - - "-" 0 0 1 - "-" "c" "r" "h" "-" - - - - -\n'
        '{"response_code":503,"response_flags":"UF","path":"/pay?session=abc&z=1","method":"GET"}\n'
    )
    row = proxy_errors(dep_ctx("meshed", logs={"meshed": text}))["proxies"][0]
    joined = json.dumps(row["samples"])
    assert "/order" in joined and "/pay" in joined
    assert (
        "id=17" not in joined
        and "session=abc" not in joined
        and "?" not in joined.replace('"?', "")
    )


# ---- scan budget


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


def test_scan_budget_stops_reading_counts_the_rest_and_keeps_partial_rows(monkeypatch):
    pods, rs = many_pods(5)
    clock = FakeClock()
    monkeypatch.setattr(common, "time", clock)

    class Ctx(FakeCtx):
        def _logs(self, argv, args, check):
            clock.t += 6  # each log read takes 6 seconds
            return super()._logs(argv, args, check)

    ctx = Ctx(
        "rw-istio",
        "meshed",
        DEP,
        overrides={"pods": pods, "replicasets.apps": rs},
        settings={"scan_budget_seconds": 10},
    )
    out = proxy_errors(ctx)
    check_outputs("proxy-errors", out)
    assert len(out["proxies"]) == 2 and out["inspected"]["podsRead"] == 2
    assert out["inspected"]["podsNotRead"] == 3
    assert ("timeout", "scan") in [(e["code"], e["scope"]) for e in ctx.errors]


def test_scan_budget_not_exhausted_is_silent(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(common, "time", clock)
    ctx = dep_ctx("meshed")
    out = proxy_errors(ctx)
    assert ctx.errors == [] and out["inspected"]["podsNotRead"] == 0


def test_scan_budget_setting_is_declared_and_bounded():
    s = yaml.safe_load((SKILL / "tasks.yaml").read_text())["settings"]["scanBudgetSeconds"]
    assert s["default"] == 90 and s["min"] == 1
    assert s["default"] < 120  # the task timeout
    with _pytest.raises(RwError) as exc:
        proxy_errors(dep_ctx("meshed", settings={"scan_budget_seconds": 0}))
    assert exc.value.code == "invalid_input"


# ---- ambient, revision labels, never-inject namespaces


def ns_doc(name, labels):
    return {"metadata": {"name": name, "labels": labels}}


def test_namespace_reports_dataplane_mode_and_exclusion():
    ni = injection.namespace_injection
    assert (
        ni(ns_doc("a", {"istio.io/dataplane-mode": "ambient"}), "a")["dataplaneMode"] == "ambient"
    )
    for name in ("kube-system", "kube-public", "kube-node-lease", "local-path-storage"):
        assert ni(ns_doc(name, {"istio-injection": "enabled"}), name)["excluded"] is True
    assert ni(None, "kube-system")["excluded"] is True
    assert ni(ns_doc("a", {}), "a")["excluded"] is False


def test_ambient_namespace_does_not_expect_a_sidecar_unless_the_pod_asks():
    inj = injection.namespace_injection(ns_doc("a", {"istio.io/dataplane-mode": "ambient"}), "a")
    e = injection.injection_expected
    assert e(inj, None, None, False, dataplane="ambient") is False
    assert e(inj, "true", "label", False, dataplane="ambient") is True
    assert (
        e(inj, "true", "annotation", False, dataplane="ambient") is False
    )  # annotation: no webhook match
    both = injection.namespace_injection(
        ns_doc("a", {"istio.io/dataplane-mode": "ambient", "istio-injection": "enabled"}), "a"
    )
    assert e(both, None, None, False, dataplane="ambient") is False
    assert e(both, None, None, False, dataplane="none") is True  # the pod opted out of ambient


def test_ambient_in_the_script_reports_dataplane_mode_per_workload():
    ns = ns_doc("rw-istio", {"istio.io/dataplane-mode": "ambient"})
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    named({"items": deployments}, "bare")["spec"]["template"]["metadata"]["labels"][
        "istio.io/dataplane-mode"
    ] = "none"
    named({"items": deployments}, "bare-optout")["spec"]["template"]["metadata"]["labels"][
        "sidecar.istio.io/inject"
    ] = "true"
    out = injection_status(
        ns_ctx("rw-istio", overrides={"namespace": ns, "deployments.apps": deployments})
    )
    check_outputs("injection-status", out)
    assert out["injection"]["dataplaneMode"] == "ambient" and out["injection"]["enabled"] is False
    meshed = workload(out, "meshed")
    assert meshed["dataplaneMode"] == "ambient" and meshed["injectionExpected"] is False
    assert meshed["missingWhileExpected"] is False
    bare = workload(out, "bare")
    assert bare["dataplaneMode"] == "none"
    assert workload(out, "bare-optout")["injectionExpected"] is True


def test_template_revision_label_in_an_unlabelled_namespace_expects_injection():
    inj = injection.namespace_injection(ns_doc("a", {}), "a")
    e = injection.injection_expected
    assert e(inj, None, None, False, revision="1-20") is True
    off = injection.namespace_injection(ns_doc("a", {"istio-injection": "disabled"}), "a")
    assert e(off, None, None, False, revision="1-20") is False
    assert e(inj, "false", "label", False, revision="1-20") is False
    assert e(injection.namespace_injection(None, "a"), None, None, False, revision="1-20") is True


def test_template_revision_is_reported_and_drives_missing_while_expected():
    deployments = copy.deepcopy(load("rw-istio_deployments")["items"])
    named({"items": deployments}, "bare")["spec"]["template"]["metadata"]["labels"][
        "istio.io/rev"
    ] = "1-20"
    ns = ns_doc("rw-istio", {})
    out = injection_status(
        ns_ctx("rw-istio", overrides={"namespace": ns, "deployments.apps": deployments})
    )
    bare = workload(out, "bare")
    assert bare["templateRevision"] == "1-20" and bare["injectionExpected"] is True
    assert bare["missingWhileExpected"] is True
    assert workload(out, "istiod")["templateRevision"] is None
    assert workload(out, "istiod")["injectionExpected"] is False


def test_never_inject_namespace_expects_nothing_even_when_labelled():
    ns = ns_doc("kube-system", {"istio-injection": "enabled"})
    ctx = FakeCtx(None, "kube-system", NS, input_ns="rw-istio", overrides={"namespace": ns})
    out = injection_status(ctx)
    check_outputs("injection-status", out)
    assert out["injection"]["excluded"] is True
    assert all(
        w["injectionExpected"] is False and w["missingWhileExpected"] is False
        for w in out["workloads"]
    )


def test_never_inject_namespace_by_name_when_the_namespace_is_forbidden():
    ctx = FakeCtx(
        None,
        "kube-system",
        NS,
        input_ns="rw-istio",
        fail={"namespaces": FORBIDDEN % "namespaces"},
    )
    out = injection_status(ctx)
    assert out["injection"]["excluded"] is True and out["injection"]["enabled"] is None
    assert workload(out, "bare")["injectionExpected"] is False


# ---- a proxy that has not started, version and endpoint fallbacks


def test_pod_without_container_statuses_is_unavailable_and_no_log_is_requested():
    pods = copy.deepcopy(load("rw-istio_pods")["items"])
    meshed = next(p for p in pods if p["metadata"]["labels"]["app"] == "meshed")
    del meshed["status"]["containerStatuses"]
    ctx = dep_ctx("meshed", overrides={"pods": pods})
    out = proxy_errors(ctx)
    assert out["proxies"] == []
    assert [u["reason"] for u in out["unavailable"]] == ["container has not started"]
    assert not any("logs" in a for a in ctx.calls)


def test_native_sidecar_without_a_status_is_not_started_either():
    p = pod("g-1", containers=("app",), init=("istio-proxy",))
    p["status"] = {"containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}]}
    from k8s_istio_health.sidecar import sidecar_status

    assert sidecar_status(p, True) is None


def test_version_falls_back_to_the_template_when_there_are_no_pods():
    out = control(dep_ctx("istiod", overrides={"pods": []}))
    check_outputs("control-plane-status", out)
    assert out["pods"] == []
    assert out["version"] == {
        "container": "discovery",
        "source": "template",
        "tags": [{"tag": "1.36", "count": 1}],
    }


def test_endpoint_with_no_ready_condition_counts_as_ready():
    slices = copy.deepcopy(load("rw-istio_endpointslices_istiod")["items"])
    ep = slices[0]["endpoints"][0]
    ep["conditions"] = {}
    slices[0]["endpoints"].append({"addresses": ["10.0.0.9"], "conditions": {"ready": None}})
    slices[0]["endpoints"].append({"addresses": ["10.0.0.8"], "conditions": {"ready": False}})
    out = control(dep_ctx("istiod", overrides={"endpointslices.discovery.k8s.io": slices}))
    assert all(w["endpoints"] == {"ready": 2, "notReady": 1} for w in out["webhooks"])


# ---- SKILL.md content


def test_skill_md_covers_the_review_points():
    text = (SKILL / "SKILL.md").read_text()
    for needle in (
        "ambient",
        "istio.io/dataplane-mode",
        "istio-revision-tag-",
        "policy: disabled",
        "bytesCut",
        "kubectl top",
        "kubernetes:k8s-istio-health/proxy-errors",
        "excludeContainers",
        "Permissions per task",
    ):
        assert needle in text, needle
    assert len(text.splitlines()) < 150
