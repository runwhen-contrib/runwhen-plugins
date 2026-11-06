"""Review fixes for k8s-base/kubectl: alias-aware denial, the secrets floor, redaction,
logs limits, stderr classification and input bounds."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_base import k8s, redact, resources
from k8s_base import kubectl_args as ka
from k8s_base.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-base"
BOTH = ["secrets", "configmaps"]


def code_of(command, **kw):
    with pytest.raises(RwError) as exc:
        ka.parse_command(command, **kw)
    return exc.value.code


def load_script():
    spec = importlib.util.spec_from_file_location(
        "kubectl_script2", SKILL / "scripts" / "kubectl.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = load_script()


class FakeCtx:
    """Answers by the first matching rule: (substring of the joined argv, (rc, out, err))."""

    def __init__(self, answers=(), **settings):
        self.settings = SimpleNamespace(
            **{
                "allowed_verbs": list(ka.DEFAULT_ALLOWED_VERBS),
                "denied_resources": list(ka.DEFAULT_DENIED_RESOURCES),
                "allowed_flags": list(ka.DEFAULT_ALLOWED_FLAGS),
                "max_output_lines": 2000,
                **settings,
            }
        )
        self.answers = list(answers)
        self.calls = []
        self.kwargs = []

    def run(self, argv, check=True, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
        joined = " ".join(argv)
        for needle, answer in self.answers:
            if needle in joined:
                if isinstance(answer, Exception):
                    raise answer
                rc, out, err = answer
                break
        else:
            rc, out, err = 0, "NAME\nx\n", ""
        if rc != 0 and check:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        return subprocess.CompletedProcess(argv, rc, out, err)


# ------------------------------------------------------------------ M1 ----


@pytest.mark.parametrize(
    "command",
    [
        "get cm -A -o yaml",
        "get cm",
        "describe cm x",
        "describe cm x -n y",
        "get po,cm",
        "get cm/x",
        "get configmap/x -o yaml",
        "get configmaps.v1",
        "get ConfigMaps",
        "get pods,cm,svc",
        "get cm,pods -n x -o jsonpath={.items[*].data}",
        "get pods cm/x",
    ],
)
def test_configmap_aliases_are_denied_when_configmaps_are(command):
    assert code_of(command, denied_resources=BOTH) == "permission_denied"


def test_configmaps_stay_readable_by_default():
    assert ka.parse_command("get cm -A -o yaml")[1] == "cm"


@pytest.mark.parametrize("alias", ["networkpolicies", "networkpolicy", "netpol", "NetPol"])
@pytest.mark.parametrize("denied", ["networkpolicies", "netpol", "networkpolicy"])
def test_networkpolicy_spellings_all_match(alias, denied):
    assert code_of(f"get {alias}", denied_resources=[denied]) == "permission_denied"
    assert code_of(f"get {alias}/x", denied_resources=[denied]) == "permission_denied"


@pytest.mark.parametrize(
    ("alias", "full"),
    [
        ("sa", "serviceaccounts"),
        ("pvc", "persistentvolumeclaims"),
        ("pv", "persistentvolumes"),
        ("ns", "namespaces"),
        ("svc", "services"),
        ("po", "pods"),
        ("deploy", "deployments"),
        ("ds", "daemonsets"),
        ("sts", "statefulsets"),
        ("rs", "replicasets"),
        ("ing", "ingresses"),
        ("csr", "certificatesigningrequests"),
        ("sc", "storageclasses"),
        ("ep", "endpoints"),
        ("cj", "cronjobs"),
        ("hpa", "horizontalpodautoscalers"),
        ("pdb", "poddisruptionbudgets"),
        ("no", "nodes"),
        ("ev", "events"),
        ("crd", "customresourcedefinitions"),
        ("quota", "resourcequotas"),
        ("limits", "limitranges"),
        ("rc", "replicationcontrollers"),
        ("pc", "priorityclasses"),
    ],
)
def test_builtin_short_names_and_plurals_resolve_both_ways(alias, full):
    assert code_of(f"get {alias}", denied_resources=[full]) == "permission_denied"
    assert code_of(f"get {full}", denied_resources=[alias]) == "permission_denied"
    assert code_of(f"get {alias}/x", denied_resources=[full]) == "permission_denied"


@pytest.mark.parametrize(
    ("singular", "plural"),
    [
        ("ingress", "ingresses"),
        ("networkpolicy", "networkpolicies"),
        ("storageclass", "storageclasses"),
        ("priorityclass", "priorityclasses"),
        ("endpoints", "endpoints"),
        ("endpoint", "endpoints"),
        ("podsecuritypolicy", "podsecuritypolicies"),
        ("ingressclass", "ingressclasses"),
        ("limitrange", "limitranges"),
    ],
)
def test_y_ies_and_es_plurals(singular, plural):
    assert code_of(f"get {singular}", denied_resources=[plural]) == "permission_denied"
    assert code_of(f"get {plural}", denied_resources=[singular]) == "permission_denied"


def test_table_entries_are_consistent():
    seen = {}
    for plural, singular, shorts in resources.BUILTIN:
        names = {plural, singular, *shorts}
        for n in names:
            assert n == n.lower()
            # the same name never belongs to two resources
            assert seen.setdefault(n, plural) == plural, n
    for must in ("secrets", "configmaps", "pods", "networkpolicies", "ingresses", "nodes"):
        assert must in {p for p, _, _ in resources.BUILTIN}


def test_unrelated_names_are_not_swept_up_by_aliasing():
    for resource in (
        "pods",
        "services",
        "deployments",
        "externalsecrets",
        "secretstores",
        "cronjobs",
    ):
        assert ka.parse_command(f"get {resource}", denied_resources=BOTH)[1] == resource
    assert ka.parse_command("get all", denied_resources=BOTH)[1] == "all"
    assert ka.parse_command("get pods cm -n x", denied_resources=BOTH)[:3] == ["get", "pods", "cm"]


ROWS = """\
NAME                      SHORTNAMES   APIVERSION                       NAMESPACED   KIND                    VERBS                                                      CATEGORIES
configmaps                cm           v1                               true         ConfigMap               create,delete,get,list,patch,update,watch
externalsecrets           es           external-secrets.io/v1beta1      true         ExternalSecret          delete,get,list,patch,update,watch                         external-secrets
widgets                                example.com/v1                   true         Widget                  get,list
gadgets                   gd,gdg       example.com/v1                   false        Gadget                  get,list
"""


def test_parse_api_resources_reads_columns_by_position():
    rows = resources.parse_api_resources(ROWS)
    by_name = {r["name"]: r for r in rows}
    assert by_name["widgets"]["shortnames"] == [] and by_name["widgets"]["kind"] == "Widget"
    assert by_name["gadgets"]["shortnames"] == ["gd", "gdg"]
    assert by_name["externalsecrets"]["shortnames"] == ["es"]
    assert by_name["externalsecrets"]["kind"] == "ExternalSecret"
    assert len(rows) == 4
    assert resources.parse_api_resources("") == []


def test_unknown_names_are_resolved_once_through_the_lookup():
    calls = []

    def lookup():
        calls.append(1)
        return resources.parse_api_resources(ROWS)

    denied = ["secrets", "externalsecrets", "widgets", "gd"]
    names = resources.expand_denied(denied, lookup)
    assert {
        "externalsecrets",
        "es",
        "externalsecret",
        "widgets",
        "widget",
        "gd",
        "gdg",
        "gadget",
    } <= names
    assert {"secrets", "secret"} <= names
    assert len(calls) == 1


def test_lookup_is_not_called_when_every_name_is_builtin():
    def lookup():
        raise AssertionError("not needed")

    assert "cm" in resources.expand_denied(["secrets", "configmaps"], lookup)


def test_dynamic_names_deny_every_alias_in_a_command():
    lookup = lambda: resources.parse_api_resources(ROWS)  # noqa: E731
    for alias in (
        "es",
        "externalsecret",
        "externalsecrets",
        "externalsecrets.external-secrets.io",
        "ES/x",
    ):
        assert (
            code_of(f"get {alias}", denied_resources=["externalsecrets"], resolver=lookup)
            == "permission_denied"
        ), alias
    assert ka.parse_command("get pods", denied_resources=["externalsecrets"], resolver=lookup)


def test_unresolvable_denied_name_fails_closed():
    lookup = lambda: resources.parse_api_resources(ROWS)  # noqa: E731
    assert code_of("get pods", denied_resources=["nosuchthing"], resolver=lookup) == "invalid_input"
    assert code_of("get pods", denied_resources=["nosuchthing"]) == "invalid_input"  # no resolver

    def broken():
        raise RwError("unreachable", "cannot reach the cluster")

    with pytest.raises(RwError) as exc:
        ka.parse_command("get pods", denied_resources=["externalsecrets"], resolver=broken)
    assert exc.value.code in ("invalid_input", "unreachable")


def test_script_resolves_unknown_denied_names_with_api_resources():
    ctx = FakeCtx(
        answers=[("api-resources", (0, ROWS, ""))],
        denied_resources=["secrets", "externalsecrets"],
    )
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get es -n x")
    assert exc.value.code == "permission_denied"
    assert ctx.calls == [["kubectl", "api-resources", "-o", "wide"]]
    ctx = FakeCtx(
        answers=[("api-resources", (0, ROWS, ""))],
        denied_resources=["externalsecrets"],
    )
    SCRIPT.main(ctx, "get pods -n x")
    assert [c[1] for c in ctx.calls] == ["api-resources", "get"]


def test_script_fails_closed_when_api_resources_fails():
    ctx = FakeCtx(
        answers=[("api-resources", (1, "", "Unable to connect to the server: connection refused"))],
        denied_resources=["externalsecrets"],
    )
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get pods")
    assert exc.value.code in ("invalid_input", "unreachable")
    assert [c[1] for c in ctx.calls] == ["api-resources"]  # the command itself never ran


def test_script_does_not_look_anything_up_for_builtin_names():
    ctx = FakeCtx(denied_resources=["secrets", "configmaps", "cm", "netpol"])
    SCRIPT.main(ctx, "get pods")
    assert [c[1] for c in ctx.calls] == ["get"]


# ------------------------------------------------------------------ L5 ----


@pytest.mark.parametrize(
    "spelling", ["secrets", "secret", "Secret/x", "pods,secrets", "secrets.v1"]
)
def test_secrets_are_denied_even_with_an_empty_setting(spelling):
    assert code_of(f"get {spelling}", denied_resources=[]) == "permission_denied"
    assert code_of(f"describe {spelling}", denied_resources=["configmaps"]) == "permission_denied"


def test_script_floor_with_an_empty_setting():
    ctx = FakeCtx(denied_resources=[])
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get secrets -n x")
    assert exc.value.code == "permission_denied" and ctx.calls == []


# ------------------------------------------------------------------ M2 ----


def test_redact_patterns():
    assert (
        redact.redact_text("Authorization: Bearer abc.def-123")
        == "Authorization: Bearer [REDACTED]"
    )
    assert (
        redact.redact_text("authorization=basic dXNlcjpwYXNz") == "authorization=basic [REDACTED]"
    )
    assert redact.redact_text("curl -H 'Bearer abcdefghijkl'") == "curl -H 'Bearer [REDACTED]'"
    assert redact.redact_text("password=hunter2 user=a") == "password=[REDACTED] user=a"
    assert redact.redact_text('"passwd": "hunter2"') == '"passwd": "[REDACTED]"'
    assert redact.redact_text("SECRET: s3cr3t") == "SECRET: [REDACTED]"
    assert redact.redact_text("token = abc123") == "token = [REDACTED]"
    assert redact.redact_text("api_key=zzz&x=1") == "api_key=[REDACTED]&x=1"
    assert redact.redact_text("API-KEY: zzz") == "API-KEY: [REDACTED]"
    assert redact.redact_text("key AKIAABCDEFGHIJKLMNOP end") == "key [REDACTED] end"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----"
    assert redact.redact_text(f"a\n{pem}\nb") == "a\n[REDACTED]\nb"
    assert redact.redact_text("-----BEGIN PRIVATE KEY-----\nMIIabc") == "[REDACTED]"  # cut block
    assert "MIIabc" not in redact.redact_text("x -----BEGIN EC PRIVATE KEY-----\nMIIabc\n")


def test_redact_leaves_ordinary_text_and_counts():
    text = "NAME READY\nsecretName: x\nSecrets: foo-token-abc\nbasic configuration\nmountPath: /var/run/secrets/x\n"
    assert redact.redact_text(text) == text
    out, n = redact.redact_counted("password=a\ntoken=b\nok\n")
    assert out == "password=[REDACTED]\ntoken=[REDACTED]\nok\n" and n == 2
    assert redact.redact_counted("") == ("", 0)


def test_redaction_does_not_cross_lines():
    text = "token:\nkind: Pod\n"
    assert redact.redact_text(text) == text


def test_script_redacts_output_and_stderr():
    out = "env:\n  DB_PASSWORD=hunter2\nAuthorization: Bearer abc.def\nAKIAABCDEFGHIJKLMNOP\n"
    ctx = FakeCtx(answers=[("describe", (0, out, "warn token=xyz\n"))])
    result = SCRIPT.main(ctx, "describe pod x")
    assert "hunter2" not in result["output"] and "abc.def" not in result["output"]
    assert "AKIA" not in result["output"] and "[REDACTED]" in result["output"]
    assert "xyz" not in result["run"]["stderr"]
    assert result["run"]["redactions"] >= 3


def test_pem_block_does_not_survive_a_line_cut():
    pem = "-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n" * 1 + "-----END PRIVATE KEY-----\n"
    ctx = FakeCtx(answers=[("get", (0, "x\n" + pem, ""))], max_output_lines=2)
    result = SCRIPT.main(ctx, "get pods")
    assert "A" * 10 not in result["output"] and "PRIVATE KEY" not in result["output"]


# ------------------------------------------------------------------ M3 ----


def test_logs_since_without_tail_tails_to_max_output_lines():
    argv = ka.parse_command("logs pod --since=1h", max_output_lines=300)
    assert "--tail=300" in argv and "--since=1h" in argv
    argv = ka.parse_command("logs pod --since-time=2026-01-01T00:00:00Z", max_output_lines=300)
    assert "--tail=300" in argv
    assert "--tail=200" in ka.parse_command("logs pod", max_output_lines=300)
    argv = ka.parse_command("logs pod --since=1h --tail=7", max_output_lines=300)
    assert argv.count("--tail=7") == 1 and "--tail=300" not in argv


def test_logs_negative_tail_is_refused():
    for bad in ("--tail=-1", "--tail=-5", "--tail -1"):
        assert code_of(f"logs pod {bad}") == "invalid_input", bad
    assert "--tail=0" in ka.parse_command("logs pod --tail=0")


def test_logs_truncated_when_the_byte_limit_was_reached():
    big = "x" * 100 + "\n"
    out = big * (ka.LOGS_LIMIT_BYTES // len(big) + 1)
    ctx = FakeCtx(answers=[("logs", (0, out, ""))], max_output_lines=100000)
    result = SCRIPT.main(ctx, "logs pod -n x")
    assert result["run"]["truncated"] is True
    ctx = FakeCtx(answers=[("logs", (0, "a\nb\n", ""))])
    assert SCRIPT.main(ctx, "logs pod -n x")["run"]["truncated"] is False


def test_logs_keep_the_newest_lines_when_cut_by_lines():
    ctx = FakeCtx(
        answers=[("logs", (0, "".join(f"l{i}\n" for i in range(10)), ""))], max_output_lines=3
    )
    result = SCRIPT.main(ctx, "logs pod --tail=10")
    assert result["output"] == "l7\nl8\nl9\n" and result["run"]["truncated"] is True
    ctx = FakeCtx(
        answers=[("get", (0, "".join(f"l{i}\n" for i in range(10)), ""))], max_output_lines=3
    )
    assert SCRIPT.main(ctx, "get pods")["output"] == "l0\nl1\nl2\n"


def test_script_passes_the_since_tail_default():
    ctx = FakeCtx(answers=[("logs", (0, "a\n", ""))], max_output_lines=123)
    result = SCRIPT.main(ctx, "logs pod --since=5m")
    assert "--tail=123" in result["run"]["argv"]


# ------------------------------------------------------------------ M4 ----


@pytest.mark.parametrize(
    ("stderr", "rc", "code"),
    [
        ('Error from server (NotFound): pods "forbidden-app" not found', 1, "not_found"),
        ('Error from server (NotFound): pods "cannot-get-app" not found', 1, "not_found"),
        ('Error from server (NotFound): pods "unable-to-connect" not found', 1, "not_found"),
        ('Error from server (NotFound): pods "x-timeout-i/o timeout" not found', 1, "not_found"),
        (
            'Error from server (Forbidden): pods "not-found" is forbidden: User "u" cannot get',
            1,
            "permission_denied",
        ),
        ("Error from server (Unauthorized): Unauthorized", 1, "permission_denied"),
        ("Error from server (TooManyRequests): too many", 1, "rate_limited"),
        (
            "Error from server (Timeout): request did not complete within requested timeout",
            1,
            "timeout",
        ),
        (
            "Error from server (ServiceUnavailable): the server is currently unable",
            1,
            "unreachable",
        ),
        (
            'Error from server (BadRequest): container "c" in pod "p" is waiting to start',
            1,
            "invalid_input",
        ),
        ("error: unknown flag: --x\nSee 'kubectl get --help' for usage.", 1, "invalid_input"),
        ("error: you must specify the type of resource to get", 1, "invalid_input"),
        ("error: Metrics API not available", 1, "dependency"),
        ('error: the server doesn\'t have a resource type "foos"', 1, "dependency"),
        ("Error from server (InternalError): something broke", 1, "internal"),
        ('warning: x\nError from server (NotFound): pods "a" not found', 1, "not_found"),
        ("error: You must be logged in to the server (Unauthorized)", 1, "permission_denied"),
        ("Unable to connect to the server: dial tcp: connection refused", 1, "unreachable"),
        ("", 124, "timeout"),
        ("", 127, "dependency"),
    ],
)
def test_classification_by_server_error_prefix(stderr, rc, code):
    assert k8s.classify_error(stderr, rc) == code


def test_get_pod_forbidden_app_not_found_end_to_end():
    ctx = FakeCtx(
        answers=[("get", (1, "", 'Error from server (NotFound): pods "forbidden-app" not found'))]
    )
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get pod forbidden-app -n x")
    assert exc.value.code == "not_found"


# ----------------------------------------------------------- L1 - L4 ----


def test_nul_and_overlong_commands_are_invalid_input():
    assert code_of("get pods\x00 -n x") == "invalid_input"
    assert code_of("get pods " + "a" * 4090) == "invalid_input"
    assert code_of("get " + "p" * 4093) == "invalid_input"  # 4097 chars
    assert ka.parse_command("get " + "p" * 4092)[1] == "p" * 4092  # exactly 4096


def test_input_declares_a_nul_free_pattern():
    task = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"][0]
    pattern = task["inputs"]["command"]["pattern"]
    # RE2 caps a repetition at 1000, so the pattern only keeps NUL out; the script enforces 4096
    import re

    assert "x00" in pattern
    assert re.fullmatch(pattern, "get pods") and not re.fullmatch(pattern, "get\x00pods")
    assert not re.fullmatch(pattern, "")


def test_output_too_large_is_invalid_input():
    from runwhen_capability.errors import OutputTooLargeError

    ctx = FakeCtx(answers=[("get", OutputTooLargeError("output exceeds >N bytes"))])
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get pods -A -o yaml")
    assert exc.value.code == "invalid_input" and "narrow" in exc.value.message


def test_ctx_run_gets_a_timeout():
    ctx = FakeCtx()
    SCRIPT.main(ctx, "get pods")
    assert ctx.kwargs[0].get("timeout") == 50


def test_run_schema_has_bounds():
    run = json.loads((SKILL / "schemas" / "run.v1.json").read_text())
    assert run["properties"]["argv"]["maxItems"] and run["properties"]["argv"]["items"]["maxLength"]
    out = json.loads((SKILL / "schemas" / "output.v1.json").read_text())
    assert out["maxLength"] == 1048576
    result = SCRIPT.main(FakeCtx(), "get pods")
    Draft202012Validator(run).validate(result["run"])
    Draft202012Validator(out).validate(result["output"])
    assert "redactions" in run["required"]


def test_longest_command_fits_the_argv_bound():
    run = json.loads((SKILL / "schemas" / "run.v1.json").read_text())
    command = "get pods " + "a " * 2000  # 4009 chars, 2002 tokens
    result = SCRIPT.main(FakeCtx(), command)
    Draft202012Validator(run).validate(result["run"])
    assert len(result["run"]["argv"]) <= run["properties"]["argv"]["maxItems"]


# ------------------------------------------------------------- defaults ----


def test_default_allowed_flags_extended():
    expected = (
        "n A l o since tail previous container show-labels sort-by field-selector no-headers "
        "timestamps since-time all-containers prefix for types show-kind label-columns "
        "ignore-not-found containers sum api-group namespaced verbs api-version recursive "
        "show-events"
    ).split()
    assert sorted(ka.DEFAULT_ALLOWED_FLAGS) == sorted(expected)
    for off in ("chunk-size", "max-log-requests", "limit-bytes"):
        assert off not in ka.DEFAULT_ALLOWED_FLAGS
        assert code_of(f"get pods --{off}=1") == "invalid_input"
    task = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"][0]
    assert sorted(task["settings"]["allowedFlags"]["default"]) == sorted(expected)
    # the newly default flags work
    assert ka.parse_command("get pods --field-selector=status.phase=Running")
    assert ka.parse_command("logs pod --timestamps --prefix --all-containers")
    assert ka.parse_command("describe pods --show-events=false")


def test_declares_discovery_urls_for_the_lookup():
    perms = yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"][0]["permissions"]
    assert {"nonResourceURLs": ["/api", "/apis"], "verbs": ["get"]} in perms


@pytest.mark.parametrize("flag", ["-v=10", "-v 10", "--v=6", "--vmodule=x=1"])
def test_verbosity_refusal_names_the_real_reason(flag):
    with pytest.raises(RwError) as exc:
        ka.parse_command(f"get pods {flag}")
    assert "verbosity" in exc.value.message and "cluster, identity" not in exc.value.message
