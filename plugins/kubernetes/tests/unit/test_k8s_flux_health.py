"""Unit tests for k8s-flux-health: lib/ and the thin scripts, against JSON captured from the
kind-rw-plugins scenario in namespace rw-flux (synthetic Flux CRDs, status set the way a
controller would). No test calls a cluster: scripts run against a fake ctx that answers kubectl
from the captured files and records every call."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_flux_health import fetch
from k8s_flux_health import flux as f
from k8s_flux_health import k8s
from k8s_flux_health.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-flux-health"
DATA = Path(__file__).parent / "data" / "k8s_flux_health"
NS = "rw-flux"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


HRS, KSS = load("helmreleases"), load("kustomizations")
HR_OK, HR_FAILED = named(HRS, "podinfo-ok"), named(HRS, "podinfo-failed")
HR_SUSPENDED, HR_FRESH = named(HRS, "podinfo-suspended"), named(HRS, "podinfo-fresh")
KS_OK, KS_BROKEN = named(KSS, "apps-ok"), named(KSS, "apps-broken")
KS_SUSPENDED = named(KSS, "apps-suspended")

HR_RES = "helmreleases.helm.toolkit.fluxcd.io"
KS_RES = "kustomizations.kustomize.toolkit.fluxcd.io"
TYPES = {"HelmRelease": "kubernetes/helmrelease", "Kustomization": "kubernetes/kustomization"}
FORBIDDEN = 'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-healthy:rw-viewer" cannot get resource "%s" in API group "x" in the namespace "rw-flux"'


def schema(name):
    return json.loads((SKILL / "schemas" / f"{name}.v1.json").read_text())


def check(outputs, **by_output):
    """Every output validates against its declared schema."""
    for name, value in outputs.items():
        Draft202012Validator(schema(by_output.get(name, name))).validate(value)


# ------------------------------------------------------------ fake ctx ----


class FakeCommandError(Exception):
    """What the v13 runner raises for a non-zero exit when check is true."""

    def __init__(self, argv, returncode, stderr):
        super().__init__(f"{argv[0]} exited {returncode}: {stderr}")
        self.code = "internal"


class FakeCtx:
    """What a script sees: resource, settings, dry_run, error(), run().

    kubectl get serves the captured objects (a copy, so annotate and patch change what a later get
    returns); `answers` are (substring of the joined argv, (rc, stdout, stderr)) checked first.
    `on_get` is called with (object, number of gets of it so far) and may change the object, as a
    controller would. Like the SDK, a non-zero exit raises unless check=False.
    """

    def __init__(self, doc, answers=(), dry_run=False, on_get=None, rtype=None, max_items=200):
        self.docs = {
            (d["kind"], d["metadata"]["name"]): copy.deepcopy(d)
            for d in (*HRS["items"], *KSS["items"])
        }
        self.kind = doc["kind"]
        self.name = doc["metadata"]["name"]
        self.resource = SimpleNamespace(
            namespace=NS, name=self.name, type=rtype or TYPES[self.kind]
        )
        self.settings = SimpleNamespace(max_items=max_items)
        self.dry_run = dry_run
        self.answers = list(answers)
        self.on_get = on_get
        self.gets = {}
        self.calls = []
        self.errors = []

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message})

    def obj(self, name=None, kind=None):
        return self.docs[(kind or self.kind, name or self.name)]

    def run(self, argv, check=True, **_):
        self.calls.append(argv)
        joined = " ".join(argv)
        result = self._answer(argv, joined)
        if check and result[0]:
            raise FakeCommandError(argv, result[0], result[2])
        return SimpleNamespace(returncode=result[0], stdout=result[1], stderr=result[2])

    def _answer(self, argv, joined):
        for needle, answer in self.answers:
            if needle in joined:
                return answer
        words = [a for a in argv[1:] if not a.startswith("-")]
        verb = next(w for w in words if w in ("get", "patch", "annotate"))
        kind = "HelmRelease" if HR_RES in argv else "Kustomization"
        assert HR_RES in argv or KS_RES in argv, argv
        name = argv[argv.index(HR_RES if kind == "HelmRelease" else KS_RES) + 1]
        if verb == "get":
            if (kind, name) not in self.docs:
                return 1, "", f'Error from server (NotFound): {kind.lower()}s "{name}" not found'
            doc = self.docs[(kind, name)]
            n = self.gets[(kind, name)] = self.gets.get((kind, name), 0) + 1
            if self.on_get:
                self.on_get(doc, n)
            return 0, json.dumps(doc), ""
        dry = "--dry-run=server" in argv
        doc = self.docs[(kind, name)]
        if verb == "annotate" and not dry:
            key, _, value = next(a for a in argv if "=" in a and "/" in a).partition("=")
            doc["metadata"].setdefault("annotations", {})[key] = value
        if verb == "patch" and not dry:
            patch = json.loads(argv[argv.index("-p") + 1])
            doc["spec"].update(patch["spec"])
        return 0, f"{kind.lower()}.x/{name} patched\n", ""


def script(name):
    spec = importlib.util.spec_from_file_location(f"flux_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


class Clock:
    """A fake time: sleep() advances monotonic(), so waits take no real time."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(fetch, "time", c)
    return c


# ------------------------------------------------------------ helpers ----


def test_parse_duration():
    assert f.parse_duration("90s") == 90
    assert f.parse_duration("1m30s") == 90
    assert f.parse_duration("500ms") == 0.5
    assert f.parse_duration("0s") == 0
    for bad in ("", "soon", "10", "5x"):
        with pytest.raises(RwError) as exc:
            f.parse_duration(bad)
        assert exc.value.code == "invalid_input"


def test_resolve_kind_from_type_then_path():
    assert f.resolve_kind("kubernetes/helmrelease") == "HelmRelease"
    assert f.resolve_kind("kubernetes/kustomization") == "Kustomization"
    path = "kubernetes/clusters/kind/namespaces/rw-flux/kustomizations/apps-ok"
    assert f.resolve_kind(None, path) == "Kustomization"
    with pytest.raises(RwError) as exc:
        f.resolve_kind("kubernetes/deployment")
    assert exc.value.code == "invalid_input"
    with pytest.raises(RwError):
        f.resolve_kind(None, "kubernetes/clusters/kind/namespaces/x/deployments/web")


# ------------------------------------------------------- ready-status ----


def test_failed_helmrelease_reports_ready_false_stalled_and_reason():
    s = f.state_view("HelmRelease", HR_FAILED)
    assert s["ready"] == "False" and s["readyReason"] == "UpgradeFailed"
    assert s["stalled"] == "True" and s["stalledReason"] == "RetriesExceeded"
    assert s["reconciling"] is None  # absent condition: null, not "False"
    assert s["suspended"] is False and s["generationObserved"] is True
    assert [c["type"] for c in s["conditions"]] == ["Ready", "Released", "Stalled"]
    assert "timed out" in s["conditions"][0]["message"]


def test_healthy_helmrelease_is_ready_and_not_stalled():
    s = f.state_view("HelmRelease", HR_OK)
    assert (s["ready"], s["stalled"], s["reconciling"]) == ("True", None, None)
    assert s["readyReason"] == "UpgradeSucceeded"


def test_object_without_status_has_no_ready_condition_rather_than_ready_false():
    s = f.state_view("HelmRelease", HR_FRESH)
    assert s["ready"] is None and s["conditions"] == [] and s["conditionCount"] == 0
    assert s["generationObserved"] is None  # nothing observed yet


def test_stalled_kustomization_without_a_reconciling_condition_is_still_reported():
    # Classic "List Unready Kustomizations" built each entry from the Ready AND the Reconciling
    # condition with jq object construction: a Ready=False Kustomization with no Reconciling
    # condition produced no entry and was never reported.
    s = f.state_view("Kustomization", KS_BROKEN)
    assert s["ready"] == "False" and s["readyReason"] == "BuildFailed"
    assert s["stalled"] == "True" and s["reconciling"] is None


def test_generation_not_observed_while_the_controller_is_behind():
    obj = copy.deepcopy(KS_OK)
    obj["metadata"]["generation"] = 4
    s = f.state_view("Kustomization", obj)
    assert s["generation"] == 4 and s["observedGeneration"] == 1
    assert s["generationObserved"] is False


def test_suspended_flag_comes_from_spec():
    assert f.state_view("HelmRelease", HR_SUSPENDED)["suspended"] is True
    assert f.state_view("HelmRelease", HR_OK)["suspended"] is False


def test_helmrelease_v2_applied_revision_is_the_newest_deployed_history_entry():
    r = f.revisions_view("HelmRelease", HR_FAILED)
    # history[0] is the failed 6.6.0 attempt; what is deployed is 6.5.1
    assert r["lastAttemptedRevision"] == "6.6.0"
    assert r["lastAppliedRevision"] == "6.5.1" and r["appliedSource"] == "status.history"
    assert r["revisionsDiffer"] is True and r["neverApplied"] is False
    helm = r["helm"]
    assert helm["chart"] == "podinfo" and helm["chartVersion"] == "6.6.0"
    assert helm["source"] == {"kind": "HelmRepository", "name": "podinfo", "namespace": NS}
    assert [h["status"] for h in helm["history"]] == ["failed", "deployed"]
    assert helm["history"][0]["chartVersion"] == "6.6.0" and helm["upgradeFailures"] == 5
    assert r["kustomization"] is None


def test_helmrelease_in_sync_has_equal_revisions():
    r = f.revisions_view("HelmRelease", HR_OK)
    assert r["lastAppliedRevision"] == r["lastAttemptedRevision"] == "6.5.1"
    assert r["revisionsDiffer"] is False
    assert r["lastHandledReconcileAt"] == "2026-11-03T20:00:00Z"


def test_helmrelease_beta_api_status_field_wins_over_history():
    obj = copy.deepcopy(HR_FAILED)
    obj["status"]["lastAppliedRevision"] = "6.4.0"
    r = f.revisions_view("HelmRelease", obj)
    assert (
        r["lastAppliedRevision"] == "6.4.0" and r["appliedSource"] == "status.lastAppliedRevision"
    )


def test_release_that_never_applied_anything_is_not_reported_as_in_sync():
    # Classic "Fetch Mismatched FluxCD HelmRelease Version" compared lastApplied != lastAttempted:
    # a release with both null compared equal and was fine.
    r = f.revisions_view("HelmRelease", HR_FRESH)
    assert r["lastAppliedRevision"] is None and r["lastAttemptedRevision"] is None
    assert r["revisionsDiffer"] is False and r["neverApplied"] is True
    # attempted but never applied (a first install that fails)
    obj = copy.deepcopy(HR_FRESH)
    obj["status"] = {"lastAttemptedRevision": "1.0.0", "history": []}
    r = f.revisions_view("HelmRelease", obj)
    assert r["revisionsDiffer"] is True and r["neverApplied"] is True


def test_helmrelease_chart_ref_source_is_reported():
    helm = f.revisions_view("HelmRelease", HR_FRESH)["helm"]
    assert helm["chart"] is None
    assert helm["source"] == {"kind": "OCIRepository", "name": "podinfo", "namespace": NS}


def test_kustomization_revisions_source_and_path():
    r = f.revisions_view("Kustomization", KS_BROKEN)
    assert r["lastAppliedRevision"].startswith("main@sha1:aaaa")
    assert r["lastAttemptedRevision"].startswith("main@sha1:bbbb")
    assert r["revisionsDiffer"] is True and r["appliedSource"] == "status.lastAppliedRevision"
    ks = r["kustomization"]
    assert ks["path"] == "./apps/broken"
    assert ks["source"] == {"kind": "GitRepository", "name": "fleet", "namespace": "flux-system"}
    assert ks["inventoryCount"] is None  # nothing recorded, not zero
    assert f.revisions_view("Kustomization", KS_OK)["kustomization"]["inventoryCount"] == 2
    assert r["helm"] is None


def test_kustomization_in_sync():
    r = f.revisions_view("Kustomization", KS_OK)
    assert r["revisionsDiffer"] is False and r["neverApplied"] is False


def test_dependency_refs_default_to_the_objects_namespace():
    assert f.dependency_refs(HR_FAILED) == [
        ("podinfo-ok", NS),
        ("not-here", NS),
        ("infra", "rw-flux-other"),
    ]
    assert f.dependency_refs(HR_OK) == []


def test_conditions_bounded_and_messages_truncated():
    obj = copy.deepcopy(HR_OK)
    obj["status"]["conditions"] = [
        {"type": f"T{i}", "status": "True", "message": "x" * 3000} for i in range(80)
    ]
    s = f.state_view("HelmRelease", obj)
    assert len(s["conditions"]) == 50 and s["conditionCount"] == 80
    assert len(s["conditions"][0]["message"]) == 1024 and s["conditions"][0]["truncated"] is True
    short = f.state_view("HelmRelease", HR_OK)["conditions"][0]
    assert short["truncated"] is False


def test_history_is_capped_at_fifty():
    obj = copy.deepcopy(HR_OK)
    obj["status"]["history"] = [
        {"name": "x", "version": i, "chartVersion": "1", "status": "superseded"} for i in range(70)
    ]
    helm = f.revisions_view("HelmRelease", obj)["helm"]
    assert len(helm["history"]) == 50 and helm["historyCount"] == 70


def test_target_view_names_the_flux_object_that_manages_it():
    obj = copy.deepcopy(HR_OK)
    obj["metadata"]["labels"] = {
        "kustomize.toolkit.fluxcd.io/name": "apps-ok",
        "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
    }
    assert f.target_view("HelmRelease", obj)["managedBy"] == {
        "kind": "Kustomization",
        "name": "apps-ok",
        "namespace": "flux-system",
    }
    assert f.target_view("HelmRelease", HR_OK)["managedBy"] is None


def test_request_timestamp_differs_within_the_same_second():
    from datetime import datetime, timezone

    a = f.request_timestamp(datetime(2026, 11, 3, 20, 0, 0, 1, tzinfo=timezone.utc))
    b = f.request_timestamp(datetime(2026, 11, 3, 20, 0, 0, 2, tzinfo=timezone.utc))
    assert a == "2026-11-03T20:00:00.000001Z" and a != b


# --------------------------------------------------- ready-status script ----


def test_ready_status_script_on_the_failed_helmrelease():
    ctx = FakeCtx(HR_FAILED)
    out = script("ready_status")(ctx)
    check(out)
    assert out["state"]["ready"] == "False" and out["state"]["stalled"] == "True"
    assert out["revisions"]["revisionsDiffer"] is True
    deps = {d["name"]: d for d in out["dependencies"]["items"]}
    assert out["dependencies"]["count"] == 3 and out["dependencies"]["truncated"] is False
    assert deps["podinfo-ok"]["read"] is True and deps["podinfo-ok"]["ready"] == "True"
    assert deps["not-here"]["read"] is True and deps["not-here"]["exists"] is False
    assert deps["not-here"]["ready"] is None and "does not exist" in deps["not-here"]["note"]
    assert ctx.errors == []


def test_dependency_in_another_namespace_is_reported_unread_and_never_fetched():
    ctx = FakeCtx(HR_FAILED)
    out = script("ready_status")(ctx)
    other = next(d for d in out["dependencies"]["items"] if d["name"] == "infra")
    assert other["namespace"] == "rw-flux-other"
    assert other["read"] is False and other["exists"] is None and other["ready"] is None
    assert "rw-flux-other" in other["note"]
    assert not any("infra" in c for c in ctx.calls)
    assert not any("rw-flux-other" in c for c in ctx.calls)


def test_ready_status_script_on_the_broken_kustomization():
    ctx = FakeCtx(KS_BROKEN)
    out = script("ready_status")(ctx)
    check(out)
    assert out["state"]["readyReason"] == "BuildFailed" and out["state"]["reconciling"] is None
    assert out["revisions"]["kustomization"]["path"] == "./apps/broken"
    assert [d["name"] for d in out["dependencies"]["items"] if d["read"]] == ["apps-ok"]


def test_ready_status_script_on_healthy_objects_is_clean():
    for doc in (HR_OK, KS_OK):
        ctx = FakeCtx(doc)
        out = script("ready_status")(ctx)
        check(out)
        assert out["state"]["ready"] == "True" and out["state"]["stalled"] is None
        assert out["revisions"]["revisionsDiffer"] is False
        assert out["dependencies"] == {"count": 0, "truncated": False, "items": []}


def test_dependency_that_is_not_ready_is_reported_with_its_reason():
    ctx = FakeCtx(HR_FAILED)
    ctx.obj("podinfo-ok")["status"]["conditions"][0].update(status="False", reason="InstallFailed")
    ctx.obj("podinfo-ok")["spec"]["suspend"] = True
    out = script("ready_status")(ctx)
    dep = out["dependencies"]["items"][0]
    assert dep["ready"] == "False" and dep["readyReason"] == "InstallFailed"
    assert dep["suspended"] is True


def test_forbidden_dependency_read_is_one_error_and_the_rest_unread():
    ctx = FakeCtx(HR_FAILED, answers=[("podinfo-ok", (1, "", FORBIDDEN % (HR_RES, HR_RES)))])
    out = script("ready_status")(ctx)
    check(out)
    # the object's own facts survive
    assert out["state"]["ready"] == "False"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "dependencies")]
    deps = out["dependencies"]["items"]
    assert [d["read"] for d in deps] == [False, False, False]
    assert deps[0]["note"] == "not read: permission denied" and deps[0]["ready"] is None
    assert deps[1]["note"] == "not read: permission denied"  # not asked again
    # one dependency read was refused; the other in-namespace dependency was not asked after that
    assert sum("podinfo-ok" in " ".join(c) or "not-here" in " ".join(c) for c in ctx.calls) == 1


def test_dependency_list_is_capped_at_fifty_but_counted():
    ctx = FakeCtx(HR_OK)
    ctx.obj()["spec"]["dependsOn"] = [
        {"name": f"d{i}", "namespace": "elsewhere"} for i in range(70)
    ]
    out = script("ready_status")(ctx)
    check(out)
    assert len(out["dependencies"]["items"]) == 50
    assert out["dependencies"]["count"] == 70 and out["dependencies"]["truncated"] is True


def test_max_items_caps_the_dependency_list():
    ctx = FakeCtx(HR_FAILED, max_items=2)
    out = script("ready_status")(ctx)
    assert len(out["dependencies"]["items"]) == 2 and out["dependencies"]["count"] == 3


# ------------------------------------------------------ error handling ----


@pytest.mark.parametrize(
    "rc,text,code",
    [
        (
            1,
            "Error from server (Forbidden): helmreleases is forbidden: cannot get resource",
            "permission_denied",
        ),
        (
            1,
            'Error from server (NotFound): helmreleases.helm.toolkit.fluxcd.io "x" not found',
            "not_found",
        ),
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
        (1, 'error: the server doesn\'t have a resource type "helmreleases"', "dependency"),
        (1, "boom", "internal"),
    ],
)
def test_classify(rc, text, code):
    assert k8s.classify(rc, text) == code


def test_kubectl_json_raises_rw_error_for_a_non_zero_exit_that_ctx_run_did_not_raise():
    class Done:
        returncode, stdout, stderr = 1, "", FORBIDDEN % ("x", "x")

    class RunCtx:
        def run(self, argv, **kwargs):
            return Done()

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(RunCtx(), "get", "pods")
    assert exc.value.code == "permission_denied"


def test_missing_object_raises_not_found_in_every_task():
    for name, kwargs in (("ready_status", {}), ("reconcile", {}), ("suspend", {}), ("resume", {})):
        ctx = FakeCtx(HR_OK, dry_run=True)
        ctx.resource.name = "nope"
        with pytest.raises(RwError) as exc:
            script(name)(ctx, **kwargs)
        assert exc.value.code == "not_found", name


def test_forbidden_target_raises_permission_denied_in_every_task():
    for name in ("ready_status", "reconcile", "suspend", "resume"):
        ctx = FakeCtx(
            HR_FAILED, dry_run=True, answers=[("get", (1, "", FORBIDDEN % (HR_RES, HR_RES)))]
        )
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "permission_denied", name
        assert len(ctx.calls) == 1  # nothing written, nothing else asked


def test_unreachable_api_server_raises_in_every_task():
    down = "Unable to connect to the server: dial tcp 10.0.0.1:443: connect: connection refused"
    for name in ("ready_status", "reconcile", "suspend", "resume"):
        ctx = FakeCtx(KS_OK, dry_run=True, answers=[("get", (1, "", down))])
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "unreachable", name


def test_missing_flux_crd_is_a_dependency_error():
    ctx = FakeCtx(
        HR_OK,
        answers=[
            ("get", (1, "", 'error: the server doesn\'t have a resource type "helmreleases"'))
        ],
    )
    with pytest.raises(RwError) as exc:
        script("ready_status")(ctx)
    assert exc.value.code == "dependency"


def test_wrong_target_type_is_refused_before_any_kubectl_call():
    for name in ("ready_status", "reconcile", "suspend", "resume"):
        ctx = FakeCtx(HR_OK, rtype="kubernetes/deployment")
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_target_type_falls_back_to_the_path():
    ctx = FakeCtx(KS_OK)
    ctx.resource = SimpleNamespace(
        namespace=NS,
        name="apps-ok",
        type=None,
        path="kubernetes/clusters/kind/namespaces/rw-flux/kustomizations/apps-ok",
    )
    assert script("ready_status")(ctx)["state"]["kind"] == "Kustomization"


def test_get_uses_the_fully_qualified_resource_name():
    for doc, resource in ((HR_OK, HR_RES), (KS_OK, KS_RES)):
        ctx = FakeCtx(doc)
        script("ready_status")(ctx)
        assert resource in ctx.calls[0]


# ---------------------------------------------------------- reconcile ----


def annotate_calls(ctx):
    return [c for c in ctx.calls if "annotate" in c]


def test_reconcile_dry_run_sends_a_server_side_dry_run_and_waits_for_nothing():
    ctx = FakeCtx(HR_OK, dry_run=True)
    out = script("reconcile")(ctx)
    check(out)
    (call,) = annotate_calls(ctx)
    assert "--dry-run=server" in call and "--overwrite" in call
    assert any(a.startswith("reconcile.fluxcd.io/requestedAt=") for a in call)
    r = out["reconcile"]
    assert r["applied"] is False and r["wouldApply"] is True and r["requestedAt"] is None
    assert r["completed"] is None and r["completionReason"] == "dry_run" and r["after"] is None
    assert "reconcile.fluxcd.io/requestedAt" not in (ctx.obj()["metadata"].get("annotations") or {})
    assert len(ctx.calls) == 2  # get, annotate: no polling


def test_reconcile_completes_when_the_controller_echoes_the_request(clock):
    def controller(doc, n):  # handled on the third look
        if n == 3:
            doc["status"]["lastHandledReconcileAt"] = doc["metadata"]["annotations"][
                "reconcile.fluxcd.io/requestedAt"
            ]

    ctx = FakeCtx(HR_FAILED, on_get=controller)
    out = script("reconcile")(ctx, timeout="30s")
    check(out)
    r = out["reconcile"]
    assert r["applied"] is True and r["completed"] is True and r["completionReason"] == "handled"
    assert (
        r["requestedAt"] == ctx.obj()["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"]
    )
    assert r["lastHandledBefore"] == "2026-11-03T20:05:00Z"
    assert r["waitedSeconds"] == fetch.POLL_SECONDS  # one sleep between the second and third look
    # handled is not succeeded: the Ready condition is reported as the controller left it
    assert r["after"]["ready"] == "False" and r["after"]["readyReason"] == "UpgradeFailed"
    assert r["after"]["lastHandledReconcileAt"] == r["requestedAt"]
    assert ctx.settings.max_items == 200


def test_reconcile_timeout_is_a_result_not_a_failure(clock):
    ctx = FakeCtx(KS_OK)
    out = script("reconcile")(ctx, timeout="5s")
    check(out)
    r = out["reconcile"]
    assert r["applied"] is True and r["completed"] is False and r["completionReason"] == "timeout"
    assert 5 <= r["waitedSeconds"] < 5 + fetch.POLL_SECONDS
    assert r["after"]["ready"] == "True"
    assert ctx.errors == []


def test_reconcile_with_zero_timeout_sends_the_request_and_does_not_wait():
    ctx = FakeCtx(KS_OK)
    out = script("reconcile")(ctx, timeout="0s")
    r = out["reconcile"]
    assert r["applied"] is True and r["completed"] is None and r["completionReason"] == "not_waited"
    assert r["after"] is None and len(ctx.calls) == 2


def test_reconcile_records_the_previous_request():
    ctx = FakeCtx(KS_OK)
    ctx.obj()["metadata"]["annotations"] = {
        "reconcile.fluxcd.io/requestedAt": "2026-01-01T00:00:00Z"
    }
    out = script("reconcile")(ctx, timeout="0s")
    assert out["reconcile"]["previousRequestedAt"] == "2026-01-01T00:00:00Z"
    assert out["reconcile"]["requestedAt"] != "2026-01-01T00:00:00Z"


def test_reconcile_annotates_the_target_in_its_namespace():
    ctx = FakeCtx(KS_OK)
    script("reconcile")(ctx, timeout="0s")
    (call,) = annotate_calls(ctx)
    assert call[:6] == ["kubectl", "-n", NS, "annotate", KS_RES, "apps-ok"]


def test_suspended_object_is_refused_and_nothing_is_written():
    ctx = FakeCtx(HR_SUSPENDED)
    with pytest.raises(RwError) as exc:
        script("reconcile")(ctx)
    assert exc.value.code == "invalid_input" and "resume" in exc.value.message
    assert annotate_calls(ctx) == []


@pytest.mark.parametrize("bad", ["", "soon", "-5s", "241s", "10m"])
def test_reconcile_timeout_is_validated_before_any_kubectl_call(bad):
    ctx = FakeCtx(HR_OK)
    with pytest.raises(RwError) as exc:
        script("reconcile")(ctx, timeout=bad)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_forbidden_annotate_raises_instead_of_reporting_a_request():
    ctx = FakeCtx(
        KS_OK,
        answers=[
            (
                "annotate",
                (
                    1,
                    "",
                    "Error from server (Forbidden): kustomizations is forbidden: cannot patch resource",
                ),
            )
        ],
    )
    with pytest.raises(RwError) as exc:
        script("reconcile")(ctx)
    assert exc.value.code == "permission_denied"


def test_a_read_failing_while_waiting_is_reported_and_the_request_stands(clock):
    ctx = FakeCtx(KS_OK)
    n = {"gets": 0}

    def fail_after_first(argv, joined, _orig=ctx._answer):
        if "get" in argv:
            n["gets"] += 1
            if n["gets"] > 1:
                return 1, "", "Unable to connect to the server: dial tcp: i/o timeout"
        return _orig(argv, joined)

    ctx._answer = fail_after_first
    out = script("reconcile")(ctx, timeout="30s")
    check(out)
    r = out["reconcile"]
    assert (
        r["applied"] is True and r["completed"] is None and r["completionReason"] == "not_observed"
    )
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "wait")]


def test_reconcile_message_is_truncated():
    ctx = FakeCtx(HR_FAILED)
    ctx.obj()["status"]["conditions"][0]["message"] = "m" * 4000
    out = script("reconcile")(ctx, timeout="0s")
    assert out["reconcile"]["after"] is None  # not waited: nothing to truncate
    done = f.reconcile_after("HelmRelease", ctx.obj())
    assert len(done["readyMessage"]) == 1024 and done["readyMessageTruncated"] is True


# ------------------------------------------------------ suspend, resume ----


def patch_calls(ctx):
    return [c for c in ctx.calls if "patch" in c]


def test_suspend_patches_spec_suspend_true():
    ctx = FakeCtx(HR_OK)
    out = script("suspend")(ctx)
    check(out, suspension="suspension")
    (call,) = patch_calls(ctx)
    assert call[:6] == ["kubectl", "-n", NS, "patch", HR_RES, "podinfo-ok"]
    assert "--type=merge" in call and "--dry-run=server" not in call
    assert json.loads(call[call.index("-p") + 1]) == {"spec": {"suspend": True}}
    assert out["suspension"] == {
        "applied": True,
        "changed": True,
        "wouldChange": False,
        "suspendedBefore": False,
        "suspendedAfter": True,
    }
    assert out["target"]["name"] == "podinfo-ok" and out["target"]["kind"] == "HelmRelease"


def test_resume_patches_spec_suspend_false():
    ctx = FakeCtx(KS_SUSPENDED)
    out = script("resume")(ctx)
    check(out)
    (call,) = patch_calls(ctx)
    assert json.loads(call[call.index("-p") + 1]) == {"spec": {"suspend": False}}
    assert out["suspension"]["changed"] is True
    assert (out["suspension"]["suspendedBefore"], out["suspension"]["suspendedAfter"]) == (
        True,
        False,
    )


def test_suspend_and_resume_dry_run_send_the_identical_patch_with_dry_run_server():
    real, dry = FakeCtx(HR_OK), FakeCtx(HR_OK, dry_run=True)
    script("suspend")(real)
    out = script("suspend")(dry)
    (r,), (d,) = patch_calls(real), patch_calls(dry)
    assert d == [*r, "--dry-run=server"]
    s = out["suspension"]
    assert (s["applied"], s["changed"], s["wouldChange"]) == (False, False, True)
    assert s["suspendedAfter"] is False  # unchanged in the cluster
    assert dry.obj()["spec"]["suspend"] is False
    out = script("resume")(FakeCtx(HR_SUSPENDED, dry_run=True))
    assert out["suspension"]["wouldChange"] is True and out["suspension"]["suspendedAfter"] is True


def test_suspending_a_suspended_object_and_resuming_a_running_one_send_nothing():
    ctx = FakeCtx(HR_SUSPENDED)
    out = script("suspend")(ctx)
    assert patch_calls(ctx) == []
    assert (out["suspension"]["applied"], out["suspension"]["changed"]) == (False, False)
    assert out["suspension"]["suspendedAfter"] is True
    ctx = FakeCtx(KS_OK)
    out = script("resume")(ctx)
    assert patch_calls(ctx) == [] and out["suspension"]["suspendedAfter"] is False


def test_forbidden_patch_raises_instead_of_reporting_a_change():
    ctx = FakeCtx(
        HR_OK,
        answers=[
            (
                "patch",
                (
                    1,
                    "",
                    "Error from server (Forbidden): helmreleases is forbidden: cannot patch resource",
                ),
            )
        ],
    )
    with pytest.raises(RwError) as exc:
        script("suspend")(ctx)
    assert exc.value.code == "permission_denied"


def test_suspend_says_which_flux_object_applies_the_target():
    ctx = FakeCtx(HR_OK)
    ctx.obj()["metadata"]["labels"] = {
        "kustomize.toolkit.fluxcd.io/name": "apps",
        "kustomize.toolkit.fluxcd.io/namespace": "flux-system",
    }
    out = script("suspend")(ctx)
    assert out["target"]["managedBy"] == {
        "kind": "Kustomization",
        "name": "apps",
        "namespace": "flux-system",
    }


# --------------------------------------------------------- permissions ----

SCRIPT_FOR_TASK = {
    t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
}
RESOURCES = {
    HR_RES: ("helm.toolkit.fluxcd.io", "helmreleases"),
    KS_RES: ("kustomize.toolkit.fluxcd.io", "kustomizations"),
}
VALUE_FLAGS = {"-n", "--namespace", "-o", "-p", "--type", "--context"}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name) -> list; patch and annotate -> patch.
    Raises on anything else, so a script that issues a new verb has to extend this mapping.
    """
    positional, it = [], iter(argv[1:])
    for a in it:
        if a in VALUE_FLAGS:
            next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb, noun = positional[0], positional[1]
    if verb == "annotate" and "=" in positional[-1]:
        positional = positional[:-1]
    group, plural = RESOURCES[noun]
    if verb == "get":
        return {(group, plural, "get" if len(positional) > 2 else "list")}
    if verb in ("patch", "annotate"):
        return {(group, plural, "patch")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(["kubectl", "-n", "x", "get", HR_RES, "a", "-o", "json"]) == {
        ("helm.toolkit.fluxcd.io", "helmreleases", "get")
    }
    assert rbac_needs(["kubectl", "-n", "x", "get", KS_RES, "-o", "json"]) == {
        ("kustomize.toolkit.fluxcd.io", "kustomizations", "list")
    }
    assert rbac_needs(["kubectl", "-n", "x", "annotate", KS_RES, "a", "k/v=1", "--overwrite"]) == {
        ("kustomize.toolkit.fluxcd.io", "kustomizations", "patch")
    }
    assert rbac_needs(["kubectl", "-n", "x", "patch", HR_RES, "a", "--type=merge", "-p", "{}"]) == {
        ("helm.toolkit.fluxcd.io", "helmreleases", "patch")
    }


@pytest.mark.parametrize("task", ["ready-status", "reconcile", "suspend", "resume"])
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, clock):
    needed = set()
    for doc in (HR_FAILED, HR_SUSPENDED, KS_BROKEN, KS_OK, KS_SUSPENDED, HR_OK):
        if task == "reconcile" and doc in (HR_SUSPENDED, KS_SUSPENDED):
            continue
        for dry in (False, True):
            ctx = FakeCtx(doc, dry_run=dry)
            kwargs = {"timeout": "3s"} if task == "reconcile" else {}
            script(task.replace("-", "_"))(ctx, **kwargs)
            for argv in ctx.calls:
                needed |= rbac_needs(argv)
    missing = needed - declared(SCRIPT_FOR_TASK[task])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"
    # and nothing more than it calls: a read task never declares patch
    extra = declared(SCRIPT_FOR_TASK[task]) - needed
    assert not extra, f"{task} declares {sorted(extra)} it never uses"


def test_read_task_is_read_only_and_writes_declare_dry_run():
    tasks = SCRIPT_FOR_TASK
    assert tasks["ready-status"]["readOnly"] is True
    for name in ("reconcile", "suspend", "resume"):
        t = tasks[name]
        assert t["readOnly"] is False and t["dryRun"] is True
        assert t["idempotent"] is True and t["destructive"] is False and t["effects"]
