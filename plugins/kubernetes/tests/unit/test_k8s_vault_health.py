"""Unit tests for k8s-vault-health: lib/ and the thin scripts, against JSON captured from the
kind-rw-plugins scenario rw-vault (a dev-mode Vault that is active, a real Vault that is initialized
and sealed after a restart, a CSI provider DaemonSet that is ready and one whose probes fail). No
test calls a cluster: scripts run against a fake ctx that answers kubectl (including
`get --raw` through the pod proxy) from the captured files and records every call."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_vault_health import csi, k8s, project, vault
from k8s_vault_health import workload as w
from k8s_vault_health.budget import Budget
from k8s_vault_health.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-vault-health"
DATA = Path(__file__).parent / "data" / "k8s_vault_health"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


STS = load("sts_vault")
STS_SEALED = load("sts_sealed")
DS = load("ds_csi")
DS_BROKEN = load("ds_csi_broken")
PODS = load("pods")["items"]
EVENTS = load("events")["items"]
HEALTH = {
    "vault-0": load("health_active"),
    "vault-sealed-0": load("health_sealed"),
}
UNINITIALIZED = load("health_uninitialized")
REFERENCE_TIME = datetime(2026, 11, 3, 22, 45, 0, tzinfo=timezone.utc)

WORKLOADS = {
    ("kubernetes/statefulset", "vault"): ("statefulsets.apps", STS),
    ("kubernetes/statefulset", "vault-sealed"): ("statefulsets.apps", STS_SEALED),
    ("kubernetes/daemonset", "vault-csi-provider"): ("daemonsets.apps", DS),
    ("kubernetes/daemonset", "vault-csi-provider-broken"): ("daemonsets.apps", DS_BROKEN),
}


PROJECTIONS = {
    proj.template(many): proj
    for proj in (project.STATEFULSET_FIELDS, project.DAEMONSET_FIELDS, project.POD_FIELDS)
    for many in (False, True)
}


def _get(obj, path):
    for key in path.split("."):
        obj = (obj or {}).get(key)
    return obj


def render(obj, proj):
    """What kubectl prints for one object under a projection (the inverse of Projection.parse)."""
    lines = ["@"]
    for path in proj.strings:
        lines.append(f"s:{path}={_get(obj, path) or ''}")
    for path in proj.values:
        value = _get(obj, path)
        lines.append(
            f"j:{path}=" + ("" if value is None else json.dumps(value, separators=(",", ":")))
        )
    return "\n".join(lines) + "\n"


class FakeCommandError(RwError):
    """What the v13 runner raises for a non-zero exit when check is true."""

    def __init__(self, argv, returncode, stderr):
        super().__init__("internal", f"{argv[0]} exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


class FakeCtx:
    """What a script sees: resource, settings, now(), error(), run().

    `answers` are (substring of the joined argv, (rc, stdout, stderr)) checked first. `health` is
    {pod: document} for the pod proxy; a pod missing from it answers like a closed port.
    """

    def __init__(
        self,
        rtype="kubernetes/statefulset",
        name="vault",
        answers=(),
        pods=None,
        health=None,
        events=None,
        namespace_target=False,
    ):
        self.resource = SimpleNamespace(
            namespace=None if namespace_target else "rw-vault",
            name="rw-vault" if namespace_target else name,
            type="kubernetes/namespace" if namespace_target else rtype,
        )
        self.settings = SimpleNamespace(max_items=200)
        self.answers = list(answers)
        self.pods = copy.deepcopy(pods if pods is not None else PODS)
        self.health = copy.deepcopy(health if health is not None else HEALTH)
        self.events = copy.deepcopy(events if events is not None else EVENTS)
        self.calls = []
        self.errors = []
        self.timeouts = []

    def now(self):
        return REFERENCE_TIME

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message, "hint": hint})

    def lines(self):
        return [" ".join(c[1:]) for c in self.calls]

    def run(self, argv, check=True, **kwargs):
        """Like the v13 runner: a non-zero exit raises unless the caller passes check=False."""
        self.calls.append(argv)
        self.timeouts.append(kwargs.get("timeout"))
        line = " ".join(argv[1:])
        answer = next((a for needle, a in self.answers if needle in line), None)
        rc, out, err = answer if answer is not None else self.default(argv, line)
        if rc and check:
            raise FakeCommandError(argv, rc, err)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def projected(self, argv, objects):
        """kubectl get ... -o jsonpath=<a projection of the skill>: the objects rendered."""
        assert argv[argv.index("-o") + 1].startswith("jsonpath="), argv  # never the whole object
        proj = PROJECTIONS[argv[argv.index("-o") + 1][len("jsonpath=") :]]
        return 0, "".join(render(o, proj) for o in objects), ""

    def default(self, argv, line):
        if argv[1:3] == ["get", "--raw"]:
            pod = argv[3].split("/pods/")[1].split("/proxy")[0].split(":")[1]
            if pod in self.health:
                return 0, json.dumps(self.health[pod]), ""
            return (
                1,
                "",
                "Error from server (ServiceUnavailable): error trying to reach service: dial tcp "
                "10.244.0.1:8200: connect: connection refused",
            )
        if "get namespaces rw-vault" in line:
            return 0, json.dumps({"kind": "Namespace", "metadata": {"name": "rw-vault"}}), ""
        if "get statefulsets.apps -l" in line:
            return self.projected(argv, [STS, STS_SEALED])
        if "get daemonsets.apps -l" in line:
            return self.projected(argv, [DS, DS_BROKEN])
        for (rtype, name), (plural, doc) in WORKLOADS.items():
            if f"get {plural} {name} " in line + " ":
                return self.projected(argv, [doc])
        if "get pods" in line:
            return self.projected(argv, self.pods)
        if "get events" in line:
            return 0, json.dumps({"items": self.events}), ""
        raise AssertionError(f"unexpected call {argv}")


def script(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def check_outputs(task, out):
    """Every output validates against the schema its tasks.yaml entry names."""
    assert set(out) == set(TASKS[task]["outputs"])
    for key, value in out.items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        errors = list(Draft202012Validator(schema).iter_errors(value))
        assert not errors, (task, key, errors[:2])


def server(ctx, **inputs):
    out = script("server_status")(ctx, **inputs)
    check_outputs("server-status", out)
    return out


def csi_status(ctx, **inputs):
    out = script("csi_provider_status")(ctx, **inputs)
    check_outputs("csi-provider-status", out)
    return out


def by_pod(items):
    return {i["pod"]: i for i in items}


def proxy_calls(ctx):
    return [c for c in ctx.calls if c[1:3] == ["get", "--raw"]]


# ------------------------------------------------------- health document ----


def doc(**over):
    base = {
        "initialized": True,
        "sealed": False,
        "standby": False,
        "performance_standby": False,
        "replication_performance_mode": "disabled",
        "replication_dr_mode": "disabled",
        "version": "1.18.5",
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "over,state,code",
    [
        ({}, "active", 200),
        ({"standby": True}, "standby", 429),
        ({"standby": True, "performance_standby": True}, "performance_standby", 473),
        ({"replication_dr_mode": "secondary", "standby": True}, "dr_secondary", 472),
        ({"sealed": True}, "sealed", 503),
        ({"sealed": True, "standby": True}, "sealed", 503),  # a sealed Vault also says standby
        ({"initialized": False, "sealed": True, "standby": True}, "uninitialized", 501),
    ],
)
def test_the_health_codes_map_to_states_in_vaults_own_order(over, state, code):
    assert vault.health_state(doc(**over)) == (state, code)


def test_active_facts():
    facts = vault.health_facts(HEALTH["vault-0"])
    assert facts["state"] == "active" and facts["healthCode"] == 200
    assert (facts["initialized"], facts["sealed"], facts["standby"]) == (True, False, False)
    assert facts["version"] == "1.18.5" and facts["clusterName"].startswith("vault-cluster-")
    assert facts["replicationDrMode"] == "disabled" and facts["enterprise"] is False


def test_sealed_facts_have_no_cluster_name():
    facts = vault.health_facts(HEALTH["vault-sealed-0"])
    assert (facts["state"], facts["healthCode"], facts["sealed"]) == ("sealed", 503, True)
    assert facts["initialized"] is True
    assert facts["clusterName"] is None and facts["clusterId"] is None


def test_uninitialized_facts():
    facts = vault.health_facts(UNINITIALIZED)
    assert (facts["state"], facts["healthCode"]) == ("uninitialized", 501)
    assert facts["initialized"] is False


def test_parse_health_refuses_what_is_not_a_health_document():
    for bad in ("", "<html>", "[]", "{}", '{"initialized": "yes", "sealed": false}'):
        with pytest.raises(RwError) as exc:
            vault.parse_health(bad)
        assert exc.value.code == "internal"
    assert vault.parse_health(json.dumps(doc()))["initialized"] is True


def test_every_status_code_is_overridden_to_200_so_the_body_survives_the_proxy():
    params = dict(p.split("=") for p in vault.QUERY.split("&"))
    assert params == {
        "activecode": "200",
        "standbycode": "200",
        "performancestandbycode": "200",
        "drsecondarycode": "200",
        "sealedcode": "200",
        "uninitcode": "200",
        "removedcode": "200",  # 530: removed from the raft cluster; older Vaults ignore it
    }


def test_the_path_goes_through_the_pod_proxy_subresource():
    path = vault.health_path("rw-vault", "vault-0", "http", 8200)
    assert path.startswith(
        "/api/v1/namespaces/rw-vault/pods/http:vault-0:8200/proxy/v1/sys/health?"
    )
    assert vault.health_path("n", "p", "https", 8201).startswith(
        "/api/v1/namespaces/n/pods/https:p:8201/proxy/"
    )


@pytest.mark.parametrize(
    "scheme,port", [("ftp", 8200), ("http", 0), ("http", 70000), ("http", "8200"), ("http", True)]
)
def test_a_bad_scheme_or_port_is_invalid_input(scheme, port):
    with pytest.raises(RwError) as exc:
        vault.health_path("n", "p", scheme, port)
    assert exc.value.code == "invalid_input"


def test_statefulset_replicas_treat_a_count_the_api_omits_as_zero():
    sealed = vault.statefulset_view(STS_SEALED)
    assert (sealed["desired"], sealed["current"], sealed["ready"], sealed["available"]) == (
        1,
        1,
        0,
        0,
    )
    assert sealed["generationObserved"] is True and sealed["updateStrategy"] == "RollingUpdate"
    ok = vault.statefulset_view(STS)
    assert (ok["desired"], ok["ready"], ok["updated"]) == (1, 1, 1)
    stale = copy.deepcopy(STS)
    stale["status"]["observedGeneration"] = 0
    assert vault.statefulset_view(stale)["generationObserved"] is False


def pod_doc(name):
    return next(p for p in PODS if p["metadata"]["name"] == name)


def test_askable_is_running_with_an_address_and_not_terminating():
    pod = pod_doc("vault-sealed-0")
    assert vault.askable(pod)  # sealed: Running, not ready
    for change in (
        lambda p: p["status"].update(phase="Pending"),
        lambda p: p["status"].pop("podIP"),
        lambda p: p["metadata"].update(deletionTimestamp="2026-11-03T22:00:00Z"),
    ):
        p = copy.deepcopy(pod)
        change(p)
        assert not vault.askable(p)


def test_pods_sort_sealed_first():
    entries = [
        {"pod": "a", "ready": True, "health": {"state": "active"}},
        {"pod": "b", "ready": False, "health": None},
        {"pod": "c", "ready": False, "health": {"state": "sealed"}},
    ]
    assert [e["pod"] for e in vault.sort_entries(entries)] == ["c", "b", "a"]


# --------------------------------------------------------------- targets ----


def test_resolve_target_from_type_then_from_path():
    assert w.resolve_target("kubernetes/statefulset")[0] == "StatefulSet"
    assert w.resolve_target("kubernetes/daemonset")[0] == "DaemonSet"
    assert w.resolve_target("kubernetes/namespace")[0] == "Namespace"
    assert (
        w.resolve_target(None, "kubernetes/clusters/k/namespaces/n/daemonsets/x")[0] == "DaemonSet"
    )
    assert w.resolve_target(None, "kubernetes/clusters/k/namespaces/n")[0] == "Namespace"
    for bad in ("kubernetes/deployment", "kubernetes/pod"):
        with pytest.raises(RwError) as exc:
            w.resolve_target(bad)
        assert exc.value.code == "invalid_input"


def test_owned_pods_follow_selector_and_controller_ownership():
    names = lambda pods: [p["metadata"]["name"] for p in pods]  # noqa: E731
    assert names(w.owned_pods(STS, PODS)) == ["vault-0"]
    assert names(w.owned_pods(STS_SEALED, PODS)) == ["vault-sealed-0"]
    assert names(w.owned_pods(DS, PODS)) == ["vault-csi-provider-qb97m"]
    stray = copy.deepcopy(pod_doc("vault-0"))
    stray["metadata"]["name"] = "vault-stray"
    stray["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "uid": "x", "controller": True}]
    assert "vault-stray" not in names(w.owned_pods(STS, [*PODS, stray]))


def test_empty_selector_raises_rather_than_selecting_the_namespace():
    with pytest.raises(RwError) as exc:
        w.selector_string({})
    assert exc.value.code == "invalid_input"


# ---------------------------------------------------------- server-status ----


def test_a_namespace_target_asks_every_vault_pod_and_lists_the_sealed_one_first():
    ctx = FakeCtx(namespace_target=True)
    out = server(ctx)
    assert [p["pod"] for p in out["pods"]] == ["vault-sealed-0", "vault-0"]
    pods = by_pod(out["pods"])
    assert pods["vault-sealed-0"]["health"]["state"] == "sealed"
    assert pods["vault-sealed-0"]["health"]["sealed"] is True
    assert pods["vault-sealed-0"]["ready"] is False and pods["vault-sealed-0"]["queried"] is True
    assert pods["vault-sealed-0"]["containerRestarts"] == 1
    assert pods["vault-0"]["health"]["state"] == "active" and pods["vault-0"]["ready"] is True
    assert {s["name"]: s["ready"] for s in out["statefulsets"]} == {"vault": 1, "vault-sealed": 0}
    assert out["counts"] == {
        "pods": 2,
        "ready": 1,
        "queried": 2,
        "failed": 0,
        "byState": {"active": 1, "sealed": 1},
    }
    assert ctx.errors == []


def test_a_namespace_target_finds_the_statefulsets_by_the_helm_chart_label():
    ctx = FakeCtx(namespace_target=True)
    server(ctx)
    lines = ctx.lines()
    assert lines[0] == "get namespaces rw-vault -o json"
    assert "get statefulsets.apps -l app.kubernetes.io/name=vault -o json" in lines[1]


def test_a_statefulset_target_asks_only_its_own_pods():
    ctx = FakeCtx(name="vault-sealed")
    out = server(ctx)
    assert [p["pod"] for p in out["pods"]] == ["vault-sealed-0"]
    assert [s["name"] for s in out["statefulsets"]] == ["vault-sealed"]
    assert len(proxy_calls(ctx)) == 1
    assert not [line for line in ctx.lines() if "get namespaces" in line]


def test_vault_is_asked_through_the_pod_proxy_and_nothing_else():
    ctx = FakeCtx(namespace_target=True)
    server(ctx)
    [first, second] = proxy_calls(ctx)  # in StatefulSet order
    assert "/pods/http:vault-0:8200/proxy/v1/sys/health?" in first[3]
    assert "/pods/http:vault-sealed-0:8200/proxy/v1/sys/health?" in second[3]
    joined = " ".join(" ".join(c) for c in ctx.calls)
    for forbidden in ("exec", "port-forward", "secret", "VAULT_TOKEN", "-H", "token"):
        assert forbidden not in joined


def test_scheme_and_port_inputs_reach_the_proxy_path():
    ctx = FakeCtx()
    server(ctx, scheme="https", port=8201)
    assert "/pods/https:vault-0:8201/proxy/" in proxy_calls(ctx)[0][3]


def test_a_bad_scheme_or_port_fails_before_any_pod_is_asked():
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        server(ctx, scheme="gopher")
    assert exc.value.code == "invalid_input" and proxy_calls(ctx) == []


@pytest.mark.parametrize(
    "over,state,code",
    [
        ({"standby": True}, "standby", 429),
        ({"standby": True, "performance_standby": True}, "performance_standby", 473),
        ({"replication_dr_mode": "secondary", "standby": True}, "dr_secondary", 472),
        ({"initialized": False, "sealed": True}, "uninitialized", 501),
    ],
)
def test_standby_and_other_states_are_facts_not_errors(over, state, code):
    health = {"vault-0": {**HEALTH["vault-0"], **over}}
    ctx = FakeCtx(health=health)
    out = server(ctx)
    entry = out["pods"][0]
    assert entry["health"]["state"] == state and entry["health"]["healthCode"] == code
    assert entry["error"] is None and ctx.errors == []
    assert out["counts"]["byState"] == {state: 1}


def test_a_pod_that_is_not_running_is_listed_and_not_asked():
    pods = copy.deepcopy(PODS)
    for p in pods:
        if p["metadata"]["name"] == "vault-sealed-0":
            p["status"]["phase"] = "Pending"
            p["status"].pop("podIP", None)
    ctx = FakeCtx(namespace_target=True, pods=pods)
    out = server(ctx)
    pending = by_pod(out["pods"])["vault-sealed-0"]
    assert (pending["queried"], pending["health"], pending["error"]) == (False, None, None)
    assert pending["phase"] == "Pending"
    assert len(proxy_calls(ctx)) == 1


def test_a_workload_with_no_pods_lists_none():
    ctx = FakeCtx(pods=[])
    out = server(ctx)
    assert out["pods"] == [] and out["counts"]["pods"] == 0 and proxy_calls(ctx) == []


def test_one_pod_that_cannot_be_asked_makes_the_result_partial_and_keeps_the_rest():
    ctx = FakeCtx(namespace_target=True, health={"vault-0": HEALTH["vault-0"]})
    out = server(ctx)
    pods = by_pod(out["pods"])
    assert pods["vault-0"]["health"]["state"] == "active"
    assert pods["vault-sealed-0"]["health"] is None
    assert pods["vault-sealed-0"]["error"]["code"] == "unreachable"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "pod/vault-sealed-0")]
    assert out["counts"]["failed"] == 1 and out["counts"]["queried"] == 1


def test_no_pod_could_be_asked_fails_the_task_with_the_first_code():
    ctx = FakeCtx(namespace_target=True, health={})
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "unreachable" and ctx.errors == []


PROXY_FORBIDDEN = (
    'Error from server (Forbidden): pods "vault-0" is forbidden: User "u" cannot get resource '
    '"pods/proxy" in API group "" in the namespace "rw-vault"'
)


def test_forbidden_pod_proxy_is_permission_denied_not_an_empty_result():
    ctx = FakeCtx(answers=[("get --raw", (1, "", PROXY_FORBIDDEN))])
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "permission_denied"


def test_a_tls_listener_asked_over_http_names_the_scheme_to_use():
    ctx = FakeCtx(
        answers=[
            (
                "get --raw",
                (
                    1,
                    "",
                    "Error from server (BadRequest): Client sent an HTTP request to an HTTPS server.",
                ),
            )
        ]
    )
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "invalid_input" and "https" in exc.value.message


def test_a_plain_listener_asked_over_https_names_the_scheme_to_use():
    ctx = FakeCtx(
        answers=[
            (
                "get --raw",
                (
                    1,
                    "",
                    "Error from server (ServiceUnavailable): error trying to reach service: tls: "
                    "first record does not look like a TLS handshake",
                ),
            )
        ]
    )
    with pytest.raises(RwError) as exc:
        server(ctx, scheme="https")
    assert exc.value.code == "invalid_input" and "http" in exc.value.message


def test_a_body_that_is_not_vault_is_an_internal_error():
    ctx = FakeCtx(answers=[("get --raw", (0, "<html>nginx</html>", ""))])
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "internal"


def test_a_proxy_timeout_is_a_timeout_error():
    import subprocess

    from k8s_vault_health.fetch import vault_health

    def run(argv, check=True, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    with pytest.raises(RwError) as exc:
        vault_health(SimpleNamespace(run=run), "rw-vault", "vault-0", "http", 8200, Budget())
    assert exc.value.code == "timeout"


NOT_FOUND = 'Error from server (NotFound): statefulsets.apps "vault" not found'
FORBIDDEN = (
    'Error from server (Forbidden): statefulsets.apps "vault" is forbidden: User "u" cannot get '
    'resource "statefulsets" in API group "apps" in the namespace "rw-vault"'
)
UNREACHABLE = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"


@pytest.mark.parametrize("script_name", ["server_status", "csi_provider_status"])
@pytest.mark.parametrize(
    "stderr,code",
    [(NOT_FOUND, "not_found"), (FORBIDDEN, "permission_denied"), (UNREACHABLE, "unreachable")],
)
def test_a_failed_read_of_the_target_raises_with_its_code(script_name, stderr, code):
    rtype = "kubernetes/statefulset" if script_name == "server_status" else "kubernetes/daemonset"
    name = "vault" if script_name == "server_status" else "vault-csi-provider"
    ctx = FakeCtx(rtype, name, answers=[(f" {name} ", (1, "", stderr))])
    with pytest.raises(RwError) as exc:
        script(script_name)(ctx)
    assert exc.value.code == code and proxy_calls(ctx) == []


def test_the_wrong_kind_of_target_is_refused_before_any_kubectl_call():
    ctx = FakeCtx("kubernetes/daemonset", "vault-csi-provider")
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []
    ctx = FakeCtx("kubernetes/statefulset", "vault")
    with pytest.raises(RwError) as exc:
        csi_status(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []
    ctx = FakeCtx("kubernetes/deployment", "vault")
    with pytest.raises(RwError):
        server(ctx)
    assert ctx.calls == []


def test_a_namespace_without_vault_is_not_found_never_an_empty_result():
    ctx = FakeCtx(namespace_target=True, answers=[("get statefulsets.apps -l", (0, "", ""))])
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "not_found"
    ctx = FakeCtx(namespace_target=True, answers=[("get daemonsets.apps -l", (0, "", ""))])
    with pytest.raises(RwError) as exc:
        csi_status(ctx)
    assert exc.value.code == "not_found"


def test_a_deleted_namespace_is_not_found():
    ctx = FakeCtx(
        namespace_target=True,
        answers=[
            (
                "get namespaces",
                (1, "", 'Error from server (NotFound): namespaces "rw-vault" not found'),
            )
        ],
    )
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "not_found"


def test_a_namespace_that_may_not_be_read_is_reported_and_the_lists_are_still_tried():
    ctx = FakeCtx(
        namespace_target=True,
        answers=[
            (
                "get namespaces",
                (1, "", 'namespaces "rw-vault" is forbidden: cannot get resource "namespaces"'),
            )
        ],
    )
    out = server(ctx)
    assert len(out["pods"]) == 2
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]


def test_a_forbidden_statefulset_list_raises():
    ctx = FakeCtx(
        namespace_target=True,
        answers=[
            (
                "get statefulsets.apps -l",
                (1, "", 'statefulsets.apps is forbidden: cannot list resource "statefulsets"'),
            )
        ],
    )
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "permission_denied"


@pytest.mark.parametrize(
    "script_name,name", [("server_status", "vault"), ("csi_provider_status", "vault-csi-provider")]
)
def test_a_forbidden_pod_list_raises_and_never_returns_an_empty_list(script_name, name):
    rtype = "kubernetes/statefulset" if script_name == "server_status" else "kubernetes/daemonset"
    ctx = FakeCtx(
        rtype,
        name,
        answers=[("get pods", (1, "", 'pods is forbidden: cannot list resource "pods"'))],
    )
    with pytest.raises(RwError) as exc:
        script(script_name)(ctx)
    assert exc.value.code == "permission_denied"


def test_the_pods_are_capped_by_max_items_and_counts_are_not():
    ctx = FakeCtx(namespace_target=True)
    ctx.settings.max_items = 1
    out = server(ctx)
    assert len(out["pods"]) == 1 and out["counts"]["pods"] == 2 and len(out["statefulsets"]) == 1


# ------------------------------------------------------ csi provider facts ----


def test_a_ready_provider_daemonset():
    status = csi.daemonset_status(DS)
    assert (status["desired"], status["current"], status["ready"], status["updated"]) == (
        1,
        1,
        1,
        1,
    )
    assert (status["available"], status["unavailable"], status["misscheduled"]) == (1, 0, 0)
    assert status["maxUnavailable"] == 1 and status["unavailableExceedsMax"] is False
    assert status["generationObserved"] is True


def test_a_provider_that_is_not_ready_is_reported_as_counts():
    status = csi.daemonset_status(DS_BROKEN)
    assert (status["desired"], status["ready"], status["available"]) == (1, 0, 0)
    assert status["unavailable"] == 1


def test_max_unavailable_is_a_number_a_percentage_rounded_up_or_the_default():
    ds = copy.deepcopy(DS_BROKEN)
    ds["status"].update(desiredNumberScheduled=10, numberUnavailable=3)
    ds["spec"]["updateStrategy"] = {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": "25%"},
    }
    status = csi.daemonset_status(ds)
    assert status["maxUnavailable"] == 3 and status["unavailableExceedsMax"] is False
    ds["status"]["numberUnavailable"] = 4
    assert csi.daemonset_status(ds)["unavailableExceedsMax"] is True
    ds["spec"]["updateStrategy"] = {"type": "OnDelete"}
    assert csi.daemonset_status(ds)["maxUnavailable"] == 1


def test_a_count_the_api_omits_is_zero_and_an_unobserved_generation_is_flagged():
    ds = copy.deepcopy(DS_BROKEN)
    for key in (
        "numberAvailable",
        "numberUnavailable",
        "numberMisscheduled",
        "updatedNumberScheduled",
    ):
        ds["status"].pop(key, None)
    ds["status"]["observedGeneration"] = 0
    status = csi.daemonset_status(ds)
    assert (
        status["available"],
        status["unavailable"],
        status["misscheduled"],
        status["updated"],
    ) == (0, 0, 0, 0)
    assert status["generationObserved"] is False


def test_provider_pods_show_restarts_and_how_the_last_run_ended():
    pods = w.owned_pods(DS_BROKEN, PODS)
    [view] = csi.pod_views("vault-csi-provider-broken", pods)
    assert view["ready"] is False and view["restarts"] >= 1 and view["node"]
    [container] = view["containers"]
    assert container["name"] == "provider" and container["init"] is False
    assert container["restartCount"] >= 1 and container["ready"] is False
    assert container["lastTerminated"]["exitCode"] == 137


def test_init_containers_are_listed_and_flagged():
    pod = copy.deepcopy(w.owned_pods(DS, PODS)[0])
    pod["status"]["initContainerStatuses"] = [
        {
            "name": "install",
            "ready": True,
            "restartCount": 2,
            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
        }
    ]
    [view] = csi.pod_views("vault-csi-provider", [pod])
    assert [(c["name"], c["init"]) for c in view["containers"]] == [
        ("install", True),
        ("provider", False),
    ]
    assert view["restarts"] == 2
    assert view["containers"][0]["reason"] == "Completed"


def test_the_container_list_is_bounded():
    pod = copy.deepcopy(w.owned_pods(DS, PODS)[0])
    pod["status"]["containerStatuses"] = [
        {"name": f"c{i}", "ready": True, "restartCount": 0, "state": {"running": {}}}
        for i in range(60)
    ]
    [view] = csi.pod_views("d", [pod])
    assert len(view["containers"]) == 50 and view["containersTruncated"] is True


def test_the_ready_pod_is_after_the_one_that_is_not():
    pods = [*w.owned_pods(DS, PODS), *w.owned_pods(DS_BROKEN, PODS)]
    names = [v["pod"] for v in csi.pod_views("d", pods)]
    assert names[0].startswith("vault-csi-provider-broken")


# ------------------------------------------------------------ csi events ----


def test_warning_events_about_the_daemonset_pods_are_grouped_by_uid():
    pods = w.owned_pods(DS_BROKEN, PODS)
    rows = csi.warning_events(EVENTS, [DS_BROKEN], pods, "1h", REFERENCE_TIME, 200)
    assert rows and all(r["name"].startswith("vault-csi-provider-broken") for r in rows)
    unhealthy = next(r for r in rows if r["reason"] == "Unhealthy")
    assert unhealthy["count"] >= 2 and unhealthy["kind"] == "Pod"
    assert unhealthy["firstSeen"] <= unhealthy["lastSeen"]
    # the sealed Vault pod's probe events are about another object
    assert all("sealed" not in r["name"] for r in rows)


def test_an_event_about_an_earlier_pod_of_the_same_name_is_not_this_pods():
    pods = copy.deepcopy(w.owned_pods(DS_BROKEN, PODS))
    pods[0]["metadata"]["uid"] = "a-newer-incarnation"
    assert csi.warning_events(EVENTS, [DS_BROKEN], pods, "1h", REFERENCE_TIME, 200) == []


def test_events_outside_the_window_are_dropped():
    pods = w.owned_pods(DS_BROKEN, PODS)
    later = datetime(2026, 11, 4, 22, 45, tzinfo=timezone.utc)
    assert csi.warning_events(EVENTS, [DS_BROKEN], pods, "1h", later, 200) == []
    assert csi.warning_events(EVENTS, [DS_BROKEN], pods, "3d", later, 200) != []


def test_event_messages_are_truncated_with_a_flag_and_the_list_is_capped():
    pods = w.owned_pods(DS_BROKEN, PODS)
    events = copy.deepcopy(EVENTS)
    for ev in events:
        ev["message"] = "x" * 2000
    rows = csi.warning_events(events, [DS_BROKEN], pods, "1h", REFERENCE_TIME, 200)
    assert all(len(r["message"]) == 1024 and r["truncated"] for r in rows)
    assert len(csi.warning_events(events, [DS_BROKEN], pods, "1h", REFERENCE_TIME, 1)) == 1


def test_a_namespace_target_reports_every_provider_daemonset():
    ctx = FakeCtx(namespace_target=True)
    out = csi_status(ctx)
    assert {d["name"]: d["ready"] for d in out["daemonsets"]} == {
        "vault-csi-provider": 1,
        "vault-csi-provider-broken": 0,
    }
    assert [p["ready"] for p in out["pods"]] == [False, True]
    assert out["events"] and ctx.errors == []
    assert (
        "get daemonsets.apps -l app.kubernetes.io/name=vault-csi-provider -o json" in ctx.lines()[1]
    )


def test_a_daemonset_target_reports_only_that_daemonset():
    ctx = FakeCtx("kubernetes/daemonset", "vault-csi-provider")
    out = csi_status(ctx)
    assert [d["name"] for d in out["daemonsets"]] == ["vault-csi-provider"]
    assert [p["pod"] for p in out["pods"]] == ["vault-csi-provider-qb97m"]
    assert out["events"] == []  # a ready provider has no Warning events


def test_a_broken_provider_shows_its_restarts_and_warning_events():
    ctx = FakeCtx("kubernetes/daemonset", "vault-csi-provider-broken")
    out = csi_status(ctx, since="3650d")
    assert out["daemonsets"][0]["ready"] == 0 and out["daemonsets"][0]["unavailable"] == 1
    assert out["pods"][0]["restarts"] >= 1
    assert any(e["reason"] == "Unhealthy" for e in out["events"])


def test_forbidden_events_make_the_result_partial_and_keep_the_daemonset_facts():
    ctx = FakeCtx(
        "kubernetes/daemonset",
        "vault-csi-provider-broken",
        answers=[
            ("get events", (1, "", 'events is forbidden: User "u" cannot list resource "events"'))
        ],
    )
    out = csi_status(ctx)
    assert out["daemonsets"][0]["name"] == "vault-csi-provider-broken" and out["events"] == []
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "events")]


def test_an_events_failure_that_is_not_forbidden_raises():
    ctx = FakeCtx(
        "kubernetes/daemonset", "vault-csi-provider", answers=[("get events", (1, "", UNREACHABLE))]
    )
    with pytest.raises(RwError) as exc:
        csi_status(ctx)
    assert exc.value.code == "unreachable"


@pytest.mark.parametrize("since", ["banana", "10", "1w", ""])
def test_a_bad_since_fails_before_any_kubectl_call(since):
    ctx = FakeCtx("kubernetes/daemonset", "vault-csi-provider")
    with pytest.raises(RwError) as exc:
        csi_status(ctx, since=since)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_the_look_back_is_taken_from_ctx_now():
    ctx = FakeCtx("kubernetes/daemonset", "vault-csi-provider-broken")
    ctx.now = lambda: datetime(2026, 11, 4, 22, 45, tzinfo=timezone.utc)
    assert csi_status(ctx)["events"] == []


# ---------------------------------------------------------------- errors ----


@pytest.mark.parametrize(
    "rc,stderr,code",
    [
        (1, FORBIDDEN, "permission_denied"),
        (1, NOT_FOUND, "not_found"),
        (1, UNREACHABLE, "unreachable"),
        (124, "", "timeout"),
        (127, "", "dependency"),
        (1, "error: the server doesn't have a resource type", "dependency"),
        (1, "Error from server (TooManyRequests): 429", "rate_limited"),
        (1, "something odd", "internal"),
    ],
)
def test_kubectl_failures_are_classified(rc, stderr, code):
    assert k8s.classify(rc, stderr) == code


def test_non_json_output_is_an_internal_error():
    ctx = FakeCtx(answers=[("get statefulsets.apps vault", (0, "not json", ""))])
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "internal"


def test_outputs_carry_no_secret_or_env_value():
    blob = json.dumps(server(FakeCtx(namespace_target=True)))
    blob += json.dumps(csi_status(FakeCtx(namespace_target=True)))
    for needle in ("VAULT_LOCAL_CONFIG", "SKIP_SETCAP", "token"):
        assert needle not in blob


# ----------------------------------------------------------- permissions ----

RESOURCES = {
    "statefulsets.apps": ("apps", "statefulsets"),
    "daemonsets.apps": ("apps", "daemonsets"),
    "pods": ("", "pods"),
    "events": ("", "events"),
    "namespaces": ("", "namespaces"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-l", "--selector", "--field-selector"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l) -> list;
    get --raw <api path>/pods/<pod>/proxy/... -> pods/proxy get. Raises on anything else.
    """
    if argv[1:3] == ["get", "--raw"]:
        assert "/proxy/" in argv[3] and "/pods/" in argv[3], argv
        return {("", "pods/proxy", "get")}
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
        return {(group, plural, "get" if named_object else "list")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, res, v)
        for rule in TASKS[task]["permissions"]
        for g in rule["apiGroups"]
        for res in rule["resources"]
        for v in rule["verbs"]
    }


def needs_of(ctxs, module):
    needed = set()
    for ctx in ctxs:
        script(module)(ctx)
        for argv in ctx.calls:
            needed |= rbac_needs(argv)
    return needed


def test_server_status_declares_exactly_the_permissions_its_calls_need():
    needed = needs_of(
        [FakeCtx(namespace_target=True), FakeCtx("kubernetes/statefulset", "vault")],
        "server_status",
    )
    assert needed == declared("server-status")


def test_csi_provider_status_declares_exactly_the_permissions_its_calls_need():
    needed = needs_of(
        [
            FakeCtx(namespace_target=True),
            FakeCtx("kubernetes/daemonset", "vault-csi-provider"),
        ],
        "csi_provider_status",
    )
    assert needed == declared("csi-provider-status")


def test_a_single_workload_target_needs_no_namespace_read():
    needed = needs_of([FakeCtx("kubernetes/statefulset", "vault")], "server_status")
    assert ("", "namespaces", "get") not in needed


def test_no_task_reads_a_secret_or_a_config_map():
    for task in TASKS.values():
        for rule in task["permissions"]:
            assert len(rule["apiGroups"]) == 1
            assert not {"secrets", "configmaps"} & set(rule["resources"])


def test_both_tasks_only_read():
    assert all(t["readOnly"] is True for t in TASKS.values())


def test_every_schema_is_valid_json_schema():
    for path in (SKILL / "schemas").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


# ------------------------------------------------- runs that prove nothing ----


def test_a_statefulset_that_wants_pods_and_has_none_to_ask_is_partial_not_clean():
    for pods in ([], [p for p in PODS if p["metadata"]["name"] == "vault-0"]):
        ctx = FakeCtx(pods=[dict(p, status={"phase": "Pending"}) for p in pods])
        out = server(ctx)
        assert out["counts"]["queried"] == 0 and proxy_calls(ctx) == []
        [err] = ctx.errors
        assert (err["code"], err["scope"]) == ("unreachable", "pods")
        assert "no Vault pod could be queried" in err["message"] and err["hint"]


def test_a_statefulset_scaled_to_zero_is_a_clean_empty_result():
    sts = copy.deepcopy(STS)
    sts["spec"]["replicas"] = 0
    ctx = FakeCtx(pods=[])
    ctx.resource.name = "vault"
    real = ctx.default
    ctx.default = lambda argv, line: (
        ctx.projected(argv, [sts])
        if "get statefulsets.apps vault " in line + " "
        else real(argv, line)
    )
    out = server(ctx)
    assert out["statefulsets"][0]["desired"] == 0 and ctx.errors == []


def test_a_pod_that_was_queried_keeps_the_result_clean_of_the_no_pod_error():
    ctx = FakeCtx()
    server(ctx)
    assert ctx.errors == []


# ---------------------------------------------------------- proxy mapping ----


MTLS = (
    "Error from server (ServiceUnavailable): error trying to reach service: remote error: "
    "tls: bad certificate"
)


def test_a_listener_that_wants_a_client_certificate_is_unreachable_with_a_hint():
    ctx = FakeCtx(answers=[("get --raw", (1, "", MTLS))])
    with pytest.raises(RwError) as exc:
        server(ctx, scheme="https")
    assert exc.value.code == "unreachable"
    assert "bad certificate" in exc.value.message
    assert "mutual TLS" in exc.value.hint


def test_the_mutual_tls_hint_reaches_the_scoped_error_of_a_partial_result():
    ctx = FakeCtx(namespace_target=True, answers=[("vault-sealed-0:8200", (1, "", MTLS))])
    out = server(ctx)
    assert by_pod(out["pods"])["vault-sealed-0"]["error"]["code"] == "unreachable"
    [err] = ctx.errors
    assert (err["code"], err["scope"]) == ("unreachable", "pod/vault-sealed-0")
    assert "mutual TLS" in err["hint"]


def test_a_404_from_the_proxy_names_the_port_input():
    not_found = (
        "Error from server (NotFound): the server could not find the requested resource (get pods "
        "http:vault-0:9999)"
    )
    ctx = FakeCtx(answers=[("get --raw", (1, "", not_found))])
    with pytest.raises(RwError) as exc:
        server(ctx, port=9999)
    assert exc.value.code == "not_found"
    assert "port" in exc.value.hint and "8200" in exc.value.hint


def test_a_removed_raft_node_is_the_removed_state_not_internal():
    removed = {**doc(), "removed_from_cluster": True}
    assert vault.health_state(removed) == ("removed", 530)
    ctx = FakeCtx(health={"vault-0": removed})
    out = server(ctx)
    entry = out["pods"][0]
    assert (entry["health"]["state"], entry["health"]["healthCode"]) == ("removed", 530)
    assert entry["error"] is None and ctx.errors == []
    assert out["counts"]["byState"] == {"removed": 1}


def test_a_530_the_proxy_could_not_carry_is_the_removed_state_too():
    ctx = FakeCtx(answers=[("get --raw", (1, "", "Error from server: unexpected status code 530"))])
    out = server(ctx)
    entry = out["pods"][0]
    assert entry["health"]["state"] == "removed" and entry["health"]["healthCode"] == 530
    assert (entry["health"]["initialized"], entry["health"]["sealed"]) == (None, None)
    assert entry["error"] is None


def test_a_port_that_contains_530_is_not_taken_for_a_removed_node():
    ctx = FakeCtx(answers=[("get --raw", (1, "", "dial tcp 10.0.0.5:8530: connection refused"))])
    with pytest.raises(RwError) as exc:
        server(ctx)
    assert exc.value.code == "unreachable"


def test_removed_pods_sort_with_the_sealed_ones_first():
    health = {
        "vault-0": HEALTH["vault-0"],
        "vault-sealed-0": {**doc(), "removed_from_cluster": True},
    }
    out = server(FakeCtx(namespace_target=True, health=health))
    assert [p["pod"] for p in out["pods"]][0] == "vault-sealed-0"


# ------------------------------------------------------------ time budget ----


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def slow(ctx, clock, seconds):
    real = ctx.run

    def run(argv, check=True, **kw):
        if argv[1:3] == ["get", "--raw"]:
            clock.now += seconds
        return real(argv, check=check, **kw)

    ctx.run = run
    return ctx


def test_pods_reached_after_the_budget_is_spent_get_a_timeout_error_and_are_not_asked():
    from k8s_vault_health.check import server_status

    clock = Clock()
    ctx = slow(FakeCtx(namespace_target=True), clock, 50)
    _sts, pods, counts = server_status(ctx, "http", 8200, 200, Budget(45, clock))
    by = by_pod(pods)
    assert by["vault-0"]["queried"] is True and by["vault-0"]["error"] is None
    assert by["vault-sealed-0"]["queried"] is False
    assert by["vault-sealed-0"]["error"]["code"] == "timeout"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("timeout", "pod/vault-sealed-0")]
    assert len(proxy_calls(ctx)) == 1
    assert counts["queried"] == 1 and counts["failed"] == 1


def test_a_budget_spent_before_any_pod_fails_the_task_with_timeout():
    from k8s_vault_health.check import server_status

    clock = Clock()
    budget = Budget(45, clock)
    clock.now += 46  # the fetches before the first pod used it all
    with pytest.raises(RwError) as exc:
        server_status(FakeCtx(), "http", 8200, 200, budget)
    assert exc.value.code == "timeout"


def test_every_proxy_call_gets_a_timeout_of_at_most_ten_seconds():
    ctx = FakeCtx(namespace_target=True)
    server(ctx)
    proxied = [t for argv, t in zip(ctx.calls, ctx.timeouts) if argv[1:3] == ["get", "--raw"]]
    assert proxied and all(t is not None and 1 <= t <= 10 for t in proxied)


# ------------------------------------------------------------ csi, bounds ----


def test_any_unavailable_pod_is_flagged_even_below_max_unavailable():
    ok = csi.daemonset_status(DS)
    assert (ok["unavailable"], ok["anyUnavailable"]) == (0, False)
    broken = csi.daemonset_status(DS_BROKEN)
    assert broken["unavailable"] == 1 and broken["anyUnavailable"] is True
    assert broken["unavailableExceedsMax"] is False  # 1 is within the default maxUnavailable of 1
    out = csi_status(FakeCtx("kubernetes/daemonset", "vault-csi-provider-broken"))
    assert out["daemonsets"][0]["anyUnavailable"] is True


def test_server_text_is_bounded_to_1024():
    facts = vault.health_facts(
        doc(version="1." + "9" * 3000, cluster_name="c" * 3000, replication_dr_mode="d" * 3000)
    )
    assert len(facts["version"]) == len(facts["clusterName"]) == 1024
    assert len(facts["replicationDrMode"]) == 1024


def test_every_string_in_the_schemas_has_a_maxlength_unless_it_is_an_enum():
    def walk(node, where):
        if isinstance(node, dict):
            types = node.get("type")
            types = types if isinstance(types, list) else [types]
            if "string" in types and not ({"enum", "const", "format"} & node.keys()):
                assert "maxLength" in node, where
            for key, value in node.items():
                walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}/{i}")

    for path in (SKILL / "schemas").glob("*.json"):
        walk(json.loads(path.read_text()), path.name)


# --------------------------------------------------- narrow kubectl fetches ----


def test_no_workload_or_pod_read_returns_a_whole_object_so_env_never_comes_back():
    ctx = FakeCtx(namespace_target=True)
    server(ctx)
    csi_status(ctx)
    csi_status(FakeCtx("kubernetes/daemonset", "vault-csi-provider"))
    reads = [
        a
        for a in ctx.calls
        if "get" in a
        and "--raw" not in a
        and a[a.index("get") + 1] != "events"
        and "namespaces" not in a
    ]
    assert reads
    for argv in reads:
        out = argv[argv.index("-o") + 1]
        assert out.startswith("jsonpath="), argv
        for forbidden in (
            "env",
            "annotations",
            "command",
            "args",
            "template",
            "metadata}",
            "spec}",
        ):
            assert forbidden not in out, (forbidden, out)


def test_the_recorded_data_holds_no_env_and_no_last_applied_configuration():
    blob = "".join(p.read_text() for p in DATA.glob("*.json"))
    for forbidden in ('"env"', "VAULT_LOCAL_CONFIG", "last-applied-configuration", "valueFrom"):
        assert forbidden not in blob


def test_a_projection_round_trips_and_leaves_out_what_the_object_lacks():
    pod = next(p for p in PODS if p["metadata"]["name"] == "vault-0")
    doc_ = project.POD_FIELDS.parse(render(pod, project.POD_FIELDS), many=False)
    assert doc_["metadata"]["name"] == "vault-0" and doc_["status"] == pod["status"]
    bare = project.POD_FIELDS.parse("@\ns:metadata.name=x\nj:status=\n", many=False)
    assert bare == {"metadata": {"name": "x"}}
    assert project.STATEFULSET_FIELDS.parse("", many=True) == {"items": []}


@pytest.mark.parametrize("bad", ["", "oops\n", "@\nx:metadata.name=a\n", "@\nj:status={nope\n"])
def test_output_that_is_not_the_projection_is_an_internal_error(bad):
    with pytest.raises(RwError) as exc:
        project.POD_FIELDS.parse(bad, many=False)
    assert exc.value.code == "internal"
