"""Unit tests for k8s-argocd-health's lib/ and scripts against Application JSON
captured from the kind-rw-plugins scenario (tests/kind/scenarios/30-k8s-argocd-health.*,
a synthetic argoproj.io CRD in namespace rw-argo)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from k8s_argocd_health import app as a
from k8s_argocd_health import k8s
from k8s_argocd_health.errors import RwError
from runwhen_capability.contracts import validate
from runwhen_capability.testing import make_context, run_main, stub_commands

SKILL = Path(__file__).parents[2] / "skills" / "k8s-argocd-health"
DATA = Path(__file__).parent / "data" / "k8s_argocd_health"
NS = "rw-argo"


def load(name):
    return json.loads((DATA / f"{name}.json").read_text())


# ------------------------------------------------------------ sync-status ----


def test_healthy_application_is_synced_and_healthy_with_nothing_wrong():
    app = load("web")
    assert a.sync_view(app)["status"] == "Synced"
    assert a.health_view(app)["status"] == "Healthy"
    assert a.conditions_view(app, 200) == []
    op = a.operation_view(app)
    assert op["phase"] == "Succeeded" and op["retryCount"] == 0 and op["failures"] == []
    view = a.application_view(app)
    assert view["destination"] == {
        "server": "https://kubernetes.default.svc",
        "name": None,
        "namespace": "web",
    }
    assert view["syncPolicy"]["automated"] is True
    assert view["syncPolicy"]["prune"] is True and view["syncPolicy"]["selfHeal"] is True
    assert view["resourceCount"] == 3 and view["operationInProgress"] is False


def test_failed_application_reports_conditions_operation_and_failed_resources():
    app = load("failed-sync")
    assert a.sync_view(app)["status"] == "OutOfSync"
    assert a.sync_view(app)["revisions"] == ["3.1.0", "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"]
    assert a.health_view(app)["status"] == "Missing"
    conds = a.conditions_view(app, 200)
    assert [c["type"] for c in conds] == ["ComparisonError", "SyncError"]
    assert "values file" in conds[0]["message"]
    op = a.operation_view(app)
    assert op["phase"] == "Failed" and op["retryCount"] == 5 and op["automated"] is True
    assert "denied the request" in op["message"]
    assert op["failureCount"] == 1
    assert (op["failures"][0]["kind"], op["failures"][0]["name"], op["failures"][0]["status"]) == (
        "Deployment",
        "api",
        "SyncFailed",
    )
    assert a.application_view(app)["syncPolicy"]["retryLimit"] == 5


def test_start_and_finish_are_not_swapped():
    # The classic task printed finishedAt as the start time and startedAt as the finish time.
    op = a.operation_view(load("failed-sync"))
    assert op["startedAt"] == "2026-11-03T12:57:00Z"
    assert op["finishedAt"] == "2026-11-03T12:59:00Z"


def test_multi_source_application_lists_every_source():
    app = load("failed-sync")
    sources = a.source_views(app)
    assert [(s["index"], s["chart"], s["ref"], s["targetRevision"]) for s in sources] == [
        (0, "api", None, "3.x"),
        (1, None, "values", "main"),
    ]
    assert sources[0]["hasHelm"] is True and sources[1]["hasHelm"] is False
    # a single `source` is a list of one
    assert [s["path"] for s in a.source_views(load("web"))] == ["deploy/web"]
    # `sources` wins over `source` when both are set, as in Argo CD
    both = copy.deepcopy(load("web"))
    both["spec"]["sources"] = [{"repoURL": "https://x.test/r.git", "targetRevision": "v2"}]
    assert [s["targetRevision"] for s in a.source_views(both)] == ["v2"]


def test_manual_sync_policy_has_no_prune_or_self_heal():
    policy = a.application_view(load("drifted"))["syncPolicy"]
    assert policy["automated"] is False
    assert policy["prune"] is None and policy["selfHeal"] is None


def test_automated_with_defaults_is_still_automated():
    # `automated: {}` is falsy in Python but enables automated sync (prune/selfHeal off).
    policy = a.application_view(load("helm-web"))["syncPolicy"]
    assert policy["automated"] is True and policy["prune"] is False and policy["selfHeal"] is False


def test_application_without_status_is_all_null_not_an_error():
    bare = {"metadata": {"name": "new", "namespace": NS}, "spec": {"project": "default"}}
    assert a.sync_view(bare)["status"] is None
    assert a.health_view(bare)["status"] is None
    assert a.operation_view(bare) is None
    assert a.conditions_view(bare, 200) == []
    assert a.source_views(bare) == []
    assert a.unhealthy_resources(bare, 200)["total"] == 0


def test_operation_in_progress_is_the_top_level_operation_field():
    running = copy.deepcopy(load("web"))
    running["operation"] = {"sync": {"revision": "main"}}
    assert a.application_view(running)["operationInProgress"] is True


def test_repo_url_credentials_are_stripped():
    assert a.clean_url("https://ci-bot:fake@git.example.test/org/x.git") == (
        "https://git.example.test/org/x.git"
    )
    assert a.clean_url("git@github.com:org/x.git") == "git@github.com:org/x.git"
    app = copy.deepcopy(load("web"))
    app["spec"]["source"]["repoURL"] = "https://ci-bot:fake@git.example.test/org/x.git"
    assert "fake" not in json.dumps(a.source_views(app))


def test_free_text_is_cut_at_1024_and_flagged():
    app = copy.deepcopy(load("drifted"))
    app["status"]["health"]["message"] = "x" * 5000
    app["status"]["conditions"][0]["message"] = "y" * 5000
    health = a.health_view(app)
    assert len(health["message"]) == 1024 and health["truncated"] is True
    cond = a.conditions_view(app, 200)[0]
    assert len(cond["message"]) == 1024 and cond["truncated"] is True
    assert a.health_view(load("drifted"))["truncated"] is False


def test_lists_are_capped():
    app = copy.deepcopy(load("web"))
    app["status"]["conditions"] = [{"type": f"C{i}", "message": "m"} for i in range(10)]
    assert len(a.conditions_view(app, 3)) == 3
    app["status"]["operationState"]["syncResult"]["resources"] = [
        {"kind": "ConfigMap", "name": f"c{i}", "status": "SyncFailed"} for i in range(80)
    ]
    op = a.operation_view(app)
    assert op["failureCount"] == 80 and len(op["failures"]) == 50


# ------------------------------------------------------ unhealthy-resources ----


def test_drifted_application_lists_degraded_progressing_and_out_of_sync_resources():
    res = a.unhealthy_resources(load("drifted"), 200)
    assert (res["total"], res["matched"]) == (5, 4)
    assert (res["unhealthyCount"], res["outOfSyncCount"]) == (2, 2)
    assert [(r["kind"], r["name"], r["health"], r["syncStatus"]) for r in res["items"]] == [
        ("Deployment", "shop", "Degraded", "Synced"),
        ("Ingress", "shop", "Progressing", "Synced"),
        ("ConfigMap", "shop-config", None, "OutOfSync"),
        ("Service", "shop", "Healthy", "OutOfSync"),
    ]
    assert res["items"][0]["message"] == "Deployment shop exceeded its progress deadline"
    assert res["items"][2]["requiresPruning"] is True
    assert res["items"][1]["group"] == "networking.k8s.io"


def test_out_of_sync_resource_with_no_health_is_listed_unlike_classic():
    # Classic only looked at health.status != Healthy and skipped resources with no health,
    # so an OutOfSync ConfigMap (no health check) never showed.
    res = a.unhealthy_resources(load("drifted"), 200)
    assert "shop-config" in [r["name"] for r in res["items"]]


def test_healthy_application_has_no_unhealthy_resources_but_a_nonzero_total():
    res = a.unhealthy_resources(load("web"), 200)
    assert res == {"total": 3, "matched": 0, "unhealthyCount": 0, "outOfSyncCount": 0, "items": []}


def test_unhealthy_resources_are_capped_but_counted():
    res = a.unhealthy_resources(load("drifted"), 2)
    assert len(res["items"]) == 2 and res["matched"] == 4


def test_missing_resource_of_failed_application_is_reported():
    res = a.unhealthy_resources(load("failed-sync"), 200)
    assert [(r["kind"], r["health"], r["syncStatus"]) for r in res["items"]] == [
        ("Deployment", "Missing", "OutOfSync")
    ]


# ------------------------------------------------------------ helm-source ----


def test_helm_application_reports_chart_release_and_value_file_names():
    view = a.helm_view(load("helm-web"))
    assert view["isHelm"] is True and view["sourceTypes"] == ["Helm"]
    (src,) = view["sources"]
    assert (src["chart"], src["targetRevision"], src["releaseName"]) == ("nginx", "15.x", "web")
    assert src["valueFiles"] == ["values-prod.yaml"]
    assert src["parameterNames"] == ["image.tag"]
    assert src["hasInlineValues"] is True


def test_helm_values_are_never_returned():
    # The Application carries parameter values and inline values: neither may appear.
    out = json.dumps([a.helm_view(load("helm-web")), a.last_deployed(load("helm-web"))])
    assert "v1.2.3" not in out and "replicaCount" not in out
    out = json.dumps([a.helm_view(load("failed-sync")), a.last_deployed(load("failed-sync"))])
    assert "v3.1.0" not in out


def test_multi_source_helm_keeps_only_the_helm_source_with_its_value_files():
    view = a.helm_view(load("failed-sync"))
    assert view["sourceCount"] == 2 and view["isHelm"] is True
    assert [s["index"] for s in view["sources"]] == [0]
    assert view["sources"][0]["valueFiles"] == [
        "$values/envs/prod/values.yaml",
        "values-extra.yaml",
    ]
    assert view["sourceTypes"] == ["Helm", "Directory"]


def test_non_helm_application_is_not_helm():
    view = a.helm_view(load("web"))
    assert (
        view["isHelm"] is False and view["sources"] == [] and view["sourceTypes"] == ["Directory"]
    )


def test_chart_without_a_helm_block_is_a_helm_source():
    app = copy.deepcopy(load("helm-web"))
    del app["spec"]["source"]["helm"]
    (src,) = a.helm_view(app)["sources"]
    assert src["chart"] == "nginx" and src["valueFiles"] == [] and src["releaseName"] is None


def test_path_source_rendered_as_helm_is_helm_by_source_type():
    app = copy.deepcopy(load("web"))
    app["status"]["sourceType"] = "Helm"
    view = a.helm_view(app)
    assert view["isHelm"] is True and view["sources"] == []


def test_last_deployed_is_the_newest_history_entry():
    last = a.last_deployed(load("helm-web"))
    assert last["historyCount"] == 2
    assert last["entry"]["id"] == 1 and last["entry"]["revision"] == "15.2.0"
    assert last["entry"]["deployedAt"] == "2026-11-03T11:00:04Z"
    assert last["entry"]["automated"] is True
    assert last["entry"]["sources"] == [
        {
            "repoURL": "https://charts.example.test",
            "path": None,
            "chart": "nginx",
            "targetRevision": "15.x",
        }
    ]


def test_last_deployed_does_not_trust_list_order():
    app = copy.deepcopy(load("helm-web"))
    app["status"]["history"].reverse()
    assert a.last_deployed(app)["entry"]["revision"] == "15.2.0"


def test_last_deployed_multi_source_uses_revisions():
    entry = a.last_deployed(load("failed-sync"))["entry"]
    assert entry["revisions"] == ["3.0.0", "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"]
    assert len(entry["sources"]) == 2


def test_no_history_is_none_with_a_zero_count():
    app = copy.deepcopy(load("web"))
    del app["status"]["history"]
    assert a.last_deployed(app) == {"historyCount": 0, "entry": None}


# ------------------------------------------------------------- classifier ----


@pytest.mark.parametrize(
    "rc,text,code",
    [
        (
            1,
            'Error from server (Forbidden): applications.argoproj.io "x" is forbidden: User "u" cannot get resource',
            "permission_denied",
        ),
        (1, 'Error from server (NotFound): applications.argoproj.io "nope" not found', "not_found"),
        (
            1,
            "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused",
            "unreachable",
        ),
        (1, "Unable to connect to the server: net/http: TLS handshake timeout", "unreachable"),
        (1, "dial tcp: lookup api.example: no such host", "unreachable"),
        (1, "context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): 429 Too Many Requests", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "applications"', "dependency"),
        (1, "boom", "internal"),
    ],
)
def test_classify(rc, text, code):
    assert k8s.classify(rc, text) == code


# ---------------------------------------------------------------- scripts ----

FORBIDDEN = (
    'Error from server (Forbidden): applications.argoproj.io "%s" is forbidden: User '
    '"system:serviceaccount:rw-healthy:rw-viewer" cannot get resource "applications" in API '
    'group "argoproj.io" in the namespace "rw-argo"'
)
TASKS = ("sync_status", "unhealthy_resources", "helm_source")
SCRIPT_OUTPUTS = {
    "sync_status": {"application", "sync", "health", "conditions", "operation", "sources"},
    "unhealthy_resources": {"resources"},
    "helm_source": {"helm", "lastDeployed"},
}


def script(name):
    spec = importlib.util.spec_from_file_location(
        f"argocd_{name}", SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def argv(name="web", ns=NS):
    return ("kubectl", "-n", ns, "get", "applications.argoproj.io", name, "-o", "json")


def resource(name="web", rtype="kubernetes/application", path=None):
    res = {
        "path": path or f"kubernetes/clusters/kind/namespaces/{NS}/applications/{name}",
        "cluster": "kind",
        "namespace": NS,
        "name": name,
    }
    if rtype:
        res["type"] = rtype
    return res


def run(task, name="web", stub=None, rtype="kubernetes/application", path=None, **settings):
    """Run a script as the executor does; (envelope, errors, argv calls)."""
    with make_context(
        resource=resource(name, rtype, path), settings={"maxItems": 200, **settings}
    ) as tc:
        calls = stub_commands(
            tc,
            {argv(name): stub or (0, json.dumps(load(name)), "")}
            if name in ("web", "helm-web", "drifted", "failed-sync") or stub
            else {},
        )
        env = run_main(script(task), tc)
        return env, tc.errors, list(calls)


def failed(task, name, stub, **kw):
    env, errors, calls = run(task, name, stub=stub, **kw)
    assert env["status"] == "failed", env
    return errors


def test_every_task_returns_its_declared_outputs_and_validates_against_the_schemas():
    tasks = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
    for task in tasks:
        name = task["name"].replace("-", "_")
        for app in ("web", "helm-web", "drifted", "failed-sync"):
            env, errors, _ = run(name, app)
            assert env["status"] == "ok" and errors == []
            assert set(env["outputs"]) == set(task["outputs"]) == SCRIPT_OUTPUTS[name]
            for out, spec in task["outputs"].items():
                schema = json.loads((SKILL / spec["schema"]).read_text())
                assert validate_schema(schema, env["outputs"][out]) == [], (task["name"], app, out)


def validate_schema(schema, value):
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(schema)
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(value)]


def test_broken_application_through_the_script():
    env, errors, calls = run("sync_status", "failed-sync")
    out = env["outputs"]
    assert out["sync"]["status"] == "OutOfSync" and out["health"]["status"] == "Missing"
    assert [c["type"] for c in out["conditions"]] == ["ComparisonError", "SyncError"]
    assert out["operation"]["phase"] == "Failed"
    assert calls == [argv("failed-sync")]


def test_healthy_application_through_the_script():
    out = run("unhealthy_resources", "web")[0]["outputs"]
    assert out["resources"]["matched"] == 0 and out["resources"]["total"] == 3
    out = run("sync_status", "web")[0]["outputs"]
    assert out["sync"]["status"] == "Synced" and out["conditions"] == []


def test_max_items_caps_the_lists_in_the_script():
    out = run("unhealthy_resources", "drifted", maxItems=1)[0]["outputs"]
    assert len(out["resources"]["items"]) == 1 and out["resources"]["matched"] == 4


@pytest.mark.parametrize("task", TASKS)
def test_forbidden_application_fails_with_permission_denied(task):
    errors = failed(task, "web", (1, "", FORBIDDEN % "web"))
    assert [e["code"] for e in errors] == ["permission_denied"]


@pytest.mark.parametrize("task", TASKS)
def test_missing_application_fails_with_not_found_not_an_empty_result(task):
    stub = (1, "", 'Error from server (NotFound): applications.argoproj.io "nope" not found')
    errors = failed(task, "nope", stub)
    assert [e["code"] for e in errors] == ["not_found"]


@pytest.mark.parametrize("task", TASKS)
def test_unreachable_api_server_fails_with_unreachable(task):
    down = "Unable to connect to the server: dial tcp 127.0.0.1:6443: connect: connection refused"
    assert [e["code"] for e in failed(task, "web", (1, "", down))] == ["unreachable"]


@pytest.mark.parametrize("task", TASKS)
def test_missing_crd_fails_with_dependency(task):
    stub = (1, "", 'error: the server doesn\'t have a resource type "applications"')
    assert [e["code"] for e in failed(task, "web", stub)] == ["dependency"]


@pytest.mark.parametrize("task", TASKS)
def test_timeout_fails_with_timeout(task):
    assert [e["code"] for e in failed(task, "web", (124, "", ""))] == ["timeout"]


def test_output_that_is_not_json_is_an_internal_error():
    assert [e["code"] for e in failed("sync_status", "web", (0, "not json", ""))] == ["internal"]


def test_error_text_is_bounded():
    errors = failed("sync_status", "web", (1, "", FORBIDDEN % ("x" * 4000)))
    assert all(len(e["message"]) <= 1100 for e in errors)


@pytest.mark.parametrize("task", TASKS)
def test_wrong_target_type_is_refused_before_any_kubectl_call(task):
    env, errors, calls = run(task, "web", rtype="kubernetes/deployment")
    assert env["status"] == "failed" and [e["code"] for e in errors] == ["invalid_input"]
    assert calls == []


def test_target_type_falls_back_to_the_path():
    env, errors, calls = run("sync_status", "web", rtype=None)
    assert env["status"] == "ok" and calls == [argv("web")]


# ------------------------------------------------------------------ RBAC ----


def rbac_rules(call):
    """The RBAC rule one kubectl argv needs (apiGroup, resource, verb)."""
    words, it = [], iter(call[1:])
    for word in it:
        if word in ("-n", "-o"):
            next(it)
        else:
            words.append(word)
    verb, kind, *rest = words
    assert verb == "get"
    resource, _, group = kind.partition(".")
    return {(group, resource, "get" if rest else "list")}


@pytest.mark.parametrize("task", TASKS)
def test_the_permissions_a_task_declares_cover_what_its_script_calls(task):
    spec = next(
        t
        for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
        if t["name"] == task.replace("_", "-")
    )
    granted = {
        (g, r, v)
        for p in spec["permissions"]
        for g in p["apiGroups"]
        for r in p["resources"]
        for v in p["verbs"]
    }
    _, _, calls = run(task, "drifted")
    needed = set().union(*(rbac_rules(c) for c in calls))
    assert needed and needed <= granted
    assert granted == needed, "a task declares exactly the permissions its script calls"


def test_contracts_validate():
    from runwhen_capability.contracts import load_yaml, split_frontmatter

    assert validate("tasks", load_yaml((SKILL / "tasks.yaml").read_text())) == []
    assert (
        validate("skill-frontmatter", split_frontmatter((SKILL / "SKILL.md").read_text())[0]) == []
    )
    rule = json.loads((SKILL.parents[1] / "integration" / "schemas" / "rbac-rule.json").read_text())
    for task in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]:
        for perm in task["permissions"]:
            assert validate_schema(rule, perm) == []


def test_rw_error_is_importable_from_the_sdk():
    assert issubclass(RwError, Exception) and RwError("not_found", "x").code == "not_found"
