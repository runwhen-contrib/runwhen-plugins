"""Unit tests for k8s-chaos: lib/ and the thin scripts, against JSON captured from the
kind-rw-plugins scenario namespace rw-chaos (web Deployment, db StatefulSet, nolimit and
nosh Deployments and their Services). No test calls a cluster: scripts run against a fake
ctx that answers kubectl from the captured files, keeps the state a chaos action changes
(pods, Services) and records every call. Sleeping is replaced by a fake clock."""

import copy
import importlib.util
import inspect
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_chaos import clock, k8s
from k8s_chaos import common as c
from k8s_chaos import service as svc
from k8s_chaos.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-chaos"
DATA = Path(__file__).parent / "data" / "k8s_chaos"
NOW = datetime(2026, 11, 3, 21, 30, 0, tzinfo=UTC)


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


WEB = load("web_deployment")
DB = load("db_statefulset")
WEB_RS = load("web_replicasets")["items"]
PODS = load("pods")["items"]
SERVICES = {s["metadata"]["name"]: s for s in load("services")["items"]}
SLICES = load("endpointslices")["items"]
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


def script(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


MAIN = {
    "delete-pods": script("delete_pods"),
    "oomkill-pod": script("oomkill_pod"),
    "mangle-service": script("mangle_service"),
    "fill-tmp": script("fill_tmp"),
}


# ------------------------------------------------------------ fake clock ----


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(clock, "monotonic", fc.monotonic)
    monkeypatch.setattr(clock, "sleep", fc.sleep)
    return fc


# ------------------------------------------------------------ fake ctx ----


class FakeCommandError(RwError):
    """What the v13 runner raises for a non-zero exit when check is true."""

    def __init__(self, argv, returncode, stderr):
        super().__init__("internal", f"{argv[0]} exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


def not_found(kind, name):
    return 1, "", f'Error from server (NotFound): {kind} "{name}" not found'


FORBIDDEN = (
    1,
    "",
    'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods" in API group "" in the namespace "rw-chaos"',
)


def merge(doc, patch):
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    doc = dict(doc) if isinstance(doc, dict) else {}
    for key, value in patch.items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = merge(doc.get(key), value)
    return doc


class FakeCtx:
    """What a script sees: resource, settings, dry_run, now(), error(), run().

    `answers` are (substring of the joined argv, (rc, stdout, stderr)) checked before the
    defaults. A list answer is used in order, the last entry repeating. The fake keeps
    the state chaos changes: pods (a delete marks a pod terminating and, after
    `heal_after` pod listings, replaces it), Services (a merge patch changes them) and the
    EndpointSlices derived from them. `exec_handler(ctx, argv)` answers kubectl exec.
    """

    def __init__(
        self,
        target="web",
        dry_run=False,
        answers=(),
        exec_handler=None,
        heal_after=2,
        pods=None,
        max_memory_limit_mi=4096,
        now=NOW,
        services=None,
    ):
        types = {
            "web": ("kubernetes/deployment", "deployments.apps", WEB),
            "db": ("kubernetes/statefulset", "statefulsets.apps", DB),
            "rw-chaos": ("kubernetes/namespace", None, None),
            "nolimit": ("kubernetes/deployment", "deployments.apps", None),
            "nosh": ("kubernetes/deployment", "deployments.apps", None),
        }
        rtype, self.kind_resource, self.doc = types[target]
        self.target_name = target
        namespace = None if target == "rw-chaos" else "rw-chaos"
        self.resource = SimpleNamespace(namespace=namespace, name=target, type=rtype, path=None)
        self.settings = SimpleNamespace(max_items=200, max_memory_limit_mi=max_memory_limit_mi)
        self.dry_run = dry_run
        self._now = now
        self.answers = list(answers)
        self.exec_handler = exec_handler
        self.pods = copy.deepcopy(pods if pods is not None else PODS)
        self.services = copy.deepcopy(services if services is not None else SERVICES)
        self.heal_after = heal_after
        self._healing = None
        self.calls = []
        self.errors = []
        self.serial = 0
        if target in ("nolimit", "nosh"):
            self.doc = self.workload_for(target)

    # -- the SDK surface
    def now(self):
        return self._now

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message})

    def run(self, argv, check=True, **kwargs):
        """Like the v13 runner: a non-zero exit raises unless the caller passes check=False."""
        self.calls.append(argv)
        line = " ".join(argv[1:])
        answer = None
        for needle, candidate in self.answers:
            if needle in line:
                answer = candidate
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                break
        if callable(answer):
            answer = answer(self, argv)
        rc, out, err = answer if answer is not None else self.default(argv)
        if rc and check:
            raise FakeCommandError(argv, rc, err)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    # -- helpers for tests
    def lines(self):
        return [" ".join(c[1:]) for c in self.calls]

    def verbs(self, verb):
        return [a for a in self.calls if verb in a[:6]]

    def workload_for(self, name):
        """A Deployment document for nolimit / nosh, shaped like web's."""
        doc = copy.deepcopy(WEB)
        doc["metadata"].update(name=name, uid=f"uid-{name}")
        doc["spec"]["selector"]["matchLabels"] = {"app": name}
        doc["spec"]["replicas"] = 1
        return doc

    def synthetic_replicasets(self):
        """ReplicaSets owned by the nolimit / nosh Deployment, from its pods' owner references."""
        out = {}
        for p in self.pods:
            ref = p["metadata"]["ownerReferences"][0]
            if (
                ref["kind"] == "ReplicaSet"
                and p["metadata"]["labels"].get("app") == self.target_name
            ):
                out[ref["uid"]] = {
                    "metadata": {
                        "uid": ref["uid"],
                        "name": ref["name"],
                        "ownerReferences": [
                            {"uid": self.doc["metadata"]["uid"], "controller": True}
                        ],
                    }
                }
        return list(out.values())

    def live_pods(self):
        return [p for p in self.pods if p["metadata"].get("deletionTimestamp") is None]

    # -- the default cluster
    def default(self, argv):
        args = [a for a in argv[1:]]
        flags = {}
        pos = []
        it = iter(args)
        for a in it:
            if a in ("-n", "-l", "-o", "-c", "-p", "--type"):
                flags[a] = next(it)
            elif a == "--":
                pos.append("--")
                pos.extend(it)
                break
            elif not a.startswith("-"):
                pos.append(a)
            else:
                flags[a] = True
        verb = pos[0]
        if verb == "get":
            return self.get(pos[1:], flags)
        if verb == "delete":
            return self.delete(argv, pos[2:], flags)
        if verb == "patch":
            return self.patch(argv, pos[2:], flags)
        if verb == "exec":
            if self.exec_handler is None:
                return 0, "", ""
            return self.exec_handler(self, argv)
        raise AssertionError(f"unexpected kubectl call {argv}")

    def get(self, pos, flags):
        kind = pos[0]
        name = pos[1] if len(pos) > 1 else None
        if kind == "namespaces":
            if name != "rw-chaos":
                return not_found("namespaces", name)
            return 0, json.dumps({"kind": "Namespace", "metadata": {"name": name}}), ""
        if kind in ("deployments.apps", "statefulsets.apps"):
            if name != self.target_name:
                return not_found(kind, name)
            return 0, json.dumps(self.workload_with_status()), ""
        if kind == "replicasets.apps":
            if self.target_name in ("nolimit", "nosh"):
                return 0, json.dumps({"items": self.synthetic_replicasets()}), ""
            return 0, json.dumps({"items": WEB_RS}), ""
        if kind == "pods":
            self.tick()
            if name:
                found = [p for p in self.pods if p["metadata"]["name"] == name]
                if not found:
                    return not_found("pods", name)
                return 0, json.dumps(found[0]), ""
            items = self.pods
            if "-l" in flags:
                items = [p for p in items if label_match(p, flags["-l"])]
            return 0, json.dumps({"items": items}), ""
        if kind == "services":
            if name:
                if name not in self.services:
                    return not_found("services", name)
                return 0, json.dumps(self.services[name]), ""
            return 0, json.dumps({"items": list(self.services.values())}), ""
        if kind == "endpointslices.discovery.k8s.io":
            name = flags["-l"].split("=", 1)[1]
            service = self.services[name]
            selector = service["spec"].get("selector") or {}
            n = sum(1 for p in self.live_pods() if ready(p) and labels_have(p, selector))
            if name == "db":
                n = sum(1 for p in self.live_pods() if ready(p) and labels_have(p, {"app": "db"}))
            return 0, json.dumps({"items": [slice_of(name, n)]}), ""
        raise AssertionError(f"unexpected get {pos}")

    def workload_with_status(self):
        doc = copy.deepcopy(self.doc)
        selector = doc["spec"]["selector"].get("matchLabels") or {}
        n = sum(1 for p in self.live_pods() if ready(p) and labels_have(p, selector))
        doc["status"] = {**doc.get("status", {}), "readyReplicas": n, "replicas": n}
        return doc

    def delete(self, argv, names, flags):
        names = [n for n in names if not n.startswith("-")]
        dry = "--dry-run=server" in argv
        out = []
        for n in names:
            found = [p for p in self.pods if p["metadata"]["name"] == n]
            if not found:
                continue
            out.append(f"pod/{n}" + (" deleted (server dry run)" if dry else " deleted"))
            if not dry:
                found[0]["metadata"]["deletionTimestamp"] = "2026-11-03T21:30:00Z"
                self._healing = self.heal_after
        return 0, "\n".join(out) + ("\n" if out else ""), ""

    def tick(self):
        """Pod listings after a delete count down to the replacements becoming ready."""
        if self._healing is None:
            return
        self._healing -= 1
        if self._healing > 0:
            return
        self._healing = None
        for p in [p for p in self.pods if p["metadata"].get("deletionTimestamp")]:
            self.pods.remove(p)
            fresh = copy.deepcopy(p)
            self.serial += 1
            fresh["metadata"].pop("deletionTimestamp")
            fresh["metadata"]["uid"] = f"replacement-{self.serial}"
            fresh["metadata"]["name"] = p["metadata"]["name"][:-5] + f"new{self.serial:02d}"
            if p["metadata"]["ownerReferences"][0]["kind"] == "StatefulSet":
                fresh["metadata"]["name"] = p["metadata"]["name"]
            self.pods.append(fresh)

    def patch(self, argv, pos, flags):
        name = pos[0]
        body = json.loads(flags["-p"])
        dry = "--dry-run=server" in argv
        if name not in self.services:
            return not_found("services", name)
        if not dry:
            self.services[name] = merge(self.services[name], body)
        return 0, f"service/{name} patched" + (" (server dry run)" if dry else "") + "\n", ""


def label_match(pod, selector):
    terms = dict(t.split("=", 1) for t in selector.split(","))
    return labels_have(pod, terms)


def labels_have(pod, want):
    labels = pod["metadata"].get("labels") or {}
    return all(labels.get(k) == v for k, v in want.items())


def ready(pod):
    return any(
        cond["type"] == "Ready" and cond["status"] == "True"
        for cond in pod["status"].get("conditions") or []
    )


def slice_of(service, n):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service}},
        "endpoints": [{"conditions": {"ready": True}} for _ in range(n)],
    }


def run_task(task, ctx, **inputs):
    return MAIN[task](ctx, **inputs)


def check_outputs(task, out):
    """Every output validates against the schema its tasks.yaml entry names."""
    assert set(out) == set(TASKS[task]["outputs"])
    for key, value in out.items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        errors = list(Draft202012Validator(schema).iter_errors(value))
        assert not errors, (task, key, errors[:2])


def writes(ctx):
    """The calls that change the cluster (not dry-run=server, not a read)."""
    out = []
    for argv in ctx.calls:
        if "--dry-run=server" in argv:
            continue
        if any(v in argv[:6] for v in ("delete", "patch", "exec")):
            out.append(argv)
    return out


def pod_named(ctx, name):
    return next(p for p in ctx.pods if p["metadata"]["name"] == name)


WEB_PODS = [p["metadata"]["name"] for p in PODS if p["metadata"]["name"].startswith("web-")]


# ------------------------------------------------------------ pure helpers ----


def test_resolve_target_from_type_then_path():
    t = c.resolve_target("kubernetes/deployment", None, "ns", "web")
    assert t == {"scope": "workload", "kind": "Deployment", "namespace": "ns", "name": "web"}
    t = c.resolve_target("kubernetes/namespace", None, None, "rw-chaos")
    assert t == {
        "scope": "namespace",
        "kind": "Namespace",
        "namespace": "rw-chaos",
        "name": "rw-chaos",
    }
    t = c.resolve_target(None, "kubernetes/clusters/k/namespaces/ns/statefulsets/db", "ns", "db")
    assert t["kind"] == "StatefulSet" and t["scope"] == "workload"
    t = c.resolve_target(None, "kubernetes/clusters/k/namespaces/rw-chaos", None, "rw-chaos")
    assert t["scope"] == "namespace"


@pytest.mark.parametrize("rtype", ["kubernetes/daemonset", "kubernetes/pod", "kubernetes/node"])
def test_resolve_target_refuses_other_kinds(rtype):
    with pytest.raises(RwError) as exc:
        c.resolve_target(rtype, None, "ns", "x")
    assert exc.value.code == "invalid_input"


def test_pick_is_stable_for_one_seed_and_varies_with_it():
    names = [f"pod-{i}" for i in range(20)]
    assert c.pick(names, 3, "a") == c.pick(list(reversed(names)), 3, "a")
    assert c.pick(names, 3, "a") != c.pick(names, 3, "b")
    assert len(c.pick(names, 50, "a")) == 20
    assert c.pick(names, 1, "a")[0] in c.pick(names, 5, "a")  # a prefix of one ranking


def test_seed_is_the_input_else_the_reference_time():
    assert c.seed_of("x", NOW) == ("x", "input")
    assert c.seed_of(None, NOW) == ("2026-11-03T21:30:00.000Z", "now")
    assert c.seed_of("  ", NOW)[1] == "now"


@pytest.mark.parametrize(
    "text,seconds", [("90s", 90), ("2m", 120), ("1m30s", 90), ("500ms", 0.5), ("10m", 600)]
)
def test_parse_duration(text, seconds):
    assert c.parse_duration(text, "t", 0, 600) == seconds


@pytest.mark.parametrize("text", ["abc", "", "10", "-5s", "1x", "11m", "0s"])
def test_parse_duration_rejects(text):
    with pytest.raises(RwError) as exc:
        c.parse_duration(text, "t", 1, 600)
    assert exc.value.code == "invalid_input"


def test_parse_bytes_and_allocation():
    assert c.parse_bytes("64Mi") == 64 * 2**20
    assert c.parse_bytes("1G") == 10**9
    assert c.parse_bytes("129e6") == 129_000_000
    assert c.parse_bytes("0.5Gi") == 2**29
    with pytest.raises(ValueError):
        c.parse_bytes("lots")
    limit = 64 * 2**20
    assert c.allocation_bytes(limit) == 128 * 2**20  # +64Mi (a quarter is only 16Mi)
    assert c.allocation_bytes(1024 * 2**20) == 1280 * 2**20  # a quarter
    assert c.allocation_bytes(limit) > limit


def test_memory_limit_finds_the_first_limited_container_and_ignores_no_limit():
    pod = {
        "spec": {
            "containers": [
                {"name": "a"},
                {"name": "b", "resources": {"limits": {"memory": "32Mi"}}},
                {"name": "c", "resources": {"limits": {"memory": "64Mi"}}},
            ]
        }
    }
    assert c.memory_limit(pod) == ("b", 32 * 2**20)
    assert c.memory_limit(pod, "c") == ("c", 64 * 2**20)
    assert c.memory_limit(pod, "a") is None
    assert c.memory_limit({"spec": {"containers": [{"name": "x"}]}}) is None
    bad = {"spec": {"containers": [{"name": "x", "resources": {"limits": {"memory": "??"}}}]}}
    assert c.memory_limit(bad) is None


def test_an_empty_selector_refuses_instead_of_selecting_every_pod():
    # the classic scripts fell through to `kubectl get pods -l ""` = every pod of the namespace
    for selector in ({}, {"matchLabels": {}}, None):
        with pytest.raises(RwError) as exc:
            c.selector_string(selector)
        assert exc.value.code == "invalid_input"
    assert c.selector_string({"matchLabels": {"b": "2", "a": "1"}}) == "a=1,b=2"
    assert (
        c.selector_string(
            {"matchExpressions": [{"key": "k", "operator": "In", "values": ["x", "y"]}]}
        )
        == "k in (x,y)"
    )


def test_owned_pods_need_the_owner_chain_not_only_the_labels():
    stranger = copy.deepcopy(next(p for p in PODS if p["metadata"]["name"].startswith("web-")))
    stranger["metadata"]["name"] = "web-stranger"
    stranger["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "other", "uid": "someone-else", "controller": True}
    ]
    mine = c.owned_pods("Deployment", WEB, WEB_RS, PODS + [stranger])
    names = {p["metadata"]["name"] for p in mine}
    assert names == set(WEB_PODS) and "web-stranger" not in names
    db_pods = c.owned_pods("StatefulSet", DB, [], PODS)
    assert {p["metadata"]["name"] for p in db_pods} == {"db-0", "db-1"}


def test_exec_failure_classes():
    def res(rc, err):
        return SimpleNamespace(returncode=rc, stdout="", stderr=err)

    argv = ["kubectl", "exec"]
    assert k8s.exec_failure(argv, res(0, "")) is None
    assert k8s.exec_failure(argv, res(137, "command terminated with exit code 137")) is None
    assert k8s.exec_failure(argv, res(1, "command terminated with exit code 1")) is None
    missing = 'OCI runtime exec failed: exec: "sh": executable file not found in $PATH: unknown'
    assert k8s.exec_failure(argv, res(1, missing)).code == "dependency"
    assert (
        k8s.exec_failure(
            argv, res(127, "sh: dd: not found\ncommand terminated with exit code 127")
        ).code
        == "dependency"
    )
    assert (
        k8s.exec_failure(argv, res(126, "command terminated with exit code 126")).code
        == "dependency"
    )
    assert (
        k8s.exec_failure(
            argv,
            res(
                1,
                'Error from server (Forbidden): pods "p" is forbidden: cannot create resource "pods/exec"',
            ),
        ).code
        == "permission_denied"
    )
    assert (
        k8s.exec_failure(argv, res(1, 'Error from server (NotFound): pods "p" not found')).code
        == "not_found"
    )
    assert (
        k8s.exec_failure(
            argv, res(1, "Unable to connect to the server: dial tcp: i/o timeout")
        ).code
        == "unreachable"
    )
    assert k8s.exec_failure(argv, res(1, "something odd")).code == "internal"


# --------------------------------------------------------- service helpers ----


def test_matching_services_need_every_selector_pair_in_the_pod_labels():
    labels = {"app": "web", "tier": "front"}
    svcs = [
        {"metadata": {"name": "a"}, "spec": {"selector": {"app": "web"}}},
        {"metadata": {"name": "b"}, "spec": {"selector": {"app": "web", "tier": "back"}}},
        {"metadata": {"name": "c"}, "spec": {}},  # ExternalName: no selector
        {"metadata": {"name": "d"}, "spec": {"selector": {"app": "web", "tier": "front"}}},
    ]
    assert [s["metadata"]["name"] for s in svc.matching_services(svcs, labels)] == ["a", "d"]


def test_mutation_plan_selector_changes_the_first_key_and_records_the_original():
    doc = copy.deepcopy(SERVICES["web"])
    doc["spec"]["selector"] = {"tier": "x", "app": "web"}
    plan = svc.mutation_plan(doc, "selector")
    assert plan["field"] == "spec.selector.app" and plan["from"] == "web"
    assert plan["patch"]["spec"]["selector"] == {"app": svc.MANGLED_VALUE, "tier": "x"}
    original = json.loads(plan["patch"]["metadata"]["annotations"][svc.ANNOTATION])
    assert original == {"selector": {"tier": "x", "app": "web"}, "ports": doc["spec"]["ports"]}


def test_mutation_plan_port_keeps_target_port_and_avoids_used_ports():
    doc = copy.deepcopy(SERVICES["web"])
    doc["spec"]["ports"].append({"name": "x", "port": 9999, "protocol": "TCP"})
    plan = svc.mutation_plan(doc, "port")
    assert plan["to"] == 9998 and plan["from"] == 80
    first = plan["patch"]["spec"]["ports"][0]
    assert first["port"] == 9998 and first["targetPort"] == 8080 and first["name"] == "http"
    # a named targetPort (the classic spliced it unquoted and broke the patch) is kept as is
    named = copy.deepcopy(SERVICES["web"])
    named["spec"]["ports"][0]["targetPort"] = "http-web"
    assert svc.mutation_plan(named, "port")["patch"]["spec"]["ports"][0]["targetPort"] == "http-web"


def test_mutation_plan_refuses_no_selector_bad_mode_and_a_service_still_mangled():
    headless = {"metadata": {"name": "ext"}, "spec": {"type": "ExternalName", "ports": []}}
    for doc, mode in ((headless, "selector"), (headless, "port"), (SERVICES["web"], "both")):
        with pytest.raises(RwError) as exc:
            svc.mutation_plan(doc, mode)
        assert exc.value.code == "invalid_input"
    mangled = copy.deepcopy(SERVICES["web"])
    mangled["metadata"].setdefault("annotations", {})[svc.ANNOTATION] = '{"selector":{"app":"web"}}'
    with pytest.raises(RwError) as exc:
        svc.mutation_plan(mangled, "selector")
    assert exc.value.code == "invalid_input" and "still mangled" in exc.value.message


def test_restore_patch_puts_the_original_back_and_removes_added_keys():
    original = svc.original_spec(SERVICES["web"])
    current = copy.deepcopy(SERVICES["web"])
    current["spec"]["selector"] = {"app": "rw-chaos-none", "extra": "1"}
    patch = svc.restore_patch(original, current)
    assert patch["spec"]["selector"] == {"app": "web", "extra": None}
    assert patch["spec"]["ports"] == original["ports"]
    assert patch["metadata"]["annotations"] == {svc.ANNOTATION: None}
    assert svc.is_restored(original, SERVICES["web"])
    assert not svc.is_restored(original, current)


def test_ready_endpoints_counts_unset_ready_as_ready():
    sl = [
        {
            "endpoints": [
                {"conditions": {"ready": True}},
                {"conditions": {}},
                {"conditions": {"ready": False}},
            ]
        }
    ]
    assert svc.ready_endpoints(sl) == 2
    assert svc.ready_endpoints([]) == 0


# ------------------------------------------------------------ delete-pods ----


def test_delete_pods_dry_run_names_the_pods_and_deletes_nothing():
    ctx = FakeCtx(dry_run=True)
    out = run_task("delete-pods", ctx, seed="s1", count=2)
    check_outputs("delete-pods", out)
    d = out["deletion"]
    assert d["applied"] is False and d["deleted"] == [] and d["deletedCount"] == 0
    assert len(d["wouldDelete"]) == 2 and d["wouldDeleteCount"] == 2
    assert d["recovery"] is None
    assert writes(ctx) == []
    (delete,) = ctx.verbs("delete")
    assert "--dry-run=server" in delete
    assert all(p["metadata"].get("deletionTimestamp") is None for p in ctx.pods)
    assert out["target"]["desiredReplicas"] == 3 and out["target"]["scope"] == "workload"


def test_a_dry_run_with_a_seed_shows_exactly_what_a_real_run_with_that_seed_deletes():
    dry = run_task("delete-pods", FakeCtx(dry_run=True), seed="abc", count=2)
    real_ctx = FakeCtx()
    real = run_task("delete-pods", real_ctx, seed="abc", count=2)
    assert real["deletion"]["deleted"] == dry["deletion"]["wouldDelete"]
    assert (
        real["deletion"]["seed"] == dry["deletion"]["seed"] == {"value": "abc", "source": "input"}
    )


def test_the_default_seed_is_the_reference_time_so_a_replay_picks_the_same_pods():
    a = run_task("delete-pods", FakeCtx(dry_run=True), count=2)
    b = run_task("delete-pods", FakeCtx(dry_run=True), count=2)
    assert a["deletion"]["seed"] == {"value": "2026-11-03T21:30:00.000Z", "source": "now"}
    assert a["deletion"]["wouldDelete"] == b["deletion"]["wouldDelete"]
    later = datetime(2026, 11, 3, 21, 31, 0, tzinfo=UTC)
    names = {
        tuple(
            run_task("delete-pods", FakeCtx(dry_run=True, now=later.replace(second=s)), count=2)[
                "deletion"
            ]["wouldDelete"]
        )
        for s in range(10)
    }
    assert len(names) > 1  # the choice does move with the time


def test_delete_pods_reports_recovery_of_a_workload_with_the_time_it_took(fake_clock):
    ctx = FakeCtx(heal_after=3)
    out = run_task("delete-pods", ctx, seed="s1", count=1)
    check_outputs("delete-pods", out)
    d = out["deletion"]
    assert d["applied"] is True and len(d["deleted"]) == 1 and d["wouldDelete"] == []
    r = d["recovery"]
    assert r["waited"] and r["recovered"] is True
    assert (
        r["desiredReplicas"] == 3 and r["readyReplicasBefore"] == 3 and r["readyReplicasAfter"] == 3
    )
    assert r["recoveryMs"] == int(sum(fake_clock.slept) * 1000) > 0
    deleted_uid = next(p for p in PODS if p["metadata"]["name"] == d["deleted"][0])["metadata"][
        "uid"
    ]
    assert deleted_uid not in {p["metadata"]["uid"] for p in ctx.pods}


def test_a_workload_that_does_not_recover_in_time_is_a_fact_not_a_failure(fake_clock):
    ctx = FakeCtx(heal_after=10_000)
    out = run_task("delete-pods", ctx, seed="s1", timeout="6s")
    r = out["deletion"]["recovery"]
    assert r["recovered"] is False and r["recoveryMs"] is None
    assert r["readyReplicasBefore"] == 3 and r["readyReplicasAfter"] == 2
    assert 6000 <= r["waitedMs"] < 9000
    assert ctx.errors == []


def test_a_statefulset_replacement_has_the_old_name_but_a_new_uid():
    ctx = FakeCtx("db", heal_after=2)
    out = run_task("delete-pods", ctx, seed="s2")
    check_outputs("delete-pods", out)
    assert out["deletion"]["recovery"]["recovered"] is True
    assert out["target"]["kind"] == "StatefulSet"
    # both old pods are not counted: only a replacement with a new uid is "ready again"
    assert any(p["metadata"]["uid"].startswith("replacement-") for p in ctx.pods)


def test_wait_for_recovery_false_does_not_watch(fake_clock):
    ctx = FakeCtx()
    out = run_task("delete-pods", ctx, seed="s1", wait_for_recovery=False)
    assert out["deletion"]["recovery"] is None and fake_clock.slept == []
    # the workload was read once, before the delete
    assert len([x for x in ctx.lines() if "get deployments.apps web" in x]) == 1


def test_a_namespace_target_picks_from_all_running_pods_and_does_not_wait(fake_clock):
    ctx = FakeCtx("rw-chaos")
    pods = copy.deepcopy(PODS)
    next(p for p in pods if p["metadata"]["name"] == "nolimit-694d6f9bf9-n96l2")["metadata"][
        "deletionTimestamp"
    ] = "2026-11-03T21:00:00Z"
    next(p for p in pods if p["metadata"]["name"] == "nosh-58ccbd7b98-b5nzr")["status"]["phase"] = (
        "Pending"
    )
    ctx.pods = pods
    seen = set()
    for i in range(30):
        c2 = FakeCtx("rw-chaos", dry_run=True, pods=pods)
        out = run_task("delete-pods", c2, seed=f"s{i}", count=1)
        seen.update(out["deletion"]["wouldDelete"])
        assert out["deletion"]["candidateCount"] == 5
    assert "nolimit-694d6f9bf9-n96l2" not in seen and "nosh-58ccbd7b98-b5nzr" not in seen
    assert seen & {"db-0", "db-1"} and seen & set(WEB_PODS)  # more than one workload is hit
    out = run_task("delete-pods", ctx, seed="s1")
    check_outputs("delete-pods", out)
    assert out["target"]["scope"] == "namespace" and out["target"]["desiredReplicas"] is None
    assert out["deletion"]["recovery"] is None and fake_clock.slept == []
    assert not any("deployments.apps" in x for x in ctx.lines())
    assert ctx.lines()[0] == "get namespaces rw-chaos -o json"


def test_a_workload_target_never_touches_pods_of_other_workloads():
    for i in range(20):
        ctx = FakeCtx(dry_run=True)
        out = run_task("delete-pods", ctx, seed=f"x{i}", count=3)
        assert set(out["deletion"]["wouldDelete"]) <= set(WEB_PODS)


def test_count_larger_than_the_candidates_deletes_them_all_and_says_so():
    ctx = FakeCtx()
    out = run_task("delete-pods", ctx, count=10, wait_for_recovery=False)
    d = out["deletion"]
    assert d["requestedCount"] == 10 and d["selectedCount"] == 3 and d["deletesAllReady"] is True


def test_no_running_pod_is_not_found_not_an_empty_success():
    pods = copy.deepcopy(PODS)
    for p in pods:
        p["status"]["phase"] = "Pending"
    ctx = FakeCtx(pods=pods)
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx)
    assert exc.value.code == "not_found" and "nothing was changed" in exc.value.message
    assert ctx.verbs("delete") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 0},
        {"count": 11},
        {"count": "2"},
        {"count": True},
        {"timeout": "soon"},
        {"timeout": "10m"},
    ],
)
def test_bad_inputs_fail_before_any_kubectl_call(kwargs):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx, **kwargs)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_a_failed_delete_raises_and_a_partial_one_reports_the_pods_it_deleted():
    ctx = FakeCtx(answers=[("delete pod", (1, "", FORBIDDEN[2].replace("list", "delete")))])
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx, seed="s1")
    assert exc.value.code == "permission_denied"
    partial = (
        1,
        "pod/web-84484b8c4d-6vwxs deleted\n",
        "Error from server: pod web-x: etcd timeout",
    )
    ctx = FakeCtx(answers=[("delete pod", partial)])
    out = run_task("delete-pods", ctx, count=2, wait_for_recovery=False)
    assert out["deletion"]["deleted"] == ["web-84484b8c4d-6vwxs"]
    assert [e["scope"] for e in ctx.errors] == ["delete"]


def test_a_pod_already_gone_is_reported_not_counted_as_deleted():
    ctx = FakeCtx(answers=[("delete pod", (0, "", ""))])  # --ignore-not-found prints nothing
    out = run_task("delete-pods", ctx, seed="s1", wait_for_recovery=False)
    d = out["deletion"]
    assert d["deleted"] == [] and d["applied"] is False and d["alreadyGoneCount"] == 1


def test_recovery_watch_failure_is_reported_after_the_delete_happened():
    ctx = FakeCtx(
        answers=[
            (
                "get deployments.apps web",
                [None, (1, "", "Unable to connect to the server: dial tcp: i/o timeout")],
            )
        ]
    )
    out = run_task("delete-pods", ctx, seed="s1")
    assert out["deletion"]["applied"] is True and out["deletion"]["recovery"]["recovered"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "recovery")]


def test_a_workload_with_an_empty_selector_acts_on_nothing():
    broken = copy.deepcopy(WEB)
    broken["spec"]["selector"] = {}
    ctx = FakeCtx()
    ctx.doc = broken
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx)
    assert exc.value.code == "invalid_input"
    assert ctx.verbs("delete") == [] and not any(
        x.endswith("get pods -o json") for x in ctx.lines()
    )


# ------------------------------------------------------------- oomkill-pod ----


def oom_exec(
    rc=137, err="command terminated with exit code 137", kill=True, kill_reason="OOMKilled"
):
    def handler(ctx, argv):
        name = argv[argv.index("exec") + 1]
        container = argv[argv.index("-c") + 1]
        if kill:
            pod = pod_named(ctx, name)
            status = next(s for s in pod["status"]["containerStatuses"] if s["name"] == container)
            status["restartCount"] += 1
            status["lastState"] = {
                "terminated": {
                    "reason": kill_reason,
                    "exitCode": 137,
                    "finishedAt": "2026-11-03T21:30:05Z",
                }
            }
        return rc, "", err

    return handler


def clean_pods():
    """The captured pods as they were before any chaos: no restarts, no lastState."""
    pods = copy.deepcopy(PODS)
    for p in pods:
        for s in p["status"]["containerStatuses"]:
            s["restartCount"] = 0
            s["lastState"] = {}
    return pods


def test_oomkill_reports_the_kill_the_restart_delta_and_the_command_shape():
    ctx = FakeCtx(exec_handler=oom_exec(), pods=clean_pods())
    out = run_task("oomkill-pod", ctx, seed="o1")
    check_outputs("oomkill-pod", out)
    o = out["oomkill"]
    assert o["applied"] is True and o["exitCode"] == 137 and o["oomKilled"] is True
    assert o["restartCountBefore"] == 0 and o["restartCountAfter"] == 1 and o["restartDelta"] == 1
    assert o["lastState"] == {
        "reason": "OOMKilled",
        "exitCode": 137,
        "finishedAt": "2026-11-03T21:30:05Z",
    }
    assert o["memoryLimitBytes"] == 64 * 2**20 and o["allocateBytes"] == 128 * 2**20
    assert o["allocateBytes"] > o["memoryLimitBytes"]
    assert o["command"] == {"binary": "sh", "argCount": 4}
    (exec_argv,) = ctx.verbs("exec")
    assert exec_argv[-1] == str(o["allocateBytes"]) and "-c" in exec_argv
    assert o["pod"]["name"] in WEB_PODS


def test_oomkill_without_a_kill_is_oomkilled_false_after_the_wait(fake_clock):
    ctx = FakeCtx(exec_handler=oom_exec(rc=0, err="", kill=False), pods=clean_pods())
    out = run_task("oomkill-pod", ctx, seed="o1", timeout="5s")
    o = out["oomkill"]
    assert o["exitCode"] == 0 and o["oomKilled"] is False and o["restartDelta"] == 0
    assert 5000 <= o["observedMs"] < 8000


def test_an_old_oomkilled_state_is_not_taken_for_this_run():
    # the captured pods already carry an OOMKilled lastState from earlier runs
    ctx = FakeCtx(exec_handler=oom_exec(rc=0, err="", kill=False))
    pod = next(p for p in PODS if p["metadata"]["name"] == "web-84484b8c4d-2ddg7")
    assert pod["status"]["containerStatuses"][0]["lastState"]["terminated"]["reason"] == "OOMKilled"
    for i in range(10):
        out = run_task("oomkill-pod", ctx, seed=f"old{i}", timeout="1s")
        assert out["oomkill"]["oomKilled"] is False


def test_oomkill_a_restart_that_is_not_an_oom_is_not_reported_as_one():
    ctx = FakeCtx(exec_handler=oom_exec(kill_reason="Error"), pods=clean_pods())
    out = run_task("oomkill-pod", ctx, seed="o1")
    assert out["oomkill"]["restartDelta"] == 1 and out["oomkill"]["oomKilled"] is False


def test_oomkill_dry_run_runs_nothing():
    ctx = FakeCtx(dry_run=True, exec_handler=oom_exec())
    out = run_task("oomkill-pod", ctx, seed="o1")
    check_outputs("oomkill-pod", out)
    o = out["oomkill"]
    assert o["applied"] is False and o["exitCode"] is None and o["oomKilled"] is None
    assert o["restartCountAfter"] is None and o["allocateBytes"] > 0
    assert ctx.verbs("exec") == [] and writes(ctx) == []


def test_a_target_with_no_memory_limit_is_invalid_input_and_runs_nothing():
    pods = [p for p in PODS if p["metadata"]["name"].startswith("nolimit")]
    ctx = FakeCtx("nolimit", pods=pods, exec_handler=oom_exec())
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", ctx)
    assert exc.value.code == "invalid_input" and "memory limit" in exc.value.message
    assert ctx.verbs("exec") == []


def test_the_namespace_form_skips_pods_without_limits_and_never_picks_them():
    seen = set()
    for i in range(25):
        ctx = FakeCtx("rw-chaos", dry_run=True)
        seen.add(run_task("oomkill-pod", ctx, seed=f"n{i}")["oomkill"]["pod"]["name"])
    assert "nolimit-694d6f9bf9-n96l2" not in seen
    assert len(seen) > 1


def test_a_named_container_without_a_limit_or_missing_is_invalid_input():
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", FakeCtx(dry_run=True), container="sidecar")
    assert exc.value.code == "invalid_input"
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", FakeCtx(dry_run=True), container="Not A Name")
    assert exc.value.code == "invalid_input"


def test_a_limit_above_the_setting_is_refused():
    ctx = FakeCtx(max_memory_limit_mi=32, dry_run=True)
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", ctx)
    assert exc.value.code == "invalid_input" and "maxMemoryLimitMi" in exc.value.message


def test_a_container_with_no_shell_is_a_dependency_error_not_a_pass():
    pods = [p for p in PODS if p["metadata"]["name"].startswith("nosh")]
    stderr = (
        "error: Internal error occurred: error executing command in container: failed to exec in "
        'container: OCI runtime exec failed: exec: "sh": executable file not found in $PATH'
    )
    ctx = FakeCtx("nosh", pods=pods, exec_handler=lambda c, a: (1, "", stderr))
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", ctx)
    assert exc.value.code == "dependency"


def test_a_missing_dd_is_a_dependency_error():
    ctx = FakeCtx(
        exec_handler=lambda c, a: (
            127,
            "",
            "sh: dd: not found\ncommand terminated with exit code 127",
        )
    )
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", ctx, seed="o1")
    assert exc.value.code == "dependency"


def test_a_forbidden_exec_raises_permission_denied():
    err = 'Error from server (Forbidden): pods "x" is forbidden: User "u" cannot create resource "pods/exec"'
    ctx = FakeCtx(exec_handler=lambda c, a: (1, "", err))
    with pytest.raises(RwError) as exc:
        run_task("oomkill-pod", ctx, seed="o1")
    assert exc.value.code == "permission_denied"


def test_an_exec_stream_cut_by_the_kill_is_still_a_result_when_the_kill_happened():
    cut = oom_exec(
        rc=1, err="error: Internal error occurred: error executing command in container: EOF"
    )
    ctx = FakeCtx(exec_handler=cut, pods=clean_pods())
    out = run_task("oomkill-pod", ctx, seed="o1")
    assert out["oomkill"]["oomKilled"] is True and out["oomkill"]["exitCode"] is None
    # but the same error with no kill is a failure, not a quiet pass
    nokill = oom_exec(rc=1, err="error: Internal error occurred: EOF", kill=False)
    with pytest.raises(RwError) as exc:
        run_task(
            "oomkill-pod", FakeCtx(exec_handler=nokill, pods=clean_pods()), seed="o1", timeout="1s"
        )
    assert exc.value.code == "internal"


def test_the_pod_disappearing_after_the_exec_is_reported():
    def gone(ctx, argv):
        ctx.pods.clear()
        return 137, "", "command terminated with exit code 137"

    out = run_task("oomkill-pod", FakeCtx(exec_handler=gone, pods=clean_pods()), seed="o1")
    o = out["oomkill"]
    assert o["podPresent"] is False and o["oomKilled"] is False and o["restartCountAfter"] is None


# ---------------------------------------------------------------- fill-tmp ----


def fill_exec(bytes_written="104857600", rc=0, err="", clean_rc=0, pod_effect=None):
    """dd writes the file (stdout is wc -c); rm -f removes it. Records what stayed behind."""

    def handler(ctx, argv):
        script_text = argv[argv.index("--") + 3]
        name = argv[argv.index("exec") + 1]
        if "dd " in script_text:
            ctx.files = getattr(ctx, "files", set()) | {argv[-2]}
            if pod_effect:
                pod_effect(ctx, name)
            return rc, (bytes_written + "\n" if bytes_written is not None else ""), err
        if "rm -f" in script_text:
            if clean_rc == 0:
                ctx.files = getattr(ctx, "files", set()) - {argv[-1]}
            clean_err = f"command terminated with exit code {clean_rc}" if clean_rc else ""
            return clean_rc, "", clean_err
        raise AssertionError(argv)

    return handler


def evict(ctx, name):
    pod = pod_named(ctx, name)
    pod["status"]["phase"] = "Failed"
    pod["status"]["reason"] = "Evicted"


def restart(ctx, name):
    pod = pod_named(ctx, name)
    pod["status"]["containerStatuses"][0]["restartCount"] += 1


def test_fill_tmp_writes_holds_and_cleans_up(fake_clock):
    ctx = FakeCtx(exec_handler=fill_exec(), pods=clean_pods())
    out = run_task("fill-tmp", ctx, seed="f1", size_mi=100, observe="6s")
    check_outputs("fill-tmp", out)
    f = out["filltmp"]
    assert (
        f["applied"] is True
        and f["bytesRequested"] == 100 * 2**20
        and f["bytesWritten"] == 100 * 2**20
    )
    assert f["writeExitCode"] == 0 and f["evicted"] is False and f["restarted"] is False
    assert f["cleanup"] == {"attempted": True, "succeeded": True, "reason": None}
    assert ctx.files == set()  # the file it wrote is gone
    assert f["path"].startswith("/tmp/rw-chaos-") and f["command"] == {
        "binary": "sh",
        "argCount": 5,
    }
    assert sum(fake_clock.slept) >= 6  # the file was held for the observe time
    execs = ctx.verbs("exec")
    assert len(execs) == 2 and execs[0][-2] == f["path"] and execs[0][-1] == "100"


def test_fill_tmp_reports_an_eviction_and_does_not_try_to_clean_a_gone_pod():
    ctx = FakeCtx(exec_handler=fill_exec(pod_effect=evict), pods=clean_pods())
    out = run_task("fill-tmp", ctx, seed="f1", observe="30s")
    f = out["filltmp"]
    assert f["evicted"] is True and f["podPhase"] == "Failed"
    assert f["cleanup"] == {"attempted": False, "succeeded": False, "reason": "pod_evicted"}
    assert len(ctx.verbs("exec")) == 1 and ctx.errors == []


def test_fill_tmp_reports_a_restart_and_still_verifies_the_cleanup():
    ctx = FakeCtx(
        exec_handler=fill_exec(
            pod_effect=restart,
            bytes_written=None,
            rc=137,
            err="command terminated with exit code 137",
        ),
        pods=clean_pods(),
    )
    out = run_task("fill-tmp", ctx, seed="f1")
    f = out["filltmp"]
    assert f["restarted"] is True and f["restartCountAfter"] == 1 and f["bytesWritten"] is None
    assert f["writeExitCode"] == 137 and f["cleanup"]["succeeded"] is True


def test_a_cleanup_that_fails_is_an_error_naming_the_file_and_the_result_survives():
    ctx = FakeCtx(exec_handler=fill_exec(clean_rc=1), pods=clean_pods())
    out = run_task("fill-tmp", ctx, seed="f1", observe="0s")
    f = out["filltmp"]
    assert f["cleanup"] == {"attempted": True, "succeeded": False, "reason": "still_present"}
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("internal", "cleanup")]
    assert f["path"] in ctx.errors[0]["message"]
    assert ctx.files  # and it really is still there


def test_the_cleanup_runs_even_when_the_write_times_out():
    calls = []

    def handler(ctx, argv):
        script_text = argv[argv.index("--") + 3]
        calls.append(script_text)
        if "dd " in script_text:
            raise subprocess.TimeoutExpired(argv, 120)
        return 0, "", ""

    ctx = FakeCtx(exec_handler=handler, pods=clean_pods())
    with pytest.raises(subprocess.TimeoutExpired):
        run_task("fill-tmp", ctx, seed="f1")
    assert any("rm -f" in x for x in calls)  # the finally path


def test_the_cleanup_runs_when_watching_the_pod_fails():
    def named_pod_read_fails(ctx, argv):
        # `get pods <name>` (the watch) fails; the candidate listing `get pods -o json` works
        if not argv[argv.index("pods") + 1].startswith("-"):
            return 1, "", "Unable to connect to the server: dial tcp: i/o timeout"
        return None

    ctx = FakeCtx(
        exec_handler=fill_exec(), pods=clean_pods(), answers=[("get pods ", named_pod_read_fails)]
    )
    with pytest.raises(RwError) as exc:
        run_task("fill-tmp", ctx, seed="f1")
    assert exc.value.code == "unreachable"
    assert ctx.files == set()  # cleanup still ran and verified
    assert len(ctx.verbs("exec")) == 2


def test_a_container_without_sh_is_a_dependency_error_and_nothing_to_clean():
    pods = [p for p in PODS if p["metadata"]["name"].startswith("nosh")]
    err = 'OCI runtime exec failed: exec: "sh": executable file not found in $PATH'
    ctx = FakeCtx("nosh", pods=pods, exec_handler=lambda c, a: (1, "", err))
    with pytest.raises(RwError) as exc:
        run_task("fill-tmp", ctx)
    assert exc.value.code == "dependency"
    assert len(ctx.verbs("exec")) == 1  # no cleanup attempt: nothing was written


def test_an_unwritable_tmp_is_invalid_input_after_a_cleanup_check():
    err = "dd: can't open '/tmp/x': Read-only file system\ncommand terminated with exit code 1"
    ctx = FakeCtx(exec_handler=fill_exec(bytes_written=None, rc=1, err=err), pods=clean_pods())
    with pytest.raises(RwError) as exc:
        run_task("fill-tmp", ctx, seed="f1")
    assert exc.value.code == "invalid_input" and "Read-only" in exc.value.message
    assert len(ctx.verbs("exec")) == 2


def test_a_partial_write_is_a_fact_not_an_error():
    err = "dd: error writing '/tmp/x': No space left on device\ncommand terminated with exit code 1"
    ctx = FakeCtx(
        exec_handler=fill_exec(bytes_written="52428800", rc=1, err=err), pods=clean_pods()
    )
    out = run_task("fill-tmp", ctx, seed="f1", observe="0s")
    f = out["filltmp"]
    assert (
        f["bytesWritten"] == 52428800
        and f["writeExitCode"] == 1
        and "No space left" in f["writeError"]
    )
    assert f["cleanup"]["succeeded"] is True


def test_write_error_text_is_capped_at_1024():
    ctx = FakeCtx(exec_handler=fill_exec(err="x" * 3000), pods=clean_pods())
    f = run_task("fill-tmp", ctx, seed="f1", observe="0s")["filltmp"]
    assert len(f["writeError"]) == 1024 and f["writeErrorTruncated"] is True


def test_fill_tmp_dry_run_runs_nothing():
    ctx = FakeCtx(dry_run=True, exec_handler=fill_exec())
    out = run_task("fill-tmp", ctx, seed="f1")
    check_outputs("fill-tmp", out)
    f = out["filltmp"]
    assert (
        f["applied"] is False and f["bytesWritten"] is None and f["cleanup"]["attempted"] is False
    )
    assert ctx.verbs("exec") == [] and writes(ctx) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size_mi": 0},
        {"size_mi": 1025},
        {"size_mi": "100"},
        {"observe": "5m"},
        {"container": "A b"},
    ],
)
def test_fill_tmp_bad_inputs_fail_before_any_kubectl_call(kwargs):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_task("fill-tmp", ctx, **kwargs)
    assert exc.value.code == "invalid_input" and ctx.calls == []


# ------------------------------------------------------------ mangle-service ----


def spec_of(ctx, name):
    return svc.original_spec(ctx.services[name])


def test_mangle_selector_drops_endpoints_then_restores_and_verifies(fake_clock):
    ctx = FakeCtx()
    original = spec_of(ctx, "web")
    out = run_task("mangle-service", ctx, revert_after="30s")
    check_outputs("mangle-service", out)
    m = out["mangle"]
    s = m["services"][0]
    assert m["applied"] is True and m["mode"] == "selector" and m["serviceCount"] == 1
    assert s["change"] == {"field": "spec.selector.app", "from": "web", "to": "rw-chaos-none"}
    assert (
        s["readyEndpointsBefore"],
        s["readyEndpointsDuring"],
        s["readyEndpointsAfterRestore"],
    ) == (3, 0, 3)
    assert s["endpointsDropped"] is True
    assert s["restore"] == {"attempted": True, "restored": True, "verified": True, "reason": None}
    assert spec_of(ctx, "web") == original
    assert svc.ANNOTATION not in ctx.services["web"]["metadata"].get("annotations", {})
    assert s["heldMs"] >= 30_000
    patches = [a for a in ctx.calls if "patch" in a[:6]]
    assert len(patches) == 2  # mangle, restore


def test_mangle_port_changes_the_port_and_keeps_the_endpoints():
    ctx = FakeCtx("db")
    original = spec_of(ctx, "db")
    out = run_task("mangle-service", ctx, mode="port", revert_after="5s")
    check_outputs("mangle-service", out)
    s = out["mangle"]["services"][0]
    assert s["change"] == {"field": "spec.ports[0].port", "from": 5432, "to": 9999}
    assert s["endpointsDropped"] is False and s["readyEndpointsDuring"] == 2
    assert s["restore"]["verified"] is True and spec_of(ctx, "db") == original


def test_the_service_is_restored_while_mangled_it_really_differs():
    seen = {}

    def spy(ctx, argv):
        annotations = ctx.services["web"]["metadata"].get("annotations") or {}
        if svc.ANNOTATION in annotations:  # only reads made while it is mangled
            seen["selector"] = ctx.services["web"]["spec"]["selector"]
            seen["annotation"] = annotations[svc.ANNOTATION]
        return None

    ctx = FakeCtx(answers=[("endpointslices", spy)])
    run_task("mangle-service", ctx, revert_after="5s")
    assert seen["selector"] == {"app": "rw-chaos-none"}
    assert json.loads(seen["annotation"])["selector"] == {"app": "web"}


def test_a_failed_restore_is_internal_and_carries_the_original_spec():
    def fail_restore(ctx, argv):
        body = json.loads(argv[argv.index("-p") + 1])
        if body["metadata"]["annotations"][svc.ANNOTATION] is None:
            return 1, "", "Error from server (Conflict): the object has been modified"
        return None

    ctx = FakeCtx(answers=[("patch service web", fail_restore)])
    original = spec_of(ctx, "web")
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, revert_after="5s")
    assert exc.value.code == "internal"
    assert svc.spec_json(original) in exc.value.message
    assert "web" in exc.value.message and "could not be restored" in exc.value.message
    assert ctx.services["web"]["spec"]["selector"] == {
        "app": "rw-chaos-none"
    }  # really still mangled


def test_a_restore_that_does_not_verify_is_internal_too():
    def noop_restore(ctx, argv):
        body = json.loads(argv[argv.index("-p") + 1])
        if body["metadata"]["annotations"][svc.ANNOTATION] is None:
            return 0, "service/web patched\n", ""  # accepted, but nothing changed
        return None

    ctx = FakeCtx(answers=[("patch service web", noop_restore)])
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, revert_after="5s")
    assert exc.value.code == "internal" and "still differs" in exc.value.message


def test_the_service_is_restored_when_observing_fails_midway():
    ctx = FakeCtx(
        answers=[
            ("endpointslices", [None, (1, "", "Unable to connect to the server: i/o timeout")])
        ]
    )
    original = spec_of(ctx, "web")
    out = run_task("mangle-service", ctx, revert_after="5s")
    assert spec_of(ctx, "web") == original
    s = out["mangle"]["services"][0]
    assert s["readyEndpointsDuring"] is None and s["restore"]["verified"] is True
    assert {e["scope"] for e in ctx.errors} == {"endpoints"}


def test_the_service_is_restored_when_the_task_is_interrupted(fake_clock):
    def boom(seconds):
        raise KeyboardInterrupt

    ctx = FakeCtx()
    original = spec_of(ctx, "web")
    clock.sleep = boom
    try:
        with pytest.raises(KeyboardInterrupt):
            run_task("mangle-service", ctx, mode="port", revert_after="60s")
    finally:
        clock.sleep = fake_clock.sleep
    assert spec_of(ctx, "web") == original


def test_a_patch_that_errors_after_the_server_applied_it_is_still_restored():
    def applied_then_error(ctx, argv):
        if "--dry-run=server" in argv:
            return None
        body = json.loads(argv[argv.index("-p") + 1])
        if body["metadata"]["annotations"][svc.ANNOTATION] is None:
            return None
        ctx.services["web"] = merge(ctx.services["web"], body)
        return 1, "", "Unable to connect to the server: net/http: TLS handshake timeout"

    ctx = FakeCtx(answers=[("patch service web", applied_then_error)])
    original = spec_of(ctx, "web")
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, revert_after="5s")
    assert exc.value.code == "unreachable"  # the cause, not hidden behind the restore
    assert spec_of(ctx, "web") == original


def test_a_workload_target_mangles_every_service_that_selects_it_and_restores_all():
    services = copy.deepcopy(SERVICES)
    extra = copy.deepcopy(services["web"])
    extra["metadata"]["name"] = "web-admin"
    extra["spec"]["ports"][0]["port"] = 8081
    services["web-admin"] = extra
    ctx = FakeCtx(services=services)
    before = {n: spec_of(ctx, n) for n in services}
    out = run_task("mangle-service", ctx, revert_after="5s")
    names = [s["name"] for s in out["mangle"]["services"]]
    assert names == ["web", "web-admin"]  # not db, nolimit, nosh
    assert all(s["restore"]["verified"] for s in out["mangle"]["services"])
    assert {n: spec_of(ctx, n) for n in services} == before


def test_a_second_service_failing_to_patch_restores_the_first():
    services = copy.deepcopy(SERVICES)
    extra = copy.deepcopy(services["web"])
    extra["metadata"]["name"] = "web-admin"
    services["web-admin"] = extra
    forbidden = (
        1,
        "",
        'Error from server (Forbidden): services "web-admin" is forbidden: cannot patch resource "services"',
    )
    ctx = FakeCtx(services=services, answers=[("patch service web-admin", forbidden)])
    before = spec_of(ctx, "web")
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, revert_after="5s")
    assert exc.value.code == "permission_denied"
    assert spec_of(ctx, "web") == before and svc.ANNOTATION not in ctx.services["web"][
        "metadata"
    ].get("annotations", {})


def test_a_namespace_target_requires_the_service_input():
    ctx = FakeCtx("rw-chaos")
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx)
    assert exc.value.code == "invalid_input" and "service" in exc.value.message
    assert writes(ctx) == []
    out = run_task(
        "mangle-service", FakeCtx("rw-chaos"), service="db", mode="port", revert_after="2s"
    )
    assert out["mangle"]["services"][0]["name"] == "db" and out["target"]["scope"] == "namespace"


def test_a_missing_service_is_not_found_and_nothing_changed():
    ctx = FakeCtx("rw-chaos")
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, service="nope")
    assert exc.value.code == "not_found" and writes(ctx) == []


def test_a_service_input_must_select_the_workload():
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, service="db")
    assert exc.value.code == "invalid_input" and "does not select" in exc.value.message
    assert writes(ctx) == []


def test_a_workload_no_service_selects_is_not_found():
    ctx = FakeCtx()
    ctx.services = {"db": SERVICES["db"]}
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx)
    assert exc.value.code == "not_found"


def test_more_than_five_matching_services_need_the_service_input():
    services = {}
    for i in range(6):
        s = copy.deepcopy(SERVICES["web"])
        s["metadata"]["name"] = f"web-{i}"
        services[s["metadata"]["name"]] = s
    ctx = FakeCtx(services=services)
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx)
    assert exc.value.code == "invalid_input" and writes(ctx) == []


def test_a_service_already_mangled_by_a_dead_run_is_refused():
    services = copy.deepcopy(SERVICES)
    services["web"]["metadata"].setdefault("annotations", {})[svc.ANNOTATION] = (
        '{"selector":{"app":"web"},"ports":[]}'
    )
    ctx = FakeCtx(services=services)
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx)
    assert exc.value.code == "invalid_input" and writes(ctx) == []


def test_mangle_dry_run_validates_the_patch_server_side_and_changes_nothing(fake_clock):
    ctx = FakeCtx(dry_run=True)
    before = copy.deepcopy(ctx.services)
    out = run_task("mangle-service", ctx, revert_after="5m")
    check_outputs("mangle-service", out)
    m = out["mangle"]
    s = m["services"][0]
    assert (
        m["applied"] is False
        and s["readyEndpointsBefore"] == 3
        and s["readyEndpointsDuring"] is None
    )
    assert s["restore"]["attempted"] is False and s["endpointsDropped"] is None
    assert ctx.services == before and fake_clock.slept == []
    assert writes(ctx) == []
    (patch,) = [a for a in ctx.calls if "patch" in a[:6]]
    assert "--dry-run=server" in patch


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "both"},
        {"revert_after": "11m"},
        {"revert_after": "0s"},
        {"revert_after": "x"},
        {"service": "Bad_Name"},
    ],
)
def test_mangle_bad_inputs_fail_before_any_kubectl_call(kwargs):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, **kwargs)
    assert exc.value.code == "invalid_input" and ctx.calls == []


# ------------------------------------------------- errors every task shares ----


@pytest.mark.parametrize("task", list(MAIN))
def test_a_missing_workload_is_not_found_and_changes_nothing(task):
    ctx = FakeCtx()
    ctx.resource.name = "nope"
    with pytest.raises(RwError) as exc:
        run_task(task, ctx)
    assert exc.value.code == "not_found" and writes(ctx) == []


@pytest.mark.parametrize("task", list(MAIN))
def test_a_missing_namespace_is_not_found(task):
    ctx = FakeCtx("rw-chaos")
    ctx.resource.name = "nope"
    with pytest.raises(RwError) as exc:
        run_task(task, ctx, **({"service": "web"} if task == "mangle-service" else {}))
    assert exc.value.code == "not_found" and writes(ctx) == []


@pytest.mark.parametrize(
    "task,needle",
    [
        ("delete-pods", "get deployments.apps web"),
        ("delete-pods", "get replicasets.apps"),
        ("delete-pods", "get pods"),
        ("oomkill-pod", "get deployments.apps web"),
        ("oomkill-pod", "get replicasets.apps"),
        ("oomkill-pod", "get pods"),
        ("fill-tmp", "get deployments.apps web"),
        ("fill-tmp", "get replicasets.apps"),
        ("fill-tmp", "get pods"),
        ("mangle-service", "get deployments.apps web"),
        ("mangle-service", "get services"),
    ],
)
def test_a_forbidden_read_raises_permission_denied_and_changes_nothing(task, needle):
    ctx = FakeCtx(answers=[(needle, FORBIDDEN)])
    with pytest.raises(RwError) as exc:
        run_task(task, ctx)
    assert exc.value.code == "permission_denied" and writes(ctx) == []


@pytest.mark.parametrize("needle", ["get services", "get namespaces"])
def test_mangle_forbidden_reads(needle):
    ctx = FakeCtx("rw-chaos", answers=[(needle, FORBIDDEN)])
    if needle == "get namespaces":
        # the Namespace read degrades to an error on the result; the service read then decides
        out = run_task("mangle-service", ctx, service="db", mode="port", revert_after="2s")
        assert [(e["code"], e["scope"]) for e in ctx.errors] == [
            ("permission_denied", "namespaces")
        ]
        assert out["mangle"]["services"][0]["restore"]["verified"] is True
        return
    with pytest.raises(RwError) as exc:
        run_task("mangle-service", ctx, service="db")
    assert exc.value.code == "permission_denied" and writes(ctx) == []


def test_mangle_forbidden_endpoint_read_is_a_scoped_error_not_a_failure():
    ctx = FakeCtx(answers=[("endpointslices", FORBIDDEN)])
    out = run_task("mangle-service", ctx, revert_after="2s")
    s = out["mangle"]["services"][0]
    assert s["readyEndpointsBefore"] is None and s["endpointsDropped"] is None
    assert s["restore"]["verified"] is True
    assert {e["scope"] for e in ctx.errors} == {"endpoints"} and ctx.errors[0][
        "code"
    ] == "permission_denied"


def test_the_namespace_read_denial_is_recorded_and_pods_decide():
    ctx = FakeCtx("rw-chaos", dry_run=True, answers=[("get namespaces", FORBIDDEN)])
    out = run_task("delete-pods", ctx, seed="s1")
    assert out["deletion"]["wouldDelete"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]


def test_unreachable_api_server_is_unreachable():
    ctx = FakeCtx(
        answers=[
            (
                "get deployments.apps",
                (1, "", "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"),
            )
        ]
    )
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx)
    assert exc.value.code == "unreachable"


def test_a_target_type_the_skill_does_not_apply_to_is_refused_before_kubectl():
    ctx = FakeCtx()
    ctx.resource.type = "kubernetes/daemonset"
    ctx.resource.path = None
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_the_real_runner_raises_on_non_zero_so_every_call_must_pass_check_false():
    # FakeCtx.run raises FakeCommandError on non-zero unless check=False, like the SDK; a call
    # that forgot check=False would surface here as an internal error instead of the mapped code
    ctx = FakeCtx(
        answers=[("get pods", (1, "", 'Error from server (NotFound): pods "x" not found'))]
    )
    with pytest.raises(RwError) as exc:
        run_task("delete-pods", ctx)
    assert exc.value.code == "not_found"


# --------------------------------------------------------------- permissions ----

RESOURCES = {
    "namespaces": ("", "namespaces"),
    "deployments.apps": ("apps", "deployments"),
    "statefulsets.apps": ("apps", "statefulsets"),
    "replicasets.apps": ("apps", "replicasets"),
    "pods": ("", "pods"),
    "services": ("", "services"),
    "service": ("", "services"),
    "endpointslices.discovery.k8s.io": ("discovery.k8s.io", "endpointslices"),
}
VALUE_FLAGS = {"-n", "-o", "-c", "-l", "-p", "--type"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l) -> list; delete -> delete;
    patch -> patch; exec -> pods get + pods/exec create and get. Raises on anything else,
    so a skill that issues a new verb has to extend this mapping.
    """
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
        named = len(positional) > 2 and "-l" not in flags
        return {(group, plural, "get" if named else "list")}
    if verb == "delete":
        assert positional[1] == "pod"
        return {("", "pods", "delete")}
    if verb == "patch":
        group, plural = RESOURCES[positional[1]]
        return {(group, plural, "patch")}
    if verb == "exec":
        return {("", "pods", "get"), ("", "pods/exec", "create"), ("", "pods/exec", "get")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, r, v)
        for rule in TASKS[task]["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def every_call(task):
    """The kubectl argvs the task issues on each target form, real and dry."""
    forms = []
    if task == "mangle-service":
        forms += [
            (FakeCtx(), {"revert_after": "2s"}),
            (FakeCtx(dry_run=True), {}),
            (FakeCtx("db"), {"mode": "port", "revert_after": "2s"}),
            (FakeCtx("rw-chaos"), {"service": "db", "revert_after": "2s"}),
        ]
    elif task == "oomkill-pod":
        forms += [
            (FakeCtx(exec_handler=oom_exec(), pods=clean_pods()), {"seed": "o1"}),
            (FakeCtx("db", exec_handler=oom_exec(), pods=clean_pods()), {"seed": "o1"}),
            (FakeCtx("rw-chaos", exec_handler=oom_exec(), pods=clean_pods()), {"seed": "o1"}),
            (FakeCtx(dry_run=True), {}),
        ]
    elif task == "fill-tmp":
        forms += [
            (FakeCtx(exec_handler=fill_exec(), pods=clean_pods()), {"seed": "f1", "observe": "2s"}),
            (
                FakeCtx("db", exec_handler=fill_exec(), pods=clean_pods()),
                {"seed": "f1", "observe": "0s"},
            ),
            (
                FakeCtx("rw-chaos", exec_handler=fill_exec(), pods=clean_pods()),
                {"seed": "f1", "observe": "0s"},
            ),
            (FakeCtx("db", dry_run=True), {}),
        ]
    else:
        forms += [
            (FakeCtx(), {"seed": "s1"}),
            (FakeCtx("db"), {"seed": "s1"}),
            (FakeCtx("rw-chaos"), {"seed": "s1"}),
            (FakeCtx(dry_run=True), {}),
        ]
    for ctx, inputs in forms:
        run_task(task, ctx, **inputs)
        yield from ctx.calls


@pytest.mark.parametrize("task", list(TASKS))
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task):
    needed = set()
    for argv in every_call(task):
        needed |= rbac_needs(argv)
    missing = needed - declared(task)
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"
    extra = declared(task) - needed
    # least privilege: nothing declared that no call needs (replicasets is the Deployment form only)
    assert not extra, f"{task} declares {sorted(extra)} that no call uses"


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-o", "json"]) == {("", "pods", "list")}
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "-l", "a=b", "-o", "json"]) == {
        ("", "pods", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", "pods", "p", "-o", "json"]) == {
        ("", "pods", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "delete", "pod", "a", "b", "--wait=false"]) == {
        ("", "pods", "delete")
    }
    assert rbac_needs(
        ["kubectl", "-n", "x", "patch", "service", "s", "--type=merge", "-p", "{}"]
    ) == {("", "services", "patch")}


# ------------------------------------------------------------- the files ----


def test_every_task_is_a_declared_destructive_write_task_that_supports_dry_run():
    assert list(TASKS) == ["delete-pods", "oomkill-pod", "mangle-service", "fill-tmp"]
    for name, t in TASKS.items():
        assert t["readOnly"] is False and t["destructive"] is True and t["dryRun"] is True, name
        assert t["effects"] and all(isinstance(e, str) and e for e in t["effects"]), name
        assert "idempotent" in t and t["permissions"], name
        assert not {"status", "errors"} & set(t["outputs"]), name
        assert "target" in t["outputs"], name
    assert TASKS["mangle-service"]["idempotent"] is False


def test_script_parameters_are_the_task_inputs_in_snake_case():
    def snake(name):
        return "".join("_" + ch.lower() if ch.isupper() else ch for ch in name)

    for name, t in TASKS.items():
        params = list(inspect.signature(MAIN[name]).parameters)[1:]
        assert params == [snake(i) for i in t["inputs"]], name


def test_task_timeouts_cover_the_longest_wait_of_each_task():
    def seconds(text):
        return int(text.rstrip("s"))

    assert seconds(TASKS["delete-pods"]["timeout"]) > 300 + 30
    assert seconds(TASKS["mangle-service"]["timeout"]) > 600 + 20 + 20 + 60
    assert seconds(TASKS["oomkill-pod"]["timeout"]) > 120 + 120
    assert seconds(TASKS["fill-tmp"]["timeout"]) > 120 + 120 + 60


def test_tasks_and_skill_validate_against_the_contracts():
    contracts = pytest.importorskip("runwhen_capability.contracts")
    assert (
        contracts.validate("tasks", contracts.load_yaml((SKILL / "tasks.yaml").read_text())) == []
    )
    front = contracts.split_frontmatter((SKILL / "SKILL.md").read_text())[0]
    assert contracts.validate("skill-frontmatter", front) == []
    assert front["runwhen"]["appliesTo"]["types"] == [
        "kubernetes/namespace",
        "kubernetes/deployment",
        "kubernetes/statefulset",
    ]
    assert len(front["description"]) <= 1024


def test_each_permission_is_an_rbac_rule_and_each_schema_is_valid():
    rbac = json.loads((SKILL.parents[1] / "integration" / "schemas" / "rbac-rule.json").read_text())
    for name, t in TASKS.items():
        for rule in t["permissions"]:
            assert not list(Draft202012Validator(rbac).iter_errors(rule)), (name, rule)
        for out in t["outputs"].values():
            Draft202012Validator.check_schema(json.loads((SKILL / out["schema"]).read_text()))


def test_skill_md_is_short_and_has_the_sections():
    text = (SKILL / "SKILL.md").read_text()
    assert len(text.splitlines()) < 150
    for heading in (
        "## When to use",
        "## What each task tells you",
        "## How to read the result",
        "## What to try next",
    ):
        assert heading in text


def test_scripts_never_print_or_return_environment_or_secrets():
    for path in list((SKILL / "scripts").glob("*.py")) + list(
        (SKILL / "lib" / "k8s_chaos").glob("*.py")
    ):
        text = path.read_text()
        for needle in ("os.environ", "getenv", "print(", "2>/dev/null", "|| true"):
            assert needle not in text, (path.name, needle)
