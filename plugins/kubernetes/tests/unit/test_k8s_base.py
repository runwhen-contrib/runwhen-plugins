"""Unit tests for k8s-base/kubectl (INT-8): the pure argument validation in
lib/k8s_base/kubectl_args.py, the error classifier, and the thin script against a fake ctx.
Nothing here calls a cluster."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft202012Validator
from k8s_base import k8s
from k8s_base import kubectl_args as ka
from k8s_base.errors import RwError

SKILL = Path(__file__).parents[2] / "skills" / "k8s-base"


def ok(command, **kw):
    return ka.parse_command(command, **kw)


def code_of(command, **kw):
    with pytest.raises(RwError) as exc:
        ka.parse_command(command, **kw)
    return exc.value.code


# ------------------------------------------------------------ verbs ----


def test_get_pods_normalises_flags():
    assert ok("get pods -n rw-broken -o wide") == [
        "get",
        "pods",
        "--namespace=rw-broken",
        "--output=wide",
    ]


def test_leading_kubectl_word_is_dropped():
    assert ok("kubectl get pods -n x") == ["get", "pods", "--namespace=x"]


@pytest.mark.parametrize(
    "command",
    [
        "get pods",
        "describe deployment web -n rw-healthy",
        "top pods -n x",
        "events -n x",
        "explain deployment.spec",
        "api-resources",
        "logs deploy/web -n rw-healthy --tail=5",
    ],
)
def test_default_verbs_are_accepted(command):
    assert ok(command)[0] == command.split()[0]


@pytest.mark.parametrize(
    "command",
    [
        "delete pod x",
        "apply -f x.yaml",
        "patch deploy web -p {}",
        "scale deploy web --replicas=0",
        "exec -it pod -- sh",
        "edit deploy web",
        "create ns x",
        "run x --image=y",
        "cp pod:/a /b",
        "port-forward pod 8080",
        "proxy",
        "config view",
        "config use-context other",
        "rollout restart deploy web",
        "cordon node1",
        "drain node1",
        "label pod x a=b",
        "annotate pod x a=b",
        "replace -f x",
        "attach pod",
        "debug pod x",
        "certificate approve x",
        "plugin list",
        "krew install x",
        "secrets",
        "pods",
        "-n x get pods",
        "",
        "   ",
    ],
)
def test_other_verbs_are_invalid_input(command):
    assert code_of(command) == "invalid_input"


def test_a_verb_outside_the_table_stays_refused_even_if_the_setting_lists_it():
    for verb in ("delete", "apply", "exec", "patch"):
        assert code_of(f"{verb} pod x", allowed_verbs=[verb, "get"]) == "invalid_input"


def test_a_verb_in_the_table_but_not_in_the_setting_is_refused():
    assert code_of("api-versions") == "invalid_input"
    assert code_of("version") == "invalid_input"
    assert code_of("auth can-i get pods") == "invalid_input"
    assert code_of("rollout history deploy/web") == "invalid_input"
    assert code_of("get pods", allowed_verbs=["describe"]) == "invalid_input"
    assert code_of("get pods", allowed_verbs=[]) == "invalid_input"


def test_the_setting_can_enable_table_verbs_including_two_word_ones():
    verbs = ["get", "api-versions", "version", "auth can-i", "rollout history"]
    assert ok("api-versions", allowed_verbs=verbs) == ["api-versions"]
    assert ok("version -o json --client", allowed_verbs=verbs, allowed_flags=["o", "client"]) == [
        "version",
        "--output=json",
        "--client",
    ]
    assert ok("auth can-i get pods -n x", allowed_verbs=verbs) == [
        "auth",
        "can-i",
        "get",
        "pods",
        "--namespace=x",
    ]
    assert ok("rollout history deploy/web -n x", allowed_verbs=verbs)[:2] == ["rollout", "history"]


def test_auth_can_i_checks_every_position_for_denied_types():
    verbs = ["auth can-i"]
    assert code_of("auth can-i get secrets", allowed_verbs=verbs) == "permission_denied"
    assert code_of("auth can-i get secret/x", allowed_verbs=verbs) == "permission_denied"
    assert ok("auth can-i get pods", allowed_verbs=verbs)[0:2] == ["auth", "can-i"]


def test_non_string_command_is_invalid_input():
    for bad in (None, 5, ["get", "pods"]):
        assert code_of(bad) == "invalid_input"


def test_unbalanced_quote_is_invalid_input():
    assert code_of('get pods -l "a=b') == "invalid_input"


# ---------------------------------------------------------- shell ----


@pytest.mark.parametrize(
    "command",
    [
        "get pods | grep x",
        "get pods ; ls",
        "get pods;",
        "get pods && ls",
        "get pods || ls",
        "get pods > /tmp/x",
        "get pods >> /tmp/x",
        "get pods < /tmp/x",
        "get pods &",
        "get $(whoami)",
        "get `id`",
    ],
)
def test_shell_syntax_is_invalid_input(command):
    assert code_of(command) == "invalid_input"


def test_a_pipe_inside_a_value_is_not_a_pipe():
    assert ok('get pods -l "app in (a,b)"')[-1] == "--selector=app in (a,b)"
    assert ok("get pods -l a=b|c")[-1] == "--selector=a=b|c"
    assert ok("get pods -o jsonpath={.a}|{.b}")[-1] == "--output=jsonpath={.a}|{.b}"


# ------------------------------------------------------- flag forms ----


def test_flag_value_forms_are_equivalent():
    expected = ["get", "pods", "--namespace=x"]
    for form in ("-n x", "-n=x", "-nx", "--namespace x", "--namespace=x"):
        assert ok(f"get pods {form}") == expected, form


def test_flags_before_and_after_the_resource():
    assert ok("get -n x pods") == ok("get pods -n x")
    assert ok("get -o wide -n x pods") == ["get", "pods", "--output=wide", "--namespace=x"]


def test_boolean_flag_forms():
    assert ok("get pods -A") == ["get", "pods", "--all-namespaces"]
    assert ok("get pods --all-namespaces") == ["get", "pods", "--all-namespaces"]
    assert ok("get pods --all-namespaces=true") == ["get", "pods", "--all-namespaces=true"]
    assert ok("get pods --all-namespaces=FALSE") == ["get", "pods", "--all-namespaces=false"]
    assert code_of("get pods --all-namespaces=maybe") == "invalid_input"
    # a boolean flag does not swallow the next word
    assert ok("get -A pods") == ["get", "pods", "--all-namespaces"]
    assert ok("get pods --show-labels x") == ["get", "pods", "x", "--show-labels"]


def test_short_cluster_of_booleans_ending_in_a_value_flag():
    assert ok("get pods -Alapp=web") == [
        "get",
        "pods",
        "--all-namespaces",
        "--selector=app=web",
    ]
    assert ok("logs pod -pc main", allowed_flags=["p", "c"]) == [
        "logs",
        "pod",
        "--previous",
        "--container=main",
        "--tail=200",
        f"--limit-bytes={ka.LOGS_LIMIT_BYTES}",
    ]


def test_value_flag_without_a_value_is_invalid():
    for cmd in ("get pods -n", "get pods --namespace", "get pods -o", "get pods -l"):
        assert code_of(cmd) == "invalid_input", cmd


def test_a_value_that_looks_like_a_flag_is_one_value_not_a_flag():
    # `-n --kubeconfig=/x` must not turn into two flags; the normalised form keeps it one argument
    argv = ok("get pods -n --kubeconfig=/x")
    assert argv == ["get", "pods", "--namespace=--kubeconfig=/x"]
    assert "--kubeconfig=/x" not in argv


def test_flag_values_with_spaces_stay_one_argument():
    assert ok('get pods -l "app in (a, b)"')[-1] == "--selector=app in (a, b)"


def test_double_dash_is_refused():
    assert code_of("get pods -- --kubeconfig=x") == "invalid_input"
    assert code_of("get pods --") == "invalid_input"


def test_output_given_twice_is_refused():
    assert code_of("get pods -o wide -o json") == "invalid_input"


def test_repeatable_selector():
    assert ok("get pods -l a=b -l c=d") == ["get", "pods", "--selector=a=b", "--selector=c=d"]


@pytest.mark.parametrize("token", ["-5", "-", "-1x", "-é"])
def test_dash_tokens_are_not_resources(token):
    assert code_of(f"get pods {token}") == "invalid_input"


def test_flag_names_are_case_sensitive():
    assert code_of("get pods --Namespace=x") == "invalid_input"
    assert code_of("get pods -N x") == "invalid_input"


def test_flags_do_not_abbreviate():
    assert code_of("get pods --names=x") == "invalid_input"
    assert code_of("get pods --all=true") == "invalid_input"


# --------------------------------------------- always-refused flags ----


@pytest.mark.parametrize(
    "flag",
    [
        "--kubeconfig=/etc/x",
        "--kubeconfig /etc/x",
        "--context=prod",
        "--context prod",
        "--cluster=c",
        "--user=u",
        "--server=https://evil",
        "--server https://evil",
        "-s https://evil",
        "-shttps://evil",
        "--token=abc",
        "--token abc",
        "--as=system:admin",
        "--as system:admin",
        "--as-group=system:masters",
        "--as-uid=0",
        "--raw=/api",
        "--raw /api",
        "--insecure-skip-tls-verify",
        "--insecure-skip-tls-verify=true",
        "--certificate-authority=/x",
        "--client-certificate=/x",
        "--client-key=/x",
        "--tls-server-name=x",
        "--username=u",
        "--password=p",
        "--cache-dir=/tmp",
        "--request-timeout=1s",
        "--proxy-url=http://x",
        "--disable-compression",
        "--kuberc=/x",
        "-v=10",
        "-v 10",
        "-v10",
        "--v=10",
        "--vmodule=x=1",
        "--profile=cpu",
        "--warnings-as-errors",
        "--match-server-version",
    ],
)
@pytest.mark.parametrize("verb", ["get pods", "describe pods", "logs pod", "top pods", "events"])
def test_connection_and_identity_flags_are_always_refused(flag, verb):
    assert code_of(f"{verb} {flag}") == "invalid_input"


def test_always_refused_even_when_the_setting_lists_them():
    every = list(ka.FLAGS) + list(ka.ALWAYS_REFUSED) + ["s", "v"]
    for flag in ("--kubeconfig=x", "--context=x", "--server=x", "--token=x", "--as=x", "--raw=/x"):
        assert code_of(f"get pods {flag}", allowed_flags=every) == "invalid_input"
    assert (
        code_of(
            "get pods --insecure-skip-tls-verify",
            allowed_flags=every + ["insecure-skip-tls-verify"],
        )
        == "invalid_input"
    )


def test_the_refusal_names_the_reason():
    with pytest.raises(RwError) as exc:
        ka.parse_command("get pods --kubeconfig=/x")
    assert "never allowed" in exc.value.message


def test_flags_that_read_files_stream_or_write_are_not_in_any_table():
    for flag in (
        "-f x",
        "--filename=x",
        "-w",
        "--watch",
        "--watch-only",
        "-f",
        "--follow",
        "--template=x",
        "--dry-run=client",
        "--output-watch-events",
        "--server-print=false",
        "--subresource=status",
        "-R",
        "--local",
        "--show-managed-fields",
    ):
        assert code_of(f"get pods {flag}", allowed_flags=list(ka.FLAGS) + ["f", "w"]) == (
            "invalid_input"
        ), flag
    assert code_of("logs pod -f") == "invalid_input"
    assert code_of("logs pod --follow") == "invalid_input"


# ------------------------------------------------ per-verb allowlist ----


EVERY_FLAG = list(ka.FLAGS)


def test_flag_must_be_in_the_verbs_own_list():
    # --tail is a flag of logs only
    assert code_of("get pods --tail=5", allowed_flags=EVERY_FLAG) == "invalid_input"
    assert code_of("describe pods -o yaml", allowed_flags=EVERY_FLAG) == "invalid_input"
    assert code_of("top pods -o yaml", allowed_flags=EVERY_FLAG) == "invalid_input"
    assert code_of("logs pod -o yaml", allowed_flags=EVERY_FLAG) == "invalid_input"
    assert code_of("get pods --previous", allowed_flags=EVERY_FLAG) == "invalid_input"
    assert code_of("events --since=1h", allowed_flags=EVERY_FLAG) == "invalid_input"


def test_every_verbs_listed_flags_are_accepted_when_the_setting_allows_all():
    values = {name: "1" for name, (_, takes) in ka.FLAGS.items() if takes}
    values["output"] = None  # checked separately
    for verb, spec in ka.VERBS.items():
        for name in spec["flags"]:
            if name == "output":
                continue
            flag = f"--{name}={values[name]}" if name in values else f"--{name}"
            argv = ka.parse_command(
                f"{verb} {flag}", allowed_verbs=list(ka.VERBS), allowed_flags=EVERY_FLAG
            )
            assert flag in argv, (verb, flag)


def test_setting_narrows_flags_by_long_or_short_name_with_or_without_dashes():
    for allowed in (["n"], ["-n"], ["namespace"], ["--namespace"]):
        assert ok("get pods -n x", allowed_flags=allowed) == ["get", "pods", "--namespace=x"]
        assert code_of("get pods -o wide", allowed_flags=allowed) == "invalid_input"
        assert code_of("get pods -A", allowed_flags=allowed) == "invalid_input"
    assert code_of("get pods -n x", allowed_flags=[]) == "invalid_input"
    assert ok("get pods", allowed_flags=[]) == ["get", "pods"]


def test_default_flags_are_the_contract_defaults():
    assert set(ka.DEFAULT_ALLOWED_FLAGS) >= set(
        "n A l o since tail previous container show-labels sort-by".split()
    )
    assert list(ka.DEFAULT_ALLOWED_VERBS) == (
        "get describe logs top events explain api-resources".split()
    )
    assert ka.DEFAULT_DENIED_RESOURCES == ("secrets",)
    # flags the table has but the default setting does not enable (they change load, not content)
    assert code_of("get pods --chunk-size=5") == "invalid_input"
    assert code_of("logs pod --limit-bytes=5") == "invalid_input"
    assert code_of("logs pod --max-log-requests=5") == "invalid_input"


def test_unknown_names_in_the_setting_enable_nothing():
    assert code_of("get pods -n x", allowed_flags=["bogus", "z"]) == "invalid_input"


def test_the_setting_cannot_add_a_flag_the_verb_lacks():
    assert code_of("get pods --follow", allowed_flags=["follow", "f"]) == "invalid_input"


# ------------------------------------------------------------ -o values ----


@pytest.mark.parametrize(
    "value",
    ["name", "wide", "json", "yaml", "jsonpath={.items[*].metadata.name}", "custom-columns=A:.a"],
)
def test_get_output_values(value):
    assert ok(f"get pods --output={json.dumps(value)}") == [
        "get",
        "pods",
        f"--output={value}",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "jsonpath-file=/etc/passwd",
        "custom-columns-file=/x",
        "go-template={{.}}",
        "go-template-file=/x",
        "template={{.}}",
        "templatefile=/x",
        "jsonpath=",
        "jsonpath",
        "YAML",
        "raw",
        "",
        "wide,json",
        "json ",
    ],
)
def test_get_output_values_outside_the_list_are_refused(value):
    assert code_of(f"get pods --output={json.dumps(value)}") == "invalid_input"


def test_output_values_per_verb():
    verbs = ["events", "explain", "api-resources", "version", "rollout history"]
    assert ok("events -o json", allowed_verbs=verbs, allowed_flags=["o"])
    assert code_of("events -o wide", allowed_verbs=verbs, allowed_flags=["o"]) == "invalid_input"
    assert ok("explain pod -o plaintext", allowed_verbs=verbs, allowed_flags=["o"])
    assert (
        code_of("explain pod -o json", allowed_verbs=verbs, allowed_flags=["o"]) == "invalid_input"
    )
    assert ok("api-resources -o wide", allowed_verbs=verbs, allowed_flags=["o"])
    assert code_of("api-resources -o yaml", allowed_verbs=verbs, allowed_flags=["o"]) == (
        "invalid_input"
    )


# --------------------------------------------------- denied resources ----

SECRET_SPELLINGS = [
    "secret",
    "secrets",
    "Secret",
    "SECRETS",
    "secrets.v1",
    "secret.v1",
    "secrets.v1.",
    "secrets.",
    "secret/x",
    "secrets/x",
    "Secret/x",
    "secrets.v1/x",
    "secrets.v1./x",
    "pods,secrets",
    "secrets,pods",
    "pods,secret/x",
    "configmaps,secrets",
    "configmaps,secrets,pods",
    "pods,,secrets",
    "all,secrets",
]


@pytest.mark.parametrize("spelling", SECRET_SPELLINGS)
@pytest.mark.parametrize("verb", ["get", "describe"])
def test_secrets_in_every_spelling_are_permission_denied(spelling, verb):
    assert code_of(f"{verb} {spelling}") == "permission_denied"
    assert code_of(f"{verb} {spelling} -n x") == "permission_denied"
    assert code_of(f"{verb} -n x {spelling}") == "permission_denied"
    assert code_of(f"{verb} -n x {spelling} some-name") == "permission_denied"


@pytest.mark.parametrize("spelling", SECRET_SPELLINGS[:10])
def test_secrets_with_any_output_or_flag_stay_denied(spelling):
    for flags in (
        "-o yaml",
        "-o json",
        "-o wide",
        "-o name",
        "-o jsonpath={.data}",
        "-o custom-columns=A:.data",
        "-A",
        "-A -o yaml",
        "-l a=b",
        "--show-labels",
        "--sort-by=.metadata.name",
    ):
        assert code_of(f"get {spelling} {flags}") == "permission_denied", flags


def test_secrets_are_denied_for_every_verb_that_takes_a_resource():
    assert code_of("get secrets -o yaml -n rw-healthy") == "permission_denied"
    assert code_of("describe secret db -n x") == "permission_denied"
    assert code_of("explain secrets") == "permission_denied"
    assert code_of("explain secret.data") == "permission_denied"
    assert code_of("events --for=secret/x") == "permission_denied"
    assert code_of("events --for=secret/x", allowed_flags=["for"]) == "permission_denied"
    verbs = list(ka.VERBS)
    assert code_of("events --for=secret/x", allowed_verbs=verbs, allowed_flags=["for"]) == (
        "permission_denied"
    )
    assert code_of("events --for secrets/x", allowed_verbs=verbs, allowed_flags=["for"]) == (
        "permission_denied"
    )
    assert code_of("rollout history secret/x", allowed_verbs=verbs) == "permission_denied"


def test_type_name_pairs_after_the_first_are_checked():
    assert code_of("get pods/a secrets/b") == "permission_denied"
    assert code_of("get deployment/web secret/db") == "permission_denied"


def test_a_name_that_is_secrets_is_not_a_type():
    # `get pods secrets` asks for the pod named 'secrets', which is not a Secret read
    assert ok("get pods secrets -n x") == ["get", "pods", "secrets", "--namespace=x"]
    assert ok("describe deployment secrets") == ["describe", "deployment", "secrets"]
    assert ok("logs deploy/secrets -n x")[:2] == ["logs", "deploy/secrets"]


def test_get_all_and_near_misses_are_allowed():
    assert ok("get all -n rw-healthy") == ["get", "all", "--namespace=rw-healthy"]
    assert ok("get all -A -o yaml") == ["get", "all", "--all-namespaces", "--output=yaml"]
    for resource in (
        "configmaps",
        "configmap/x",
        "cm",
        "secretstores.external-secrets.io",
        "externalsecrets",
        "externalsecrets.external-secrets.io",
        "sealedsecrets",
        "serviceaccounts",
        "sa",
        "pods,services",
        "deployments.apps",
        "deploy/web",
        "pods",
        "pod/x",
        "events",
        "nodes",
        "ingresses",
        "ingress",
        "networkpolicies",
        "csidrivers.storage.k8s.io",
        "secretproviderclasses.secrets-store.csi.x-k8s.io",
    ):
        assert ok(f"get {resource}")[1] == resource, resource


def test_denied_resources_setting_is_configurable():
    assert code_of("get configmaps", denied_resources=["secrets", "configmaps"]) == (
        "permission_denied"
    )
    assert code_of("get cm", denied_resources=["cm"]) == "permission_denied"
    assert code_of("get ConfigMap/x", denied_resources=["configmap"]) == "permission_denied"
    assert code_of("get ingresses", denied_resources=["ingress"]) == "permission_denied"
    assert code_of("get ingress", denied_resources=["ingresses"]) == "permission_denied"
    assert code_of("get secrets.v1", denied_resources=["secrets.v1"]) == "permission_denied"
    assert code_of("get secrets", denied_resources=["Secret"]) == "permission_denied"
    # secrets is a floor: an empty setting, or one without it, still denies it
    assert code_of("get secrets", denied_resources=[]) == "permission_denied"
    assert code_of("get secrets", denied_resources=["configmaps"]) == "permission_denied"


def test_denied_wins_over_other_errors_only_after_flags_are_valid():
    # the first violation wins: an unknown flag is reported before the resource
    assert code_of("get secrets --bogus") == "invalid_input"
    assert code_of("get secrets --kubeconfig=x") == "invalid_input"
    assert code_of("delete secrets") == "invalid_input"


def test_resource_type_and_denied_type_helpers():
    assert ka.resource_type("Secrets.v1./x") == "secrets"
    assert ka.resource_type("secret/x") == "secret"
    assert ka.resource_type("deployments.apps") == "deployments"
    names = ka.denied_names(["secrets"])
    assert ka.denied_type("pods,Secret", names) == "secret"
    assert ka.denied_type("pods", names) is None
    assert ka.denied_type("", names) is None
    assert ka.denied_type("secrets", set()) is None
    assert "secret" in ka.denied_names([]) and "secret" in ka.denied_names([""])


# -------------------------------------------------------------- logs ----


def test_logs_gets_a_default_tail_and_byte_limit():
    assert ok("logs deploy/web -n rw-healthy") == [
        "logs",
        "deploy/web",
        "--namespace=rw-healthy",
        "--tail=200",
        f"--limit-bytes={ka.LOGS_LIMIT_BYTES}",
    ]


def test_logs_keeps_the_callers_window():
    argv = ok("logs deploy/web -n rw-healthy --tail=5")
    assert argv.count("--tail=5") == 1 and "--tail=200" not in argv
    argv = ok("logs pod --since=10m --tail=9")
    assert "--since=10m" in argv and argv.count("--tail=9") == 1
    argv = ok("logs pod --since=10m")  # a window alone tails to maxOutputLines (newest win)
    assert "--tail=2000" in argv and "--tail=200" not in argv


def test_logs_tail_must_be_an_integer():
    assert ok("logs pod --tail=0")[:3] == ["logs", "pod", "--tail=0"]
    for bad in ("--tail=abc", "--tail=", "--tail=1.5", "--tail=1;2", "--tail=-1"):
        assert code_of(f"logs pod {bad}") == "invalid_input", bad


def test_logs_limit_bytes_is_validated_when_enabled():
    flags = ["limit-bytes"]
    assert ok("logs pod --limit-bytes=10", allowed_flags=flags).count("--limit-bytes=10") == 1
    for bad in ("0", "-5", "abc", ""):
        assert code_of(f"logs pod --limit-bytes={bad}", allowed_flags=flags) == "invalid_input"


def test_other_verbs_get_no_defaults():
    assert ok("get pods") == ["get", "pods"]
    assert ok("describe pods") == ["describe", "pods"]


# ----------------------------------------------------------- truncate ----


def test_truncate_output_by_lines():
    text = "".join(f"line {i}\n" for i in range(10))
    out, total, cut = ka.truncate_output(text, 3)
    assert (out, total, cut) == ("line 0\nline 1\nline 2\n", 10, True)
    out, total, cut = ka.truncate_output(text, 10)
    assert (out, total, cut) == (text, 10, False)
    out, total, cut = ka.truncate_output(text, 100)
    assert (total, cut) == (10, False)


def test_truncate_output_empty_and_without_trailing_newline():
    assert ka.truncate_output("", 5) == ("", 0, False)
    assert ka.truncate_output("a\nb", 5) == ("a\nb\n", 2, False)


def test_truncate_output_by_bytes():
    out, total, cut = ka.truncate_output("x" * 100, 5, max_bytes=10)
    assert (out, total, cut) == ("x" * 10, 1, True)
    out, _, cut = ka.truncate_output("é" * 10, 5, max_bytes=5)
    assert cut and out == "éé"  # never a split character


# ------------------------------------------------------ classification ----


@pytest.mark.parametrize(
    ("stderr", "rc", "code"),
    [
        (
            'Error from server (Forbidden): pods is forbidden: User "x" cannot list resource "pods"',
            1,
            "permission_denied",
        ),
        ('Error from server (NotFound): pods "nope" not found', 1, "not_found"),
        (
            "Unable to connect to the server: dial tcp 1.2.3.4:6443: connect: connection refused",
            1,
            "unreachable",
        ),
        ("Unable to connect to the server: dial tcp: lookup x: no such host", 1, "unreachable"),
        (
            "Unable to connect to the server: x509: certificate signed by unknown authority",
            1,
            "unreachable",
        ),
        ("Error: context deadline exceeded", 1, "timeout"),
        ("", 124, "timeout"),
        ("Error from server (TooManyRequests): 429 Too Many Requests", 1, "rate_limited"),
        ('error: the server doesn\'t have a resource type "foo"', 1, "dependency"),
        ("", 127, "dependency"),
        ("something odd", 1, "internal"),
    ],
)
def test_classify_error(stderr, rc, code):
    assert k8s.classify_error(stderr, rc) == code


# ------------------------------------------------------------- script ----


def load_script():
    spec = importlib.util.spec_from_file_location(
        "kubectl_script", SKILL / "scripts" / "kubectl.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = load_script()

PODS_TABLE = "NAME READY STATUS\nweb-1 1/1 Running\nweb-2 0/1 CrashLoopBackOff\n"


class FakeCtx:
    """What the script sees. Raises on a non-zero exit unless check=False, like the SDK."""

    def __init__(self, rc=0, stdout=PODS_TABLE, stderr="", **settings):
        self.settings = SimpleNamespace(
            **{
                "allowed_verbs": list(ka.DEFAULT_ALLOWED_VERBS),
                "denied_resources": list(ka.DEFAULT_DENIED_RESOURCES),
                "allowed_flags": list(ka.DEFAULT_ALLOWED_FLAGS),
                "max_output_lines": 2000,
                **settings,
            }
        )
        self.answer = (rc, stdout, stderr)
        self.calls = []

    def run(self, argv, check=True, **kw):
        self.calls.append(list(argv))
        rc, out, err = self.answer
        if rc != 0 and check:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        return subprocess.CompletedProcess(argv, rc, out, err)


def test_script_runs_the_normalised_argv():
    ctx = FakeCtx()
    result = SCRIPT.main(ctx, "get pods -n rw-broken -o wide")
    assert ctx.calls == [["kubectl", "get", "pods", "--namespace=rw-broken", "--output=wide"]]
    assert result["output"] == PODS_TABLE
    assert result["run"] == {
        "argv": ["kubectl", "get", "pods", "--namespace=rw-broken", "--output=wide"],
        "lines": 3,
        "truncated": False,
        "redactions": 0,
        "stderr": "",
        "stderrTruncated": False,
    }


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("get secrets -n rw-healthy", "permission_denied"),
        ("get pods,secrets", "permission_denied"),
        ("get secret/x -o yaml", "permission_denied"),
        ("delete pod x", "invalid_input"),
        ("get pods --kubeconfig=/x", "invalid_input"),
        ("get pods | sh", "invalid_input"),
        ("", "invalid_input"),
    ],
)
def test_script_refuses_before_any_kubectl_call(command, code):
    ctx = FakeCtx()
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, command)
    assert exc.value.code == code
    assert ctx.calls == []


def test_script_reads_settings():
    ctx = FakeCtx(denied_resources=["secrets", "configmaps"])
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get configmaps -n x")
    assert exc.value.code == "permission_denied" and ctx.calls == []
    ctx = FakeCtx(allowed_verbs=["get"])
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "describe pods")
    assert exc.value.code == "invalid_input" and ctx.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        {"max_output_lines": 0},
        {"max_output_lines": 100001},
        {"max_output_lines": "2000"},
        {"max_output_lines": True},
        {"allowed_verbs": "get"},
        {"denied_resources": None},
        {"allowed_flags": [1]},
    ],
)
def test_script_rejects_bad_settings_before_kubectl(bad):
    ctx = FakeCtx(**bad)
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get pods")
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_script_caps_output_lines():
    ctx = FakeCtx(stdout="".join(f"l{i}\n" for i in range(50)), max_output_lines=5)
    result = SCRIPT.main(ctx, "get pods")
    assert result["output"] == "".join(f"l{i}\n" for i in range(5))
    assert result["run"]["lines"] == 50 and result["run"]["truncated"] is True


def test_script_returns_stderr_of_a_successful_run():
    ctx = FakeCtx(stdout="", stderr="No resources found in rw-empty namespace.\n")
    result = SCRIPT.main(ctx, "get pods -n rw-empty")
    assert result["output"] == ""
    assert result["run"]["stderr"] == "No resources found in rw-empty namespace."
    assert result["run"]["lines"] == 0


def test_script_caps_stderr():
    ctx = FakeCtx(stderr="w" * 5000)
    run = SCRIPT.main(ctx, "get pods")["run"]
    assert len(run["stderr"]) == 1024 and run["stderrTruncated"] is True


@pytest.mark.parametrize(
    ("stderr", "rc", "code"),
    [
        (
            'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource "pods"',
            1,
            "permission_denied",
        ),
        ('Error from server (NotFound): namespaces "nope" not found', 1, "not_found"),
        ("Unable to connect to the server: dial tcp: connection refused", 1, "unreachable"),
        ("context deadline exceeded", 1, "timeout"),
    ],
)
def test_script_failure_raises_with_the_classified_code(stderr, rc, code):
    ctx = FakeCtx(rc=rc, stdout="", stderr=stderr)
    with pytest.raises(RwError) as exc:
        SCRIPT.main(ctx, "get pods -n rw-broken")
    assert exc.value.code == code
    assert "Forbidden" in exc.value.message or code != "permission_denied"


def test_script_timeout_raises_timeout():
    class Slow(FakeCtx):
        def run(self, argv, check=True, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(RwError) as exc:
        SCRIPT.main(Slow(), "get pods")
    assert exc.value.code == "timeout"


def test_script_never_passes_the_original_command_text_to_kubectl():
    ctx = FakeCtx()
    SCRIPT.main(ctx, "get   pods   -nx   -ojson")
    assert ctx.calls == [["kubectl", "get", "pods", "--namespace=x", "--output=json"]]


# ------------------------------------------------- declaration & files ----


def tasks_yaml():
    return yaml.safe_load((SKILL / "tasks.yaml").read_text())


def test_declaration_matches_the_contract_example():
    declared = tasks_yaml()["tasks"][0]
    settings = declared["settings"]
    assert tuple(settings["allowedVerbs"]["default"]) == ka.DEFAULT_ALLOWED_VERBS
    assert tuple(settings["deniedResources"]["default"]) == ka.DEFAULT_DENIED_RESOURCES
    assert tuple(settings["allowedFlags"]["default"]) == ka.DEFAULT_ALLOWED_FLAGS
    assert settings["maxOutputLines"]["default"] == 2000
    assert declared["readOnly"] is True
    assert declared["permissions"] == [
        {"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list", "watch"]},
        {"nonResourceURLs": ["/api", "/apis"], "verbs": ["get"]},  # to resolve CRD names
    ]
    assert list(declared["inputs"]) == ["command"]


def test_permissions_cover_what_the_validated_commands_do():
    # only get/list/watch is ever declared; every allowed verb maps to reads
    verbs = set(tasks_yaml()["tasks"][0]["permissions"][0]["verbs"])
    assert verbs == {"get", "list", "watch"}
    for forbidden in ("create", "update", "patch", "delete", "deletecollection", "*"):
        assert forbidden not in verbs
    # and nothing the table accepts writes: no verb in it is a mutating kubectl verb
    mutating = {
        "delete",
        "apply",
        "create",
        "patch",
        "edit",
        "replace",
        "scale",
        "exec",
        "cp",
        "set",
    }
    assert mutating.isdisjoint({v.split()[0] for v in ka.VERBS})
    assert "rollout history" in ka.VERBS and "rollout" not in ka.VERBS


def test_output_schemas_validate():
    for name in ("output", "run"):
        schema = json.loads((SKILL / "schemas" / f"{name}.v1.json").read_text())
        Draft202012Validator.check_schema(schema)
    result = SCRIPT.main(FakeCtx(), "get pods")
    Draft202012Validator(json.loads((SKILL / "schemas" / "output.v1.json").read_text())).validate(
        result["output"]
    )
    Draft202012Validator(json.loads((SKILL / "schemas" / "run.v1.json").read_text())).validate(
        result["run"]
    )


def test_no_table_flag_is_both_always_refused_and_listed():
    assert not set(ka.FLAGS) & ka.ALWAYS_REFUSED
    for spec in ka.VERBS.values():
        assert not set(spec["flags"]) & ka.ALWAYS_REFUSED
        assert set(spec["flags"]) <= set(ka.FLAGS)
