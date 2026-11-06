"""The skill's scripts and kubectl wrapper run against a fake ctx that serves
the captured kind JSON (or a scripted kubectl failure)."""

import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest
from k8s_workload_health import fetch
from k8s_workload_health.k8s import RwError, classify, kubectl_json

SKILL = Path(__file__).parents[2] / "skills" / "k8s-workload-health"
DATA = Path(__file__).parent / "data" / "k8s_workload_health"

PLURAL = {
    "deployments.apps": "deployments",
    "statefulsets.apps": "statefulsets",
    "daemonsets.apps": "daemonsets",
    "replicasets.apps": "replicasets",
    "controllerrevisions.apps": "controllerrevisions",
}


def script(name):
    spec = importlib.util.spec_from_file_location(f"skill_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """ctx.run returns a CompletedProcess without raising, like the installed SDK."""

    def __init__(self, ns, name, rtype="kubernetes/deployment", fail=None, path=None):
        self.ns, self.fail, self.calls, self.errors = ns, fail or {}, [], []
        self.resource = types.SimpleNamespace(namespace=ns, name=name)
        if rtype:
            self.resource.type = rtype
        if path:
            self.resource.path = path
        self.settings = types.SimpleNamespace(max_items=200, restart_threshold=1)

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "message": message, "scope": scope})

    def run(self, argv):
        assert argv[0] == "kubectl" and argv[-2:] == ["-o", "json"]
        resource = argv[argv.index("get") + 1]
        self.calls.append(resource)
        if resource in self.fail:
            rc, err = self.fail[resource]
            return subprocess.CompletedProcess(argv, rc, "", err)
        plural = PLURAL.get(resource, resource)
        items = json.loads((DATA / f"{self.ns[3:]}_{plural}.json").read_text())["items"]
        if (
            resource in PLURAL
            and len(argv) > argv.index("get") + 2
            and argv[argv.index("get") + 2] != "-l"
        ):
            wanted = argv[argv.index("get") + 2]
            hit = [i for i in items if i["metadata"]["name"] == wanted]
            if not hit:
                return subprocess.CompletedProcess(
                    argv, 1, "", f'Error from server (NotFound): {resource} "{wanted}" not found'
                )
            return subprocess.CompletedProcess(argv, 0, json.dumps(hit[0]), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"items": items}), "")


FORBIDDEN = (
    1,
    'Error from server (Forbidden): deployments.apps "crashloop" is forbidden: User "u" cannot get resource "deployments" in API group "apps" in the namespace "rw-broken"',
)
UNREACHABLE = (
    1,
    "Unable to connect to the server: dial tcp 10.0.0.1:443: connect: connection refused",
)
NOTFOUND = (1, 'Error from server (NotFound): deployments.apps "crashloop" not found')


@pytest.mark.parametrize("name", ["rollout_status", "warning_events"])
@pytest.mark.parametrize(
    "failure,code",
    [(FORBIDDEN, "permission_denied"), (NOTFOUND, "not_found"), (UNREACHABLE, "unreachable")],
)
def test_failed_target_fetch_raises_the_classified_error_never_an_empty_result(name, failure, code):
    ctx = FakeCtx("rw-broken", "crashloop", fail={"deployments.apps": failure})
    with pytest.raises(RwError) as e:
        script(name).main(ctx)
    assert e.value.code == code


def test_a_missing_target_raises_not_found_from_the_server():
    with pytest.raises(RwError) as e:
        script("rollout_status").main(FakeCtx("rw-broken", "nope"))
    assert e.value.code == "not_found"


def test_warning_events_fails_when_the_events_list_is_forbidden():
    ctx = FakeCtx("rw-broken", "crashloop", fail={"events": FORBIDDEN})
    with pytest.raises(RwError) as e:
        script("warning_events").main(ctx)
    assert e.value.code == "permission_denied"


def test_rollout_status_keeps_the_facts_and_reports_a_failed_revision_list():
    ctx = FakeCtx("rw-broken", "stuck-rollout", fail={"replicasets.apps": FORBIDDEN})
    out = script("rollout_status").main(ctx)
    assert out["revisions"] == [] and out["rollout"]["progressDeadlineExceeded"] is True
    assert ctx.errors == [
        {"code": "permission_denied", "message": ctx.errors[0]["message"], "scope": "revisions"}
    ]


def test_rollout_status_happy_path_lists_revisions_without_errors():
    ctx = FakeCtx("rw-broken", "stuck-rollout")
    out = script("rollout_status").main(ctx)
    assert [r["revision"] for r in out["revisions"]] == [2, 1] and ctx.errors == []


@pytest.mark.parametrize("name", ["pod_status", "pod_restarts"])
@pytest.mark.parametrize(
    "rtype,ns,target",
    [
        ("kubernetes/statefulset", "rw-healthy", "db"),
        ("kubernetes/daemonset", "rw-healthy", "agent"),
    ],
)
def test_pod_tasks_on_statefulsets_and_daemonsets_do_not_list_controller_revisions(
    name, rtype, ns, target
):
    ctx = FakeCtx(ns, target, rtype=rtype)
    script(name).main(ctx)
    assert "controllerrevisions.apps" not in ctx.calls and ctx.calls == [ctx.calls[0], "pods"]


def test_deployment_pod_tasks_list_replicasets_for_ownership():
    ctx = FakeCtx("rw-healthy", "web")
    out = script("pod_status").main(ctx)
    assert (
        ctx.calls == ["deployments.apps", "replicasets.apps", "pods"]
        and out["counts"]["total"] == 2
    )


def test_warning_events_lists_revisions_for_statefulsets():
    ctx = FakeCtx("rw-healthy", "db", rtype="kubernetes/statefulset")
    script("warning_events").main(ctx)
    assert "controllerrevisions.apps" in ctx.calls


def test_bad_since_is_rejected_before_any_kubectl_call():
    for name in ("pod_restarts", "warning_events", "config_changes"):
        ctx = FakeCtx("rw-broken", "crashloop")
        with pytest.raises(RwError) as e:
            script(name).main(ctx, since="yesterday")
        assert e.value.code == "invalid_input" and ctx.calls == []


def test_target_kind_falls_back_to_the_resource_path():
    ctx = FakeCtx(
        "rw-healthy",
        "db",
        rtype=None,
        path="kubernetes/clusters/c/namespaces/rw-healthy/statefulsets/db",
    )
    assert fetch.target_kind(ctx) == "StatefulSet"
    ctx = FakeCtx(
        "rw-healthy",
        "agent",
        rtype=None,
        path="kubernetes/clusters/c/namespaces/rw-healthy/daemonsets/agent",
    )
    assert fetch.target_kind(ctx) == "DaemonSet"
    assert fetch.TARGETS_BY_PLURAL["deployments"][0] == "Deployment"


def test_unsupported_or_unknown_target_type_is_invalid_input():
    for ctx in (
        FakeCtx("n", "x", rtype="kubernetes/pod"),
        FakeCtx("n", "x", rtype=None),
        FakeCtx("n", "x", rtype=None, path="kubernetes/clusters/c/namespaces/n/pods/x"),
    ):
        with pytest.raises(RwError) as e:
            fetch.target_kind(ctx)
        assert e.value.code == "invalid_input"


@pytest.mark.parametrize(
    "rc,stderr,code",
    [
        (
            1,
            'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods"',
            "permission_denied",
        ),
        (1, "cannot watch resource", "permission_denied"),
        (1, 'Error from server (NotFound): pods "x" not found', "not_found"),
        (1, "Unable to connect to the server: dial tcp: lookup api: no such host", "unreachable"),
        (1, "Unable to connect to the server: dial tcp 1.2.3.4:443: i/o timeout", "unreachable"),
        (1, "dial tcp 1.2.3.4:443: connect: connection refused", "unreachable"),
        (
            1,
            "tls: failed to verify certificate: x509: certificate signed by unknown authority",
            "unreachable",
        ),
        (1, "Error from server (Timeout): context deadline exceeded", "timeout"),
        (124, "", "timeout"),
        (1, "Error from server (TooManyRequests): Too Many Requests", "rate_limited"),
        (1, "the server rejected our request: 429", "rate_limited"),
        (127, "kubectl: command not found", "dependency"),
        (1, 'error: the server doesn\'t have a resource type "foos"', "dependency"),
        (1, "something odd", "internal"),
    ],
)
def test_classify(rc, stderr, code):
    assert classify(rc, stderr) == code


def test_kubectl_json_parses_stdout_on_success_and_raises_with_code_on_failure():
    ok = types.SimpleNamespace(
        run=lambda argv: subprocess.CompletedProcess(argv, 0, '{"a": 1}', "")
    )
    assert kubectl_json(ok, "get", "x") == {"a": 1}
    bad = types.SimpleNamespace(
        run=lambda argv: subprocess.CompletedProcess(argv, 1, "", "Forbidden")
    )
    with pytest.raises(RwError) as e:
        kubectl_json(bad, "get", "x")
    assert e.value.code == "permission_denied" and "Forbidden" in e.value.message


def test_kubectl_json_rejects_non_json_output():
    junk = types.SimpleNamespace(
        run=lambda argv: subprocess.CompletedProcess(argv, 0, "not json", "")
    )
    with pytest.raises(RwError) as e:
        kubectl_json(junk, "get", "x")
    assert e.value.code == "internal"
