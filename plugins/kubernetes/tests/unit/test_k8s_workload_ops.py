"""Unit tests for k8s-workload-ops: lib/ and the thin scripts, against JSON captured
from the kind-rw-plugins scenarios (rw-healthy/web, db, agent) after real
restarts, rollbacks and resource changes, so the revision histories are real.
No test calls a cluster: scripts run against a fake ctx that answers kubectl
from the captured files and records every call."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from k8s_workload_ops import k8s
from k8s_workload_ops import workload as w
from k8s_workload_ops.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-workload-ops"
DATA = Path(__file__).parent / "data" / "k8s_workload_ops"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


WEB = load("web_deployment")
WEB_GITOPS = load("web_gitops_deployment")
DB = load("db_statefulset")
AGENT = load("agent_daemonset")
WEB_RS = load("web_replicasets")["items"]
DB_CR = load("db_controllerrevisions")["items"]
AGENT_CR = load("agent_controllerrevisions")["items"]
PODS = load("pods")["items"]
HPAS = load("hpas")["items"]


# ------------------------------------------------------------ fake ctx ----

TARGETS = {
    "web": ("kubernetes/deployment", "deployments.apps", WEB),
    "db": ("kubernetes/statefulset", "statefulsets.apps", DB),
    "agent": ("kubernetes/daemonset", "daemonsets.apps", AGENT),
}
STATUS_OK = (0, 'deployment "web" successfully rolled out\n', "")


class FakeCommandError(RwError):
    """What the v13 runner raises for a non-zero exit when check is true."""

    def __init__(self, argv, returncode, stderr):
        super().__init__("internal", f"{argv[0]} exited {returncode}: {stderr}")
        self.returncode = returncode
        self.stderr = stderr


class FakeCtx:
    """What a script sees: resource, settings, dry_run, error(), run().

    `answers` are (substring of the joined argv, (rc, stdout, stderr)) checked
    before the defaults, which serve the captured objects and succeed on writes.
    An answer may be a list: its entries are used in order, the last one repeating.
    """

    def __init__(
        self, name="web", answers=(), dry_run=False, allow_zero=False, doc=None, with_hpa=False
    ):
        rtype, self.kind_resource, base = TARGETS[name]
        self.doc = copy.deepcopy(doc or base)
        self.resource = SimpleNamespace(namespace="rw-healthy", name=name, type=rtype)
        self.settings = SimpleNamespace(max_items=200, allow_scale_to_zero=allow_zero)
        self.dry_run = dry_run
        self.hpas = HPAS if with_hpa else []
        self.answers = list(answers)
        self.calls = []
        self.errors = []

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope, "message": message})

    def line(self, i=-1):
        return " ".join(self.calls[i][1:])

    def lines(self):
        return [" ".join(c[1:]) for c in self.calls]

    def writes(self):
        verbs = (
            "patch",
            "rollout restart",
            "rollout undo",
            "scale ",
            "delete ",
            "set resources",
            "annotate",
        )
        return [x for x in self.lines() if any(v in x for v in verbs)]

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
        rc, out, err = answer if answer is not None else self.default(argv, line)
        if rc and check:
            raise FakeCommandError(argv, rc, err)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def default(self, argv, line):
        name = self.resource.name
        if f"get {self.kind_resource} {name}" in line:
            return 0, json.dumps(self.doc), ""
        if "get replicasets.apps" in line:
            return 0, json.dumps({"items": WEB_RS}), ""
        if "get controllerrevisions.apps" in line:
            return 0, json.dumps({"items": DB_CR if "app=db" in line else AGENT_CR}), ""
        if "get pods" in line:
            return 0, json.dumps({"items": PODS}), ""
        if "get horizontalpodautoscalers" in line:
            return 0, json.dumps({"items": self.hpas}), ""
        if "rollout status" in line:
            return STATUS_OK
        if "rollout undo" in line:
            return 0, f"{name} rolled back\n", ""
        if " delete pod " in line:
            names = []
            for a in argv[argv.index("pod") + 1 :]:
                if a.startswith("-"):
                    break
                names.append(a)
            return 0, "".join(f"pod/{n}\n" for n in names), ""
        if "set resources" in line:
            return 0, json.dumps(self.apply_resources(argv)), ""
        return 0, f"{name} ok\n", ""

    def apply_resources(self, argv):
        doc = copy.deepcopy(self.doc)
        container = next(a.split("=", 1)[1] for a in argv if a.startswith("--containers="))
        spec = next(
            c for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == container
        )
        for a in argv:
            for flag, section in (("--requests=", "requests"), ("--limits=", "limits")):
                if a.startswith(flag):
                    for pair in a[len(flag) :].split(","):
                        key, value = pair.split("=")
                        spec.setdefault("resources", {}).setdefault(section, {})[key] = value
        return doc


def script(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def tasks():
    import yaml

    return {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}


TASKS = tasks()


def check_outputs(task, out):
    """Every output validates against the schema its tasks.yaml entry names."""
    assert set(out) == set(TASKS[task]["outputs"])
    for key, value in out.items():
        schema = json.loads((SKILL / TASKS[task]["outputs"][key]["schema"]).read_text())
        errors = list(Draft202012Validator(schema).iter_errors(value))
        assert not errors, (task, key, errors[:2])


RUN = {
    "rollout-restart": ("rollout_restart", {}),
    "rollback": ("rollback", {}),
    "scale": ("scale", {"replicas": 3}),
    "delete-pods": ("delete_pods", {}),
    "set-resources": ("set_resources", {"memory_limit": "96Mi"}),
}


def run_script(task, ctx, **inputs):
    module, defaults = RUN[task]
    return script(module)(ctx, **{**defaults, **inputs})


# ------------------------------------------------------------ helpers ----


def test_resolve_kind_from_type_then_from_path():
    assert w.resolve_kind("kubernetes/statefulset") == w.STATEFULSET
    assert (
        w.resolve_kind(None, "kubernetes/clusters/c/namespaces/ns/daemonsets/agent") == w.DAEMONSET
    )
    assert w.resolve_kind("", "kubernetes/clusters/c/namespaces/ns/deployments/web") == w.DEPLOYMENT
    for bad in (
        ("kubernetes/job", None),
        (None, "kubernetes/clusters/c/namespaces/ns/jobs/x"),
        (None, None),
    ):
        with pytest.raises(ValueError):
            w.resolve_kind(*bad)


def test_parse_duration_accepts_every_unit_and_rejects_the_rest():
    assert w.parse_duration("90s") == 90
    assert w.parse_duration("1m30s") == 90
    assert w.parse_duration("2m") == 120
    assert w.parse_duration("500ms") == 0.5
    assert w.parse_duration("1h") == 3600
    assert w.parse_duration("1d") == 86400
    for bad in ("", "soon", "10", "5x", "1.5m", "-3s"):
        with pytest.raises(RwError) as exc:
            w.parse_duration(bad)
        assert exc.value.code == "invalid_input"


def test_kubectl_duration_is_whole_seconds_without_a_day_unit():
    assert w.kubectl_duration(120) == "120s"
    assert w.kubectl_duration(0.5) == "1s"
    assert w.kubectl_duration(86400) == "86400s"


def test_clean_reason():
    assert w.clean_reason(None) is None
    assert w.clean_reason("   ") is None
    assert w.clean_reason("  bad deploy  ") == "bad deploy"
    for bad in ("x" * 257, "line one\nline two", "tab\there"):
        with pytest.raises(RwError) as exc:
            w.clean_reason(bad)
        assert exc.value.code == "invalid_input"


def test_limit_text_marks_truncation():
    assert w.limit_text("short") == ("short", False)
    text, truncated = w.limit_text("x" * 2000)
    assert len(text) == 1024 and truncated is True


# --------------------------------------------------------------- gitops ----


def test_plain_workload_is_not_gitops_managed():
    assert w.gitops(WEB) == {"managed": False, "tool": None, "signals": []}
    view = w.target_view(w.DEPLOYMENT, WEB)
    assert view["gitopsManaged"] is False and view["name"] == "web" and view["paused"] is False


def test_flux_label_is_reported_as_data_not_as_a_refusal():
    g = w.gitops(WEB_GITOPS)
    assert g["managed"] is True and g["tool"] == "flux"
    assert g["signals"] == [
        {"kind": "label", "key": "kustomize.toolkit.fluxcd.io/name", "tool": "flux"}
    ]
    assert w.target_view(w.DEPLOYMENT, WEB_GITOPS)["gitopsManaged"] is True


def test_argo_annotation_and_managed_by_are_detected_but_helm_is_not_gitops():
    argo = {
        "metadata": {
            "annotations": {"argocd.argoproj.io/tracking-id": "app:apps/Deployment:ns/web"}
        }
    }
    assert w.gitops(argo)["tool"] == "argocd"
    flux = {"metadata": {"labels": {"app.kubernetes.io/managed-by": "Flux"}}}
    assert w.gitops(flux)["tool"] == "flux"
    helm = {"metadata": {"labels": {"app.kubernetes.io/managed-by": "Helm"}}}
    assert w.gitops(helm)["managed"] is False


def test_gitops_reports_keys_never_values():
    secretish = {"metadata": {"annotations": {"argocd.argoproj.io/sync-options": "token=hunter2"}}}
    assert "hunter2" not in json.dumps(w.gitops(secretish))


# ------------------------------------------------------------ ownership ----


def test_owned_pods_follow_selector_and_owner_chain():
    owned = w.owned_pods(w.DEPLOYMENT, WEB, WEB_RS, PODS)
    assert sorted(p["metadata"]["name"] for p in owned) == [
        "web-7cb4d5696c-mgbh9",
        "web-7cb4d5696c-n4xwc",
    ]
    assert [p["metadata"]["name"] for p in w.owned_pods(w.STATEFULSET, DB, [], PODS)] == ["db-0"]
    assert [p["metadata"]["name"] for p in w.owned_pods(w.DAEMONSET, AGENT, [], PODS)] == [
        "agent-rgjx7"
    ]


def test_a_pod_with_the_right_labels_but_another_controller_is_not_owned():
    stranger = copy.deepcopy(next(p for p in PODS if p["metadata"]["name"].startswith("web-")))
    stranger["metadata"]["name"] = "web-impostor"
    stranger["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "other", "uid": "not-ours", "controller": True}
    ]
    owned = w.owned_pods(w.DEPLOYMENT, WEB, WEB_RS, PODS + [stranger])
    assert "web-impostor" not in [p["metadata"]["name"] for p in owned]


def test_empty_selector_raises_rather_than_selecting_the_namespace():
    with pytest.raises(ValueError):
        w.selector_string({})
    assert (
        w.selector_string(
            {
                "matchLabels": {"b": "2", "a": "1"},
                "matchExpressions": [{"key": "c", "operator": "Exists"}],
            }
        )
        == "a=1,b=2,c"
    )


# ------------------------------------------------------------ revisions ----


def test_revision_views_are_newest_first_with_init_containers_flagged():
    views = w.revision_views(w.DEPLOYMENT, WEB_RS)
    assert [v["revision"] for v in views] == [7, 6, 5, 4, 2]
    assert views[0]["images"] == [{"container": "nginx", "image": "nginx:alpine", "init": False}]
    spec = {
        "containers": [{"name": "app", "image": "a:1"}],
        "initContainers": [{"name": "side", "image": "s:2"}],
    }
    assert w.images(spec) == [
        {"container": "app", "image": "a:1", "init": False},
        {"container": "side", "image": "s:2", "init": True},
    ]


def test_current_revision_per_kind():
    assert w.current_revision(w.DEPLOYMENT, WEB, []) == 7
    assert (
        w.current_revision(w.STATEFULSET, DB, w.revision_views(w.STATEFULSET, DB_CR)) == 5
    )  # status.updateRevision
    assert (
        w.current_revision(w.DAEMONSET, AGENT, w.revision_views(w.DAEMONSET, AGENT_CR)) == 5
    )  # newest
    assert w.current_revision(w.DAEMONSET, AGENT, []) is None


# ------------------------------------------------------------- rollback ----


def plan(kind, doc, items, to=None):
    return w.rollback_plan(
        kind, doc, w.revision_views(kind, items), to, doc["spec"].get("revisionHistoryLimit")
    )


def test_rollback_defaults_to_the_previous_revision_not_current_minus_one():
    # history is 2,4,5,6,7: after earlier rollbacks the numbers have gaps
    p = plan(w.DEPLOYMENT, WEB, WEB_RS)
    assert p["from"]["revision"] == 7 and p["to"]["revision"] == 6 and p["changed"] is True
    assert p["toRevision"] is None
    assert [a["revision"] for a in p["available"]] == [7, 6, 5, 4, 2]
    assert [a["current"] for a in p["available"]] == [True, False, False, False, False]


def test_rollback_to_a_named_revision_and_to_the_current_one():
    p = plan(w.DEPLOYMENT, WEB, WEB_RS, to=4)
    assert p["to"]["revision"] == 4 and p["toRevision"] == 4 and p["changed"] is True
    again = plan(w.DEPLOYMENT, WEB, WEB_RS, to=7)
    assert again["changed"] is False


def test_rollback_works_for_statefulset_and_daemonset_history():
    p = plan(w.STATEFULSET, DB, DB_CR)
    assert (p["from"]["revision"], p["to"]["revision"]) == (5, 4)
    p = plan(w.DAEMONSET, AGENT, AGENT_CR, to=2)
    assert (p["from"]["revision"], p["to"]["revision"]) == (5, 2)


@pytest.mark.parametrize("to", [99, 0, -1, "4", True])
def test_rollback_refuses_a_revision_that_is_not_in_the_history(to):
    with pytest.raises(RwError) as exc:
        plan(w.DEPLOYMENT, WEB, WEB_RS, to=to)
    assert exc.value.code == "invalid_input"


def test_rollback_with_no_previous_revision_says_so_and_names_the_history_limit():
    only = [
        r
        for r in WEB_RS
        if r["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "7"
    ]
    with pytest.raises(RwError) as exc:
        plan(w.DEPLOYMENT, WEB, only)
    assert exc.value.code == "invalid_input" and "revisionHistoryLimit is 10" in exc.value.message


def test_a_paused_deployment_cannot_be_rolled_back_or_restarted():
    paused = copy.deepcopy(WEB)
    paused["spec"]["paused"] = True
    with pytest.raises(RwError) as exc:
        plan(w.DEPLOYMENT, paused, WEB_RS)
    assert exc.value.code == "invalid_input"
    with pytest.raises(RwError):
        w.check_restartable(w.DEPLOYMENT, paused)
    w.check_restartable(w.DEPLOYMENT, WEB)
    w.check_restartable(w.STATEFULSET, DB)


def test_undo_skip_is_recognised_from_kubectl_output():
    assert w.undo_was_skipped(
        "deployment.apps/web skipped rollback (current template already matches revision 6)"
    )
    assert not w.undo_was_skipped("deployment.apps/web rolled back")


def test_rollout_failure_reasons():
    assert w.classify_rollout_failure("error: timed out waiting for the condition") == (
        "timeout",
        True,
    )
    assert (
        w.classify_rollout_failure('error: deployment "web" exceeded its progress deadline')[0]
        == "progress_deadline_exceeded"
    )
    assert (
        w.classify_rollout_failure(
            "error: rollout status is only available for RollingUpdate strategy type"
        )[0]
        == "update_strategy_on_delete"
    )
    assert w.classify_rollout_failure("something else")[1] is False


# ---------------------------------------------------------------- scale ----


def test_scale_refuses_a_daemonset_negative_and_non_integer_replicas():
    for kind, replicas in (
        (w.DAEMONSET, 2),
        (w.DEPLOYMENT, -1),
        (w.DEPLOYMENT, "3"),
        (w.DEPLOYMENT, 2.5),
        (w.DEPLOYMENT, True),
        (w.DEPLOYMENT, None),
    ):
        with pytest.raises(RwError) as exc:
            w.check_scale_input(kind, replicas, False)
        assert exc.value.code == "invalid_input"
    w.check_scale_input(w.STATEFULSET, 3, False)


def test_scale_to_zero_needs_the_setting():
    with pytest.raises(RwError) as exc:
        w.check_scale_input(w.DEPLOYMENT, 0, False)
    assert exc.value.code == "invalid_input" and "allowScaleToZero" in exc.value.message
    w.check_scale_input(w.DEPLOYMENT, 0, True)


def test_only_autoscalers_that_target_this_workload_count():
    assert [h["name"] for h in w.hpas_targeting(w.DEPLOYMENT, "web", HPAS)] == ["web"]
    assert w.hpas_targeting(w.DEPLOYMENT, "other", HPAS) == []
    assert w.hpas_targeting(w.STATEFULSET, "web", HPAS) == []  # same name, different kind


def test_scale_plan_refuses_under_an_autoscaler_unless_overridden():
    hpas = w.hpas_targeting(w.DEPLOYMENT, "web", HPAS)
    with pytest.raises(RwError) as exc:
        w.scale_plan(w.DEPLOYMENT, WEB, 3, hpas, False)
    assert exc.value.code == "invalid_input" and "overrideHpa" in exc.value.message
    p = w.scale_plan(w.DEPLOYMENT, WEB, 3, hpas, True)
    assert p["overrodeHpa"] is True and p["from"] == 2 and p["to"] == 3 and p["changed"] is True
    assert w.scale_plan(w.DEPLOYMENT, WEB, 2, [], False)["changed"] is False


# --------------------------------------------------------------- delete ----


def owned_web():
    return w.owned_pods(w.DEPLOYMENT, WEB, WEB_RS, PODS)


def test_delete_plan_all_or_named():
    everything = w.delete_plan(owned_web(), None)
    assert everything["deletesAll"] is True and everything["ownedCount"] == 2
    one = w.delete_plan(owned_web(), ["web-7cb4d5696c-mgbh9"])
    assert [p["name"] for p in one["pods"]] == ["web-7cb4d5696c-mgbh9"] and one[
        "deletesAll"
    ] is False


def test_delete_plan_never_includes_a_pod_the_workload_does_not_own():
    with pytest.raises(RwError) as exc:
        w.delete_plan(owned_web(), ["web-7cb4d5696c-mgbh9", "db-0"])
    assert (
        exc.value.code == "invalid_input"
        and "db-0" in exc.value.message
        and "nothing was deleted" in exc.value.message
    )


@pytest.mark.parametrize("names", [[], "web-7cb4d5696c-mgbh9", [""], [1]])
def test_delete_plan_refuses_unusable_pod_names(names):
    with pytest.raises(RwError) as exc:
        w.delete_plan(owned_web(), names)
    assert exc.value.code == "invalid_input"


def test_delete_plan_with_no_pods_deletes_nothing():
    p = w.delete_plan([], None)
    assert p["pods"] == [] and p["deletesAll"] is False


def test_parse_deleted_reads_kubectl_name_output():
    assert w.parse_deleted("pod/a\npod/b\n") == ["a", "b"]
    assert w.parse_deleted("") == []


# ------------------------------------------------------------ resources ----


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100m", "0.1"),
        ("0.5", "0.5"),
        ("1", "1"),
        ("64Mi", 64 * 2**20),
        ("1Gi", 2**30),
        ("1G", 10**9),
        ("500M", 5 * 10**8),
        ("1e3", 1000),
        ("2k", 2000),
    ],
)
def test_parse_quantity(text, expected):
    from decimal import Decimal

    assert w.parse_quantity(text) == Decimal(str(expected))


@pytest.mark.parametrize("bad", ["", "lots", "10x", "Mi", "1 Gi", "-1"])
def test_parse_quantity_rejects_junk(bad):
    with pytest.raises(ValueError):
        w.parse_quantity(bad)


def test_resource_input_needs_one_positive_quantity():
    assert w.check_resource_input(
        {"cpuLimit": " 500m ", "memoryLimit": None, "cpuRequest": ""}
    ) == {"cpuLimit": "500m"}
    for values in (
        {},
        {"cpuLimit": None},
        {"cpuLimit": "0"},
        {"memoryLimit": "0Mi"},
        {"cpuLimit": "lots"},
    ):
        with pytest.raises(RwError) as exc:
            w.check_resource_input(values)
        assert exc.value.code == "invalid_input"


def test_resource_plan_reports_before_and_after_for_each_field():
    p = w.resource_plan(WEB, None, {"memoryLimit": "96Mi", "cpuLimit": "250m"})
    assert p["container"] == "nginx" and p["init"] is False
    fields = {f["field"]: f for f in p["fields"]}
    assert fields["memoryLimit"] == {
        "field": "memoryLimit",
        "before": "64Mi",
        "after": "96Mi",
        "requested": True,
        "differs": True,
    }
    assert fields["cpuLimit"]["before"] is None and fields["cpuLimit"]["after"] == "250m"
    assert fields["cpuRequest"]["before"] == "5m" and fields["cpuRequest"]["requested"] is False
    assert fields["cpuRequest"]["differs"] is False
    assert p["limits"] == "cpu=250m,memory=96Mi" and p["requests"] == ""


def test_equivalent_quantities_are_not_a_change():
    p = w.resource_plan(WEB, None, {"cpuRequest": "0.005", "memoryRequest": "8192Ki"})
    assert [f["differs"] for f in p["fields"]] == [False, False, False, False]


def test_container_choice_with_one_several_unknown_and_init_containers():
    multi = copy.deepcopy(WEB)
    spec = multi["spec"]["template"]["spec"]
    spec["containers"].append({"name": "sidecar", "image": "s:1"})
    spec["initContainers"] = [
        {"name": "setup", "image": "i:1", "resources": {"requests": {"cpu": "1"}}}
    ]
    with pytest.raises(RwError) as exc:
        w.choose_container(multi, None)
    assert exc.value.code == "invalid_input" and "nginx" in exc.value.message
    assert w.choose_container(multi, "sidecar") == "sidecar"
    with pytest.raises(RwError):
        w.choose_container(multi, "nope")
    p = w.resource_plan(multi, "setup", {"cpuLimit": "2"})
    assert p["init"] is True and p["fields"][0]["before"] == "1"


def test_a_request_above_its_limit_is_refused():
    with pytest.raises(RwError) as exc:
        w.resource_plan(WEB, None, {"memoryRequest": "128Mi"})  # limit is 64Mi
    assert exc.value.code == "invalid_input" and "exceed" in exc.value.message
    w.resource_plan(WEB, None, {"cpuRequest": "2"})  # no cpu limit set: fine
    w.resource_plan(WEB, None, {"memoryRequest": "128Mi", "memoryLimit": "256Mi"})  # raising both


# ---------------------------------------------------------- k8s errors ----

STDERR = {
    "permission_denied": 'Error from server (Forbidden): deployments.apps "web" is forbidden: User "u" cannot get resource "deployments"',
    "not_found": 'Error from server (NotFound): deployments.apps "web" not found',
    "unreachable": "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout",
    "timeout": "context deadline exceeded",
    "rate_limited": "Error from server (TooManyRequests): 429 Too Many Requests",
    "dependency": 'error: the server doesn\'t have a resource type "horizontalpodautoscalers"',
    "internal": "error: something odd",
}


@pytest.mark.parametrize("code", sorted(STDERR))
def test_kubectl_failures_are_classified(code):
    assert k8s.classify(1, STDERR[code]) == code


def test_exit_codes_124_and_127():
    assert k8s.classify(124, "") == "timeout"
    assert k8s.classify(127, "") == "dependency"


def test_the_error_message_skips_kubectls_warning_lines():
    warning = "Warning: previously managed with 'kubectl apply'\nError from server (Forbidden): cannot patch"
    ctx = FakeCtx(answers=[("rollout undo", (1, "", warning))])
    with pytest.raises(RwError) as exc:
        k8s.kubectl_text(ctx, "rollout", "undo", "deployment/web")
    assert (
        exc.value.code == "permission_denied"
        and "Forbidden" in exc.value.message
        and "Warning" not in exc.value.message
    )


def test_non_json_output_is_an_internal_error():
    ctx = FakeCtx(answers=[("get deployments.apps web", (0, "<html>", ""))])
    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(ctx, "get", "deployments.apps", "web")
    assert exc.value.code == "internal"


FIRST_READ = {  # tasks, the stderr class that must come out, whichever script reads first
    "permission_denied": STDERR["permission_denied"],
    "not_found": STDERR["not_found"],
    "unreachable": STDERR["unreachable"],
}


@pytest.mark.parametrize("task", sorted(RUN))
@pytest.mark.parametrize("code", sorted(FIRST_READ))
def test_a_failed_read_of_the_target_raises_with_its_code_and_writes_nothing(task, code):
    ctx = FakeCtx(answers=[("get deployments.apps web", (1, "", FIRST_READ[code]))])
    with pytest.raises(RwError) as exc:
        run_script(task, ctx)
    assert exc.value.code == code
    assert ctx.writes() == []


@pytest.mark.parametrize(
    "task,needle",
    [
        ("rollout-restart", "patch"),
        ("rollback", "rollout undo"),
        ("scale", "scale deployment.apps/web"),
        ("delete-pods", "delete pod"),
        ("set-resources", "set resources"),
    ],
)
@pytest.mark.parametrize("dry", [False, True])
def test_a_forbidden_write_raises_permission_denied_and_never_returns_a_result(task, needle, dry):
    ctx = FakeCtx(
        answers=[(needle, (1, "", STDERR["permission_denied"].replace("get", "patch")))],
        dry_run=dry,
    )
    with pytest.raises(RwError) as exc:
        run_script(task, ctx)
    assert exc.value.code == "permission_denied"


# ------------------------------------------------------- rollout-restart ----


def test_restart_is_one_patch_then_waits_and_reports_the_new_revision():
    bumped = copy.deepcopy(WEB)
    bumped["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "8"
    # the first read is before the restart, the second after it
    ctx = FakeCtx(
        answers=[
            ("get deployments.apps web", [(0, json.dumps(WEB), ""), (0, json.dumps(bumped), "")])
        ]
    )
    out = run_script("rollout-restart", ctx, reason="bad config", timeout="90s")
    check_outputs("rollout-restart", out)
    r = out["restart"]
    assert r["applied"] is True and r["completed"] is True and r["completionReason"] == "rolled_out"
    assert (r["revisionBefore"], r["revisionAfter"]) == (7, 8)
    assert (
        r["reason"] == "bad config" and r["reasonRecorded"] is True and r["reasonCleared"] is False
    )
    assert r["restartedAt"] is not None and r["updateStrategy"] == "RollingUpdate"
    lines = ctx.lines()
    patches = [c for c in ctx.calls if "patch" in c]
    assert len(patches) == 1  # reason and restart annotation in one write: no half-done state
    assert not any(" annotate " in x or "rollout restart" in x for x in lines)
    body = json.loads(patches[0][patches[0].index("-p") + 1])
    assert body["metadata"]["annotations"] == {"runwhen.com/restart-reason": "bad config"}
    template = body["spec"]["template"]["metadata"]["annotations"]
    assert template["kubectl.kubernetes.io/restartedAt"] == r["restartedAt"]
    assert "--type=merge" in patches[0] and "--dry-run=server" not in patches[0]
    status = "-n rw-healthy rollout status deployment.apps/web --timeout=90s"
    assert lines.index(status) > lines.index(" ".join(patches[0][1:]))
    assert ctx.errors == []
    # a Deployment keeps its revision on itself: no ReplicaSet list needed
    assert not any("replicasets" in x for x in lines)


def test_restart_dry_run_sends_the_same_patch_as_a_server_dry_run_and_does_not_wait():
    ctx = FakeCtx(dry_run=True)
    out = run_script("rollout-restart", ctx, reason="why")
    check_outputs("rollout-restart", out)
    r = out["restart"]
    assert r["applied"] is False and r["completed"] is None and r["revisionAfter"] is None
    # a dry run reports nothing as done, and still shows the strategy the approver needs
    assert r["reasonRecorded"] is False and r["reasonCleared"] is False and r["restartedAt"] is None
    assert (
        r["updateStrategy"] == "RollingUpdate"
        and out["target"]["updateStrategy"] == "RollingUpdate"
    )
    patch = next(c for c in ctx.calls if "patch" in c)
    assert patch[-1] == "--dry-run=server"
    assert "kubectl.kubernetes.io/restartedAt" in patch[patch.index("-p") + 1]
    assert not any("rollout status" in x for x in ctx.lines())
    assert ctx.writes() == [" ".join(patch[1:])]


def test_restart_without_a_reason_clears_an_older_reason_annotation():
    old = copy.deepcopy(WEB)
    old["metadata"]["annotations"]["runwhen.com/restart-reason"] = "last week"
    ctx = FakeCtx(doc=old)
    r = run_script("rollout-restart", ctx)["restart"]
    patch = next(c for c in ctx.calls if "patch" in c)
    body = json.loads(patch[patch.index("-p") + 1])
    assert body["metadata"]["annotations"] == {
        "runwhen.com/restart-reason": None
    }  # merge patch: delete
    assert (r["reasonPresentBefore"], r["reasonCleared"], r["reasonRecorded"]) == (
        True,
        True,
        False,
    )
    dry = FakeCtx(doc=old, dry_run=True)
    r = run_script("rollout-restart", dry)["restart"]
    assert (r["reasonPresentBefore"], r["reasonCleared"]) == (True, False)  # visible, not yet done


def test_restart_without_a_reason_and_none_before_clears_nothing():
    r = run_script("rollout-restart", FakeCtx())["restart"]
    assert (r["reasonPresentBefore"], r["reasonCleared"], r["reasonRecorded"]) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize("strategy", ["Recreate", "OnDelete"])
def test_restart_reports_the_update_strategy_so_the_approver_sees_what_it_will_do(strategy):
    doc = copy.deepcopy(DB)
    doc["spec"]["updateStrategy"] = {"type": strategy}
    ctx = FakeCtx("db", doc=doc, dry_run=True)
    out = run_script("rollout-restart", ctx)
    assert (
        out["restart"]["updateStrategy"] == strategy and out["target"]["updateStrategy"] == strategy
    )


def test_a_slow_rollout_is_reported_not_failed():
    ctx = FakeCtx(
        answers=[
            ("rollout status", (1, "Waiting...\n", "error: timed out waiting for the condition\n"))
        ]
    )
    out = run_script("rollout-restart", ctx, timeout="5s")
    check_outputs("rollout-restart", out)
    r = out["restart"]
    assert r["applied"] is True and r["completed"] is False and r["completionReason"] == "timeout"
    assert ctx.errors == []


def test_progress_deadline_exceeded_is_reported_as_the_rollout_state():
    ctx = FakeCtx(
        answers=[
            ("rollout status", (1, "", 'error: deployment "web" exceeded its progress deadline\n'))
        ]
    )
    r = run_script("rollout-restart", ctx)["restart"]
    assert (
        r["completed"] is False
        and r["completionReason"] == "progress_deadline_exceeded"
        and ctx.errors == []
    )


def test_a_forbidden_status_call_is_an_error_not_a_slow_rollout():
    # kubectl prints "timed out" after a failed watch too; that must not read as a slow rollout
    err = 'E1 "Failed to watch" err="failed to list: deployments.apps is forbidden"\nerror: timed out waiting for the condition\n'
    ctx = FakeCtx(answers=[("rollout status", (1, "", err))])
    out = run_script("rollout-restart", ctx)
    r = out["restart"]
    assert (
        r["applied"] is True and r["completed"] is False and r["completionReason"] == "not_observed"
    )
    assert [e["code"] for e in ctx.errors] == ["permission_denied"] and ctx.errors[0][
        "scope"
    ] == "rollout-status"


def test_a_long_status_message_is_truncated_with_a_flag():
    ctx = FakeCtx(answers=[("rollout status", (0, "x" * 5000, ""))])
    r = run_script("rollout-restart", ctx)["restart"]
    assert len(r["message"]) == 1024 and r["truncated"] is True


def test_a_failed_patch_fails_the_task_with_nothing_half_done():
    ctx = FakeCtx(answers=[("patch", (1, "", STDERR["permission_denied"]))])
    with pytest.raises(RwError) as exc:
        run_script("rollout-restart", ctx, reason="r")
    assert exc.value.code == "permission_denied"
    assert not any("rollout status" in x for x in ctx.lines())


def test_restart_that_cannot_read_the_new_revision_keeps_its_result():
    ctx = FakeCtx(
        answers=[
            ("get deployments.apps web", [(0, json.dumps(WEB), ""), (1, "", STDERR["unreachable"])])
        ]
    )
    out = run_script("rollout-restart", ctx)
    assert out["restart"]["revisionAfter"] is None and out["restart"]["completed"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "revision")]


def test_restart_statefulset_and_daemonset_read_their_controller_revisions():
    for name, expect in (("db", 5), ("agent", 5)):
        ctx = FakeCtx(name)
        out = run_script("rollout-restart", ctx)
        check_outputs("rollout-restart", out)
        assert out["restart"]["revisionBefore"] == expect
        assert any("controllerrevisions" in x for x in ctx.lines())
        ref = f"{w.SHORT[out['target']['kind']]}/{name}"
        assert any(c[1:5] == ["-n", "rw-healthy", "patch", ref] for c in ctx.calls)


def test_restart_refuses_a_paused_deployment_before_writing():
    paused = copy.deepcopy(WEB)
    paused["spec"]["paused"] = True
    ctx = FakeCtx(doc=paused)
    with pytest.raises(RwError) as exc:
        run_script("rollout-restart", ctx)
    assert exc.value.code == "invalid_input" and ctx.writes() == []


@pytest.mark.parametrize("timeout", ["soon", "0s", "241s", "10m", "1d", ""])
def test_restart_rejects_a_bad_timeout_before_any_kubectl_call(timeout):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_script("rollout-restart", ctx, timeout=timeout)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_restart_rejects_a_bad_reason_before_any_kubectl_call():
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_script("rollout-restart", ctx, reason="a\nb")
    assert exc.value.code == "invalid_input" and ctx.calls == []


# -------------------------------------------------------------- rollback ----


def test_rollback_names_the_revision_it_shows_the_approver():
    ctx = FakeCtx()
    out = run_script("rollback", ctx)
    check_outputs("rollback", out)
    r = out["rollback"]
    assert (
        r["applied"] is True
        and r["changed"] is True
        and r["from"]["revision"] == 7
        and r["to"]["revision"] == 6
    )
    assert "-n rw-healthy rollout undo deployment.apps/web --to-revision=6" in ctx.lines()
    assert r["revisionHistoryLimit"] == 10


def test_rollback_dry_run_adds_the_server_dry_run_flag_and_reports_would_change():
    ctx = FakeCtx(dry_run=True)
    out = run_script("rollback", ctx, to_revision=4)
    r = out["rollback"]
    assert (r["applied"], r["changed"], r["wouldChange"]) == (False, False, True)
    assert r["to"]["revision"] == 4
    assert (
        ctx.line(-1)
        == "-n rw-healthy rollout undo deployment.apps/web --to-revision=4 --dry-run=server"
    )
    real = run_script("rollback", FakeCtx(), to_revision=4)["rollback"]
    assert (real["applied"], real["changed"], real["wouldChange"]) == (True, True, False)


def test_rollback_to_the_current_revision_writes_nothing():
    ctx = FakeCtx()
    r = run_script("rollback", ctx, to_revision=7)["rollback"]
    assert r["applied"] is False and r["changed"] is False and ctx.writes() == []


def test_rollback_reports_kubectls_own_skip():
    ctx = FakeCtx(
        answers=[
            (
                "rollout undo",
                (
                    0,
                    "deployment.apps/web skipped rollback (current template already matches revision 6)\n",
                    "",
                ),
            )
        ]
    )
    r = run_script("rollback", ctx)["rollback"]
    assert r["skipped"] is True and r["applied"] is False and r["changed"] is False


def test_rollback_statefulset_and_daemonset():
    ctx = FakeCtx("db")
    out = run_script("rollback", ctx)
    check_outputs("rollback", out)
    assert out["rollback"]["to"]["revision"] == 4
    assert "-n rw-healthy rollout undo statefulset.apps/db --to-revision=4" in ctx.lines()
    ctx = FakeCtx("agent")
    run_script("rollback", ctx, to_revision=2)
    assert "-n rw-healthy rollout undo daemonset.apps/agent --to-revision=2" in ctx.lines()


def test_rollback_with_nothing_to_go_back_to_fails_before_writing():
    rev7 = next(
        r
        for r in WEB_RS
        if r["metadata"]["annotations"]["deployment.kubernetes.io/revision"] == "7"
    )
    ctx = FakeCtx(answers=[("get replicasets.apps", (0, json.dumps({"items": [rev7]}), ""))])
    with pytest.raises(RwError) as exc:
        run_script("rollback", ctx)
    assert exc.value.code == "invalid_input" and ctx.writes() == []


# ----------------------------------------------------------------- scale ----


def test_scale_sets_replicas_and_reports_from_and_to():
    ctx = FakeCtx()
    out = run_script("scale", ctx, replicas=5)
    check_outputs("scale", out)
    s = out["scale"]
    assert (
        s["applied"] is True
        and (s["from"], s["to"]) == (2, 5)
        and s["readyBefore"] == 2
        and s["hpas"] == []
    )
    assert "-n rw-healthy scale deployment.apps/web --replicas=5" in ctx.lines()


def test_scale_dry_run_flag_and_statefulset():
    ctx = FakeCtx("db", dry_run=True)
    out = run_script("scale", ctx, replicas=3)
    s = out["scale"]
    assert (s["applied"], s["changed"], s["wouldChange"]) == (False, False, True)
    assert (s["from"], s["to"]) == (1, 3)
    assert ctx.line(-1) == "-n rw-healthy scale statefulset.apps/db --replicas=3 --dry-run=server"
    real = run_script("scale", FakeCtx("db"), replicas=3)["scale"]
    assert (real["applied"], real["changed"], real["wouldChange"]) == (True, True, False)


def test_scale_to_the_current_count_writes_nothing():
    ctx = FakeCtx()
    s = run_script("scale", ctx, replicas=2)["scale"]
    assert s["changed"] is False and s["applied"] is False and ctx.writes() == []


def test_scale_refuses_a_daemonset_with_no_kubectl_call():
    ctx = FakeCtx("agent")
    with pytest.raises(RwError) as exc:
        run_script("scale", ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


@pytest.mark.parametrize("replicas", [-1, "3", 1.5, None])
def test_scale_refuses_bad_replicas_with_no_kubectl_call(replicas):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        script("scale")(ctx, replicas=replicas)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_scale_to_zero_is_refused_unless_the_setting_allows_it():
    ctx = FakeCtx()
    with pytest.raises(RwError):
        run_script("scale", ctx, replicas=0)
    assert ctx.calls == []
    ctx = FakeCtx(allow_zero=True)
    out = run_script("scale", ctx, replicas=0)
    assert out["scale"]["to"] == 0 and "--replicas=0" in ctx.line(-1)


def test_scale_is_refused_under_an_autoscaler_unless_overridden():
    ctx = FakeCtx(with_hpa=True)
    with pytest.raises(RwError) as exc:
        run_script("scale", ctx)
    assert exc.value.code == "invalid_input" and ctx.writes() == []
    ctx = FakeCtx(with_hpa=True)
    out = run_script("scale", ctx, override_hpa=True)
    check_outputs("scale", out)
    assert out["scale"]["overrodeHpa"] is True and [h["name"] for h in out["scale"]["hpas"]] == [
        "web"
    ]
    assert ctx.writes()


def test_an_hpa_that_cannot_be_listed_blocks_the_scale_so_the_guard_is_not_bypassed():
    # the classic guards read a failed lookup as "no autoscaler" and went ahead
    ctx = FakeCtx(answers=[("get horizontalpodautoscalers", (1, "", STDERR["permission_denied"]))])
    with pytest.raises(RwError) as exc:
        run_script("scale", ctx)
    assert exc.value.code == "permission_denied" and "nothing was scaled" in exc.value.message
    assert ctx.writes() == []


def test_an_hpa_that_cannot_be_listed_is_reported_when_the_guard_is_overridden():
    ctx = FakeCtx(answers=[("get horizontalpodautoscalers", (1, "", STDERR["permission_denied"]))])
    out = run_script("scale", ctx, override_hpa=True)
    check_outputs("scale", out)
    assert out["scale"]["hpas"] is None and out["scale"]["applied"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "hpa")]


# ----------------------------------------------------------- delete-pods ----


def test_delete_all_pods_by_name_not_by_selector():
    ctx = FakeCtx()
    out = run_script("delete-pods", ctx)
    check_outputs("delete-pods", out)
    d = out["deletion"]
    assert (
        d["applied"] is True
        and d["deletesAll"] is True
        and d["deletedCount"] == 2
        and d["ownedCount"] == 2
    )
    assert sorted(d["deleted"]) == ["web-7cb4d5696c-mgbh9", "web-7cb4d5696c-n4xwc"]
    delete = next(x for x in ctx.lines() if " delete pod " in x)
    assert "-l" not in delete.split() and "--wait=false" in delete and "-o name" in delete


def test_delete_some_pods_and_dry_run_names_would_delete_not_deleted():
    ctx = FakeCtx(dry_run=True)
    out = run_script("delete-pods", ctx, pod_names=["web-7cb4d5696c-n4xwc"])
    check_outputs("delete-pods", out)
    d = out["deletion"]
    assert d["applied"] is False and d["deletesAll"] is False
    assert d["deleted"] == [] and d["deletedCount"] == 0  # nothing was deleted
    assert d["wouldDelete"] == ["web-7cb4d5696c-n4xwc"] and d["wouldDeleteCount"] == 1
    assert ctx.line(-1).endswith("--dry-run=server")


def test_delete_never_touches_a_pod_it_does_not_own():
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        run_script("delete-pods", ctx, pod_names=["data-user-644b99cfbb-xcvk7"])
    assert exc.value.code == "invalid_input" and ctx.writes() == []
    # the pod shares no owner with web even though the namespace holds it
    ctx = FakeCtx("db")
    out = run_script("delete-pods", ctx)
    assert out["deletion"]["deleted"] == ["db-0"]


def test_delete_with_no_pods_is_a_no_op():
    ctx = FakeCtx(answers=[("get pods", (0, json.dumps({"items": []}), ""))])
    d = run_script("delete-pods", ctx)["deletion"]
    assert (
        d["applied"] is False
        and d["deletedCount"] == 0
        and d["deletesAll"] is False
        and ctx.writes() == []
    )


def test_delete_in_batches_keeps_what_was_deleted_when_a_later_batch_fails():
    main = script("delete_pods")
    main.__globals__["BATCH"] = 1  # one pod per kubectl call
    ctx = FakeCtx(answers=[("delete pod web-7cb4d5696c-n4xwc", (1, "", STDERR["unreachable"]))])
    d = main(ctx)["deletion"]
    assert d["deletedCount"] == 1 and d["requestedCount"] == 2
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("unreachable", "delete")]


def test_a_first_batch_failure_raises():
    main = script("delete_pods")
    main.__globals__["BATCH"] = 1
    ctx = FakeCtx(answers=[(" delete pod ", (1, "", STDERR["unreachable"]))])
    with pytest.raises(RwError) as exc:
        main(ctx)
    assert exc.value.code == "unreachable"


def test_delete_lists_are_capped_by_max_items_but_counts_are_not():
    many = []
    for i in range(5):
        p = copy.deepcopy(PODS[3])
        p["metadata"]["name"] = f"web-extra-{i}"
        many.append(p)
    ctx = FakeCtx(answers=[("get pods", (0, json.dumps({"items": PODS + many}), ""))])
    ctx.settings.max_items = 3
    d = run_script("delete-pods", ctx)["deletion"]
    assert d["deletedCount"] == 7 and len(d["deleted"]) == 3 and len(d["pods"]) == 3


# ---------------------------------------------------------- set-resources ----


def test_set_resources_builds_the_kubectl_call_and_reads_the_result_back():
    ctx = FakeCtx()
    out = run_script("set-resources", ctx, memory_limit="96Mi", cpu_limit="250m")
    check_outputs("set-resources", out)
    r = out["resources"]
    assert r["applied"] is True and r["changed"] is True and r["container"] == "nginx"
    after = {f["field"]: f["after"] for f in r["fields"]}
    assert after == {
        "cpuRequest": "5m",
        "cpuLimit": "250m",
        "memoryRequest": "8Mi",
        "memoryLimit": "96Mi",
    }
    assert (
        ctx.line(-1)
        == "-n rw-healthy set resources deployment.apps/web --containers=nginx --limits=cpu=250m,memory=96Mi -o json"
    )


def test_set_resources_dry_run_and_the_request_flag():
    ctx = FakeCtx("db", dry_run=True)
    out = script("set_resources")(ctx, cpu_request="10m")
    r = out["resources"]
    assert (r["applied"], r["changed"], r["wouldChange"]) == (False, False, True)
    assert (
        ctx.line(-1)
        == "-n rw-healthy set resources statefulset.apps/db --containers=db --requests=cpu=10m --dry-run=server -o json"
    )


def test_set_resources_to_the_current_values_writes_nothing():
    ctx = FakeCtx()
    r = script("set_resources")(ctx, memory_limit="64Mi", cpu_request="5m")["resources"]
    assert r["changed"] is False and r["applied"] is False and ctx.writes() == []


def test_set_resources_reports_what_the_server_returned_not_what_was_asked():
    # an admission webhook may adjust values: the report follows the server's object
    ctx = FakeCtx()
    adjusted = ctx.apply_resources(
        ["kubectl", "set", "resources", "--containers=nginx", "--limits=memory=96Mi"]
    )
    adjusted["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "128Mi"
    ctx.answers.insert(0, ("set resources", (0, json.dumps(adjusted), "")))
    f = {
        x["field"]: x
        for x in run_script("set-resources", ctx, memory_limit="96Mi")["resources"]["fields"]
    }
    assert f["memoryLimit"]["after"] == "128Mi"


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"cpu_limit": "0"},
        {"memory_limit": "lots"},
        {"container": "nope", "cpu_limit": "1"},
        {"memory_request": "1Gi"},
    ],
)
def test_set_resources_refusals(inputs):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        script("set_resources")(ctx, **inputs)
    assert exc.value.code == "invalid_input" and ctx.writes() == []


def test_set_resources_with_input_errors_makes_no_call_before_the_read():
    ctx = FakeCtx()
    with pytest.raises(RwError):
        script("set_resources")(ctx)
    assert ctx.calls == []


# ----------------------------------------------------- every task, shared ----


@pytest.mark.parametrize("task", sorted(RUN))
@pytest.mark.parametrize("dry", [False, True])
def test_every_task_reports_gitops_management_in_target(task, dry):
    ctx = FakeCtx(doc=WEB_GITOPS, dry_run=dry)
    out = run_script(task, ctx)
    check_outputs(task, out)
    assert out["target"]["gitopsManaged"] is True and out["target"]["gitops"]["tool"] == "flux"
    assert ctx.writes()  # it is reported, not a reason to skip the write


@pytest.mark.parametrize("task", sorted(RUN))
def test_every_task_reports_not_gitops_managed_for_a_plain_workload(task):
    ctx = FakeCtx()
    out = run_script(task, ctx)
    assert out["target"]["gitopsManaged"] is False and out["target"]["name"] == "web"


@pytest.mark.parametrize("task", sorted(RUN))
def test_no_kubectl_call_hides_errors(task):
    ctx = FakeCtx()
    run_script(task, ctx)
    for call in ctx.calls:
        assert call[0] == "kubectl" and not any(("/dev/null" in a or "||" in a) for a in call)


@pytest.mark.parametrize("name", ["web", "db", "agent"])
def test_target_kind_from_the_path_when_type_is_absent(name):
    ctx = FakeCtx(name)
    plural = {"web": "deployments", "db": "statefulsets", "agent": "daemonsets"}[name]
    ctx.resource.type = None
    ctx.resource.path = f"kubernetes/clusters/c/namespaces/rw-healthy/{plural}/{name}"
    out = script("delete_pods")(ctx)
    assert (
        out["target"]["kind"]
        == {"web": "Deployment", "db": "StatefulSet", "agent": "DaemonSet"}[name]
    )


# ------------------------------------------------------------ the files ----


def test_every_task_is_a_declared_write_task():
    assert list(TASKS) == ["rollout-restart", "rollback", "scale", "delete-pods", "set-resources"]
    for name, t in TASKS.items():
        assert t["readOnly"] is False and t["dryRun"] is True, name
        assert t["effects"] and all(isinstance(e, str) and e for e in t["effects"]), name
        assert t["destructive"] is (name == "delete-pods"), name
        assert "idempotent" in t, name
        assert t["permissions"], name
        assert not {"status", "errors"} & set(t["outputs"]), name
        assert "target" in t["outputs"], name
    assert [TASKS[n]["idempotent"] for n in TASKS] == [False, False, True, False, True]


def test_tasks_and_skill_validate_against_the_contracts():
    contracts = pytest.importorskip("runwhen_capability.contracts")
    assert (
        contracts.validate("tasks", contracts.load_yaml((SKILL / "tasks.yaml").read_text())) == []
    )
    front = contracts.split_frontmatter((SKILL / "SKILL.md").read_text())[0]
    assert contracts.validate("skill-frontmatter", front) == []
    assert "role" not in front["runwhen"]  # no allowed role describes a write skill


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


# ------------------------------------------------------- review fixes ----


def test_scripts_never_depend_on_the_runner_raising_for_a_non_zero_exit():
    # the v13 runner raises on a non-zero exit unless check=False; every call must opt out so the
    # return code, stdout and stderr stay in this skill's hands
    ctx = FakeCtx(
        answers=[("rollout status", (1, "", "error: timed out waiting for the condition"))]
    )
    run_script("rollout-restart", ctx)
    seen = []
    real_run = ctx.run

    def spy(argv, check=True, **kw):
        seen.append(check)
        return real_run(argv, check=check, **kw)

    ctx.run = spy
    run_script("rollout-restart", ctx)
    assert seen and not any(seen)


def test_delete_reports_pods_deleted_before_a_failure_in_the_same_batch():
    # kubectl deleted one pod, then failed on the other: the name is on stdout and must not be lost
    one, two = "web-7cb4d5696c-mgbh9", "web-7cb4d5696c-n4xwc"
    ctx = FakeCtx(answers=[(" delete pod ", (1, f"pod/{one}\n", STDERR["unreachable"]))])
    out = run_script("delete-pods", ctx)
    check_outputs("delete-pods", out)
    d = out["deletion"]
    assert d["deleted"] == [one] and d["deletedCount"] == 1 and d["applied"] is True
    assert d["remainingCount"] == 1 and d["requestedCount"] == 2
    [error] = ctx.errors
    assert error["code"] == "unreachable" and error["scope"] == "delete"
    assert "1 of 2" in error["message"] and "1 requested pods remain" in error["message"]
    assert two not in d["deleted"]


def test_delete_says_how_many_requested_pods_remain_after_a_later_batch_fails():
    main = script("delete_pods")
    main.__globals__["BATCH"] = 1
    ctx = FakeCtx(answers=[("delete pod web-7cb4d5696c-n4xwc", (1, "", STDERR["unreachable"]))])
    d = main(ctx)["deletion"]
    assert d["deletedCount"] == 1 and d["remainingCount"] == 1
    assert "1 requested pods remain" in ctx.errors[0]["message"]


def test_a_failure_that_deleted_nothing_raises():
    ctx = FakeCtx(answers=[(" delete pod ", (1, "", STDERR["permission_denied"]))])
    with pytest.raises(RwError) as exc:
        run_script("delete-pods", ctx)
    assert exc.value.code == "permission_denied"


def test_pods_already_gone_are_reported_separately_not_as_deleted():
    one, two = "web-7cb4d5696c-mgbh9", "web-7cb4d5696c-n4xwc"
    ctx = FakeCtx(
        answers=[(" delete pod ", (0, f"pod/{one}\n", ""))]
    )  # --ignore-not-found: two is silent
    d = run_script("delete-pods", ctx)["deletion"]
    assert d["deleted"] == [one] and d["alreadyGone"] == [two] and d["alreadyGoneCount"] == 1
    assert d["remainingCount"] == 0 and ctx.errors == []
    assert "--ignore-not-found" in next(c for c in ctx.calls if "delete" in c)


def test_dry_run_outputs_never_read_as_a_done_change():
    for task, inputs in (
        ("scale", {}),
        ("rollback", {}),
        ("set-resources", {}),
        ("delete-pods", {}),
        ("rollout-restart", {}),
    ):
        ctx = FakeCtx(dry_run=True)
        out = run_script(task, ctx, **inputs)
        check_outputs(task, out)
        body = next(v for k, v in out.items() if k != "target")
        assert body["applied"] is False, task
        assert body.get("changed", False) is False, task
        assert body.get("deleted", []) == [] and body.get("deletedCount", 0) == 0, task
        assert body.get("reasonRecorded", False) is False and body.get("restartedAt") is None, task
    assert run_script("scale", FakeCtx(dry_run=True))["scale"]["wouldChange"] is True
    assert run_script("rollback", FakeCtx(dry_run=True))["rollback"]["wouldChange"] is True
    assert run_script("set-resources", FakeCtx(dry_run=True))["resources"]["wouldChange"] is True


def test_target_carries_the_update_strategy_for_every_kind():
    assert w.target_view(w.DEPLOYMENT, WEB)["updateStrategy"] == "RollingUpdate"
    assert w.target_view(w.STATEFULSET, DB)["updateStrategy"] == "RollingUpdate"
    assert w.target_view(w.DAEMONSET, AGENT)["updateStrategy"] == "RollingUpdate"


@pytest.mark.parametrize(
    "text",
    ["1e999999999999", "1E31", "1e-31", "9" * 40, "1e" + "9" * 400],
)
def test_parse_quantity_caps_the_exponent_and_digits(text):
    with pytest.raises(ValueError):
        w.parse_quantity(text)


def test_an_absurd_quantity_is_invalid_input_not_a_crash():
    for text in ("1e999999999999", "1e99999"):
        with pytest.raises(RwError) as exc:
            w.check_resource_input({"cpuLimit": text})
        assert exc.value.code == "invalid_input"
    assert w.check_resource_input({"cpuLimit": "1e3"}) == {"cpuLimit": "1e3"}


@pytest.mark.parametrize("text", ["512", "1000", "500m", "1e3", "100000"])
def test_a_memory_value_under_1mi_without_a_byte_unit_asks_for_mi(text):
    for field in ("memoryRequest", "memoryLimit"):
        with pytest.raises(RwError) as exc:
            w.check_resource_input({field: text})
        assert exc.value.code == "invalid_input" and "Mi" in exc.value.message


@pytest.mark.parametrize("text", ["512Ki", "64Mi", "1Mi", "1048576", "2G", "1e9", "128974848"])
def test_memory_with_a_unit_or_at_least_1mi_is_accepted(text):
    assert w.check_resource_input({"memoryLimit": text}) == {"memoryLimit": text}


def test_the_unitless_rule_is_for_memory_only():
    assert w.check_resource_input({"cpuLimit": "1", "cpuRequest": "500m"})["cpuRequest"] == "500m"


@pytest.mark.parametrize(
    "reason",
    ["a b", "a b", "a‮b", "a‪b", "a⁦b", "a⁩b", "a‎b", "a​b", "a\x85b"],
)
def test_reason_rejects_unicode_line_separators_and_bidi_controls(reason):
    with pytest.raises(RwError) as exc:
        w.clean_reason(reason)
    assert exc.value.code == "invalid_input"


def test_reason_allows_ordinary_unicode():
    assert (
        w.clean_reason("déploiement échoué – retour arrière")
        == "déploiement échoué – retour arrière"
    )


def test_images_are_capped_at_50_in_code():
    spec = {"containers": [{"name": f"c{i}", "image": "i"} for i in range(60)]}
    assert len(w.images(spec)) == 50
    spec["containers"] = spec["containers"][:49]
    spec["initContainers"] = [{"name": f"i{i}", "image": "i"} for i in range(5)]
    got = w.images(spec)
    assert len(got) == 50 and got[-1]["init"] is True


def test_scale_reports_at_most_50_autoscalers():
    hpas = [{"name": f"h{i}"} for i in range(60)]
    plan = w.scale_plan(w.DEPLOYMENT, WEB, 3, hpas, True)
    assert len(plan["hpas"]) == 50


def test_set_resources_reports_whether_an_autoscaler_targets_the_workload():
    ctx = FakeCtx(with_hpa=True)
    out = script("set_resources")(ctx, cpu_request="10m")
    check_outputs("set-resources", out)
    assert out["resources"]["hpaPresent"] is True and out["resources"]["hpaNames"] == ["web"]
    out = script("set_resources")(FakeCtx(), cpu_request="10m")
    assert out["resources"]["hpaPresent"] is False and out["resources"]["hpaNames"] == []


def test_set_resources_still_changes_when_autoscalers_cannot_be_listed():
    ctx = FakeCtx(answers=[("get horizontalpodautoscalers", (1, "", STDERR["permission_denied"]))])
    out = script("set_resources")(ctx, cpu_request="10m")
    assert out["resources"]["hpaPresent"] is None and out["resources"]["applied"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "hpa")]


def test_set_resources_hpa_list_permission_is_declared():
    rules = TASKS["set-resources"]["permissions"]
    assert any("horizontalpodautoscalers" in r["resources"] and "list" in r["verbs"] for r in rules)


def test_schema_caps_match_the_code_caps():
    def schema(name):
        return json.loads((SKILL / "schemas" / f"{name}.v1.json").read_text())

    target = schema("target")["properties"]["gitops"]["properties"]["signals"]
    assert target["maxItems"] == 50
    assert schema("restart")["properties"]["message"]["maxLength"] == 1024
    assert schema("restart")["properties"]["reason"]["maxLength"] == 256
    rollback = schema("rollback")["properties"]
    assert rollback["available"]["maxItems"] == 50
    assert rollback["from"]["properties"]["images"]["maxItems"] == 50
    assert rollback["to"]["properties"]["images"]["maxItems"] == 50
    assert rollback["available"]["items"]["properties"]["images"]["maxItems"] == 50
    assert schema("scale")["properties"]["hpas"]["maxItems"] == 50
    assert schema("resources")["properties"]["hpaNames"]["maxItems"] == 50
    assert schema("resources")["properties"]["fields"]["maxItems"] == 4


def test_effects_name_the_recreate_and_ondelete_strategies():
    for task in ("rollout-restart", "rollback", "set-resources"):
        text = " ".join(TASKS[task]["effects"])
        assert "Recreate" in text and "OnDelete" in text, task
    assert "removes an earlier reason" in " ".join(TASKS["rollout-restart"]["effects"])
