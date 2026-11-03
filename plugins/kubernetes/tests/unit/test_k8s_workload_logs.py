"""Unit tests for k8s-workload-logs against JSON and log text captured from the
kind-rw-plugins scenarios (plugins/kubernetes/tests/kind, 20-broken.yaml and
30-k8s-workload-logs.yaml). The `noisy` Deployment prints one trace per language
in a loop; `crashloop` is waiting in CrashLoopBackOff with a previous run."""

import importlib.util
import json
import re
import subprocess
import types
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_workload_logs import fetch, logs, workload
from k8s_workload_logs.errors import RwError
from k8s_workload_logs.k8s import classify_error

HERE = Path(__file__).parent
DATA = HERE / "data" / "k8s_workload_logs"
SKILL = HERE.parents[1] / "skills" / "k8s-workload-logs"
TASKS = yaml.safe_load((SKILL / "tasks.yaml").read_text())
SETTINGS = {k: v["default"] for k, v in TASKS["settings"].items()}


def load(ns, resource):
    return json.loads((DATA / f"{ns}_{resource}.json").read_text())["items"]


def named(items, name):
    return next(i for i in items if i["metadata"]["name"] == name)


def owned(ns, kind, name):
    plural = {
        "Deployment": "deployments",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
    }[kind]
    wl = named(load(ns, plural), name)
    replicasets = load(ns, "replicasets") if kind == "Deployment" else []
    return workload.owned_pods(kind, wl, replicasets, load(ns, "pods"))


def text(name):
    return (DATA / name).read_text()


def stream(pod="p", container="c", previous=False, init=False):
    return {"pod": pod, "container": container, "init": init, "previous": previous}


# ---------------------------------------------------------------- planning ----


def test_owned_pods_ignores_the_old_replicaset():
    pods = owned("broken", "Deployment", "noisy")
    assert [p["metadata"]["name"] for p in pods] == ["noisy-7cc85fc74-bzrdb"]


def test_plan_reads_every_container_of_a_running_pod():
    plan = logs.plan_streams(owned("broken", "Deployment", "noisy"))
    assert [(s["container"], s["previous"]) for s in plan["streams"]] == [
        ("app", False),
        ("quiet", False),
    ]
    assert plan["unavailable"] == [] and plan["pods"] == plan["podsSelected"] == 1


def test_plan_crashlooping_container_reads_previous_and_says_why_current_is_unavailable():
    plan = logs.plan_streams(owned("broken", "Deployment", "crashloop"))
    assert [(s["container"], s["previous"]) for s in plan["streams"]] == [("app", True)]
    assert [(u["container"], u["previous"], u["reason"]) for u in plan["unavailable"]] == [
        ("app", False, "container is waiting: CrashLoopBackOff")
    ]


def test_plan_image_pull_backoff_has_no_logs_and_is_listed_not_skipped():
    plan = logs.plan_streams(owned("broken", "Deployment", "bad-image"))
    assert plan["streams"] == []
    assert plan["unavailable"][0]["reason"].startswith("container is waiting: ")


def test_plan_exclude_and_only():
    pods = owned("broken", "Deployment", "noisy")
    plan = logs.plan_streams(pods, exclude=["quiet"])
    assert [s["container"] for s in plan["streams"]] == ["app"] and plan["excluded"] == ["quiet"]
    plan = logs.plan_streams(pods, only="quiet")
    assert [s["container"] for s in plan["streams"]] == ["quiet"] and plan["excluded"] == []


def test_plan_reads_restarting_pods_first_and_reports_the_pods_it_skipped():
    pods = load("broken", "pods")
    plan = logs.plan_streams(pods, max_pods=1)
    assert plan["pods"] == len(pods) and plan["podsSelected"] == 1
    assert plan["streams"][0]["pod"] in {"crashloop-77d67d4789-f6rbl", "broken-sts-0"}


def test_plan_pending_pod_without_container_status():
    pod = {
        "metadata": {"name": "p"},
        "spec": {"containers": [{"name": "c"}]},
        "status": {"phase": "Pending"},
    }
    plan = logs.plan_streams([pod])
    assert plan["streams"] == [] and "no container status yet" in plan["unavailable"][0]["reason"]


def test_plan_init_containers_are_read_and_marked_init():
    pod = {
        "metadata": {"name": "p"},
        "spec": {"initContainers": [{"name": "i"}], "containers": [{"name": "c"}]},
        "status": {
            "initContainerStatuses": [
                {"name": "i", "restartCount": 0, "state": {"terminated": {"exitCode": 0}}}
            ],
            "containerStatuses": [{"name": "c", "restartCount": 0, "state": {"running": {}}}],
        },
    }
    assert [(s["container"], s["init"]) for s in logs.plan_streams([pod])["streams"]] == [
        ("i", True),
        ("c", False),
    ]


# ------------------------------------------------------------- recent logs ----


def test_allocate_gives_small_streams_all_and_splits_the_rest():
    assert logs.allocate([5, 100, 100], 105) == [5, 50, 50]
    assert logs.allocate([10, 10], 100) == [10, 10]
    assert logs.allocate([10, 10], 0) == [0, 0]


def test_recent_logs_keeps_the_newest_lines_and_flags_truncation():
    fetched = [
        (stream("a"), "\n".join(f"a{i}" for i in range(10)) + "\n"),
        (stream("b"), "b0\nb1\n"),
    ]
    sections, index = logs.recent_logs(fetched, 6)
    assert sections[0][1] == ["a6", "a7", "a8", "a9"] and sections[1][1] == ["b0", "b1"]
    assert [(i["lines"], i["linesFetched"], i["truncated"]) for i in index] == [
        (4, 10, True),
        (2, 2, False),
    ]


def test_clip_cuts_lines_to_1024_and_says_so():
    assert logs.clip("short") == ("short", False)
    clipped, cut = logs.clip("x" * 5000)
    assert len(clipped) == 1024 and clipped.endswith("...") and cut


def test_recent_logs_clips_long_lines_and_flags_the_stream():
    sections, index = logs.recent_logs([(stream(), "ok\n" + "y" * 3000 + "\n")], 100)
    assert all(len(line) <= 1024 for line in sections[0][1])
    assert index[0]["lineTruncated"] is True


def test_render_section_header_and_lines():
    out = logs.render_section(stream("p", "c", previous=True), ["one", "two"])
    assert out == "==> p/c [previous] 2 lines\none\ntwo\n"


# ---------------------------------------------------------- error patterns ----


def rows_for(log, patterns=None, ignore=None):
    rows, ignored = logs.error_patterns(
        [(stream("noisy", "app"), logs.split_lines(log))],
        patterns or SETTINGS["patterns"],
        SETTINGS["ignore"] if ignore is None else ignore,
    )
    return {r["category"]: r for r in rows}, ignored


def test_default_patterns_count_categories_in_the_noisy_log():
    log = text("noisy_app.log")
    by_cat, _ = rows_for(log)
    lines = log.splitlines()
    word = re.compile(
        r"\b(error|err)\b", re.IGNORECASE
    )  # also matches "runtime error" in the Go panic lines
    assert by_cat["error"]["count"] == sum(bool(word.search(line)) for line in lines) > 3
    assert any(s.startswith("ERROR payment failed") for s in by_cat["error"]["samples"])
    assert by_cat["fatal"]["count"] == sum(line.startswith("panic:") for line in lines)
    assert by_cat["connection"]["count"] == sum("connection refused" in line for line in lines)
    assert by_cat["timeout"]["count"] == sum("request timeout" in line for line in lines)
    assert (
        by_cat["exception"]["count"] >= 3
    )  # NullPointerException, IOException, InvalidOperationException
    assert (
        "http5xx" not in by_cat and "oom" not in by_cat
    )  # categories with no match produce no row


def test_samples_are_the_first_three_lines_and_bounded():
    by_cat, _ = rows_for("".join(f"ERROR number {i}\n" for i in range(10)))
    assert by_cat["error"]["count"] == 10
    assert by_cat["error"]["samples"] == ["ERROR number 0", "ERROR number 1", "ERROR number 2"]
    assert by_cat["error"]["truncated"] is False
    by_cat, _ = rows_for("ERROR " + "x" * 5000 + "\n")
    assert len(by_cat["error"]["samples"][0]) == 1024 and by_cat["error"]["truncated"] is True


def test_one_line_counts_in_every_category_it_matches():
    by_cat, _ = rows_for("FATAL: connection refused while out of memory\n")
    assert set(by_cat) == {"fatal", "connection", "oom"}


def test_http_5xx_needs_a_status_context():
    by_cat, _ = rows_for(
        'GET /x HTTP/1.1" 503 12\nstatus=502 upstream\nGET /y 200 503ms\nid 500123\n'
    )
    assert by_cat["http5xx"]["count"] == 2


def test_lines_saying_zero_errors_are_ignored_and_counted():
    log = 'error: 0\n"error":0\nfailures=false\nERROR real problem\n'
    by_cat, ignored = rows_for(log)
    assert by_cat["error"]["count"] == 1 and ignored == 3
    by_cat, ignored = rows_for(log, ignore=[])
    assert by_cat["error"]["count"] == 3 and ignored == 0


def test_custom_patterns_and_invalid_regex():
    by_cat, _ = rows_for("login denied for bob\n", patterns={"auth": "denied|unauthorized"})
    assert list(by_cat) == ["auth"]
    with pytest.raises(RwError, match="'bad'") as exc:
        logs.compile_patterns({"bad": "("})
    assert exc.value.code == "invalid_input"


def test_rows_are_ordered_by_count():
    rows, _ = logs.error_patterns(
        [(stream(), ["ERROR a", "ERROR b", "timeout"])], SETTINGS["patterns"], []
    )
    assert [r["category"] for r in rows] == ["error", "timeout"]


# ------------------------------------------------------------- stacktraces ----


def traces_for(log, previous=False):
    return logs.stacktraces([(stream("noisy", "app", previous=previous), logs.split_lines(log))])


def test_noisy_log_has_one_group_per_language():
    out = traces_for(text("noisy_app.log"))
    by_lang = {t["language"]: t for t in out}
    assert set(by_lang) == {"python", "java", "go", "node", "dotnet"}
    assert by_lang["python"]["exception"] == "ZeroDivisionError"
    assert "java.lang.NullPointerException" in {
        t["exception"] for t in out if t["language"] == "java"
    }
    assert by_lang["go"]["exception"] == "panic"
    assert by_lang["node"]["exception"] == "TypeError"
    assert by_lang["dotnet"]["exception"] == "System.InvalidOperationException"
    # --tail cut the log mid-iteration: the first Java trace lost its header (a "Caused by" group is left)
    # and the first Python traceback is gone entirely; every other trace is counted once
    lines = text("noisy_app.log").splitlines()
    counts = {(t["language"], t["exception"]): t["count"] for t in out}
    assert counts["python", "ZeroDivisionError"] == sum(
        line.startswith("ZeroDivisionError") for line in lines
    )
    assert counts["go", "panic"] == sum(line.startswith("panic:") for line in lines)
    assert counts["node", "TypeError"] == sum(line.startswith("TypeError") for line in lines)
    assert counts["dotnet", "System.InvalidOperationException"] == sum(
        line.startswith("Unhandled exception.") for line in lines
    )
    assert counts["java", "java.lang.NullPointerException"] == sum(
        line.startswith("Exception in thread") for line in lines
    )


def test_python_sample_is_the_whole_traceback():
    py = next(t for t in traces_for(text("noisy_app.log")) if t["language"] == "python")
    assert py["firstLine"] == "ZeroDivisionError: division by zero"
    assert py["sample"][0] == "Traceback (most recent call last):"
    assert py["sample"][-1] == "ZeroDivisionError: division by zero"
    assert len(py["sample"]) == 6 and not py["sampleTruncated"]


def test_java_sample_keeps_frames_and_caused_by():
    java = next(
        t
        for t in traces_for(text("noisy_app.log"))
        if t["exception"] == "java.lang.NullPointerException"
    )
    sample = "\n".join(java["sample"])
    assert "at com.example.App.process(App.java:42)" in sample
    assert "Caused by: java.io.IOException: boom" in sample and "... 2 more" in sample


def test_go_sample_includes_the_goroutine_dump_and_stops_at_the_next_log_line():
    go = next(t for t in traces_for(text("noisy_app.log")) if t["language"] == "go")
    assert go["firstLine"].startswith("panic: runtime error: index out of range")
    assert any(line.startswith("goroutine 1 [running]:") for line in go["sample"])
    assert go["sample"][-1] == "exit status 2"


def test_go_fatal_error_and_panic_without_a_dump():
    fatal = "fatal error: concurrent map writes\n\ngoroutine 7 [running]:\nmain.f(...)\n\t/a.go:3\n"
    assert [t["exception"] for t in traces_for(fatal)] == ["fatal error"]
    assert traces_for("panic: just a message line\nnext normal line\n") == []


def test_ordinary_error_lines_are_not_traces():
    assert traces_for(text("web.log")) == []
    assert (
        traces_for(
            "ERROR payment failed: connection refused\nZeroDivisionError: division by zero\n"
        )
        == []
    )


def test_sample_is_cut_to_forty_lines():
    frames = [f'  File "/a.py", line {i}, in f{i}\n    x()' for i in range(60)]
    log = "Traceback (most recent call last):\n" + "\n".join(frames) + "\nValueError: deep\n"
    (t,) = traces_for(log)
    assert len(t["sample"]) == logs.TRACE_SAMPLE_LINES and t["sampleTruncated"]
    assert t["exception"] == "ValueError"


def test_traces_group_by_type_and_first_line_ignoring_numbers_and_uuids():
    one = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: order 17 for 6f1c3a52-0000-4000-8000-1234567890ab\n'
    two = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: order 99 for aaaaaaaa-0000-4000-8000-1234567890ab\n'
    other = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: other\n'
    out = logs.stacktraces(
        [
            (stream("p1", "c"), logs.split_lines(one + two)),
            (stream("p2", "c", previous=True), logs.split_lines(other + one)),
        ]
    )
    assert [(t["exception"], t["count"]) for t in out] == [("KeyError", 3), ("KeyError", 1)]
    assert out[0]["pods"] == ["p1", "p2"] and out[0]["inPrevious"] is True


def test_traceback_cut_off_by_the_log_end_is_still_reported():
    (t,) = traces_for('Traceback (most recent call last):\n  File "a.py", line 1, in f\n')
    assert t["language"] == "python" and t["exception"] == "(unknown)"


def test_chained_python_exceptions_are_separate_traces():
    log = (
        'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: k\n\n'
        "During handling of the above exception, another exception occurred:\n\n"
        'Traceback (most recent call last):\n  File "a.py", line 5, in g\nRuntimeError: wrapped\n'
    )
    assert [t["exception"] for t in traces_for(log)] == ["KeyError", "RuntimeError"]


def test_dotnet_without_the_unhandled_prefix_and_java_behind_a_log_prefix():
    dotnet = "System.NullReferenceException: Object reference not set\n   at A.B.C() in /src/A.cs:line 4\n"
    assert [(t["language"], t["exception"]) for t in traces_for(dotnet)] == [
        ("dotnet", "System.NullReferenceException")
    ]
    java = "2026-11-03 ERROR app - java.sql.SQLException: closed\n\tat a.b.C.d(C.java:1)\n"
    assert [(t["language"], t["exception"]) for t in traces_for(java)] == [
        ("java", "java.sql.SQLException")
    ]


def test_trace_sample_lines_are_clipped():
    log = (
        'Traceback (most recent call last):\n  File "'
        + "z" * 3000
        + '", line 1, in f\nKeyError: k\n'
    )
    (t,) = traces_for(log)
    assert all(len(line) <= 1024 for line in t["sample"]) and t["sampleTruncated"] is True


def test_nested_trace_lists_are_capped():
    one = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: k\n'
    out = logs.stacktraces([(stream(f"p{i}", f"c{i}"), logs.split_lines(one)) for i in range(80)])
    assert (
        out[0]["count"] == 80
        and len(out[0]["pods"]) == logs.MAX_TRACE_PODS
        and len(out[0]["containers"]) == 50
    )


# ------------------------------------------------------------ errors / ctx ----


@pytest.mark.parametrize(
    "message, code",
    [
        ('pods "x" is forbidden: User cannot list resource "pods"', "permission_denied"),
        (
            'Error from server (Forbidden): jobs is forbidden: User "x" cannot watch resource "jobs"',
            "permission_denied",
        ),
        ('Error from server (NotFound): deployments.apps "x" not found', "not_found"),
        ("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", "unreachable"),
        ("dial tcp: lookup nope: no such host", "unreachable"),
        ("x509: certificate signed by unknown authority", "unreachable"),
        ("context deadline exceeded", "timeout"),
        ("Too Many Requests (429)", "rate_limited"),
        ('the server doesn\'t have a resource type "foo"', "dependency"),
        ("something else", "internal"),
    ],
)
def test_classify_error(message, code):
    assert classify_error(message) == code


def test_classify_error_by_exit_code():
    assert (
        classify_error("", 124) == "timeout"
        and classify_error("kubectl: not installed", 127) == "dependency"
    )


class Failed:
    """A kubectl failure: stderr text and exit code."""

    def __init__(self, stderr, returncode=1):
        self.stderr, self.returncode = stderr, returncode


class FakeCtx:
    """A ctx whose kubectl answers from a table of (argv markers, answer). Like the SDK's
    ctx.run it returns the process, failed or not, and never raises on a non-zero exit."""

    def __init__(
        self, answers, resource_type="kubernetes/deployment", name="noisy", namespace="rw-broken"
    ):
        self.answers = list(answers)
        self.calls = []
        self.errors = []
        self.resource = types.SimpleNamespace(
            type=resource_type, name=name, namespace=namespace, cluster="c", path=None
        )
        s = {
            "max_items": 200,
            "max_pods": 20,
            "max_lines": 2000,
            "max_scan_lines": 5000,
            "exclude_containers": SETTINGS["excludeContainers"],
            "patterns": SETTINGS["patterns"],
            "ignore": SETTINGS["ignore"],
        }
        self.settings = types.SimpleNamespace(**s)
        self.artifacts = FakeArtifacts()

    def run(self, argv):
        self.calls.append(argv)
        for needle, answer in self.answers:
            if all(n in argv for n in needle):
                if isinstance(answer, Failed):
                    return types.SimpleNamespace(
                        returncode=answer.returncode, stdout="", stderr=answer.stderr
                    )
                out = answer if isinstance(answer, str) else json.dumps(answer)
                return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        raise AssertionError(f"no answer for {argv}")

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append((code, scope, message))


class FakeArtifacts:
    def __init__(self):
        self.written = {}

    def open(self, name, content_type):
        return FakeWriter(self, name)


class FakeWriter:
    def __init__(self, parent, name):
        self.parent, self.name, self.chunks = parent, name, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.parent.written[self.name] = b"".join(self.chunks).decode()
        self.ref = {"$artifact": f"art_{self.name}"}

    def write(self, b):
        self.chunks.append(b)


def noisy_ctx(extra=(), **kw):
    answers = [
        (["get", "deployments.apps", "noisy"], named(load("broken", "deployments"), "noisy")),
        (["get", "replicasets.apps"], {"items": load("broken", "replicasets")}),
        (["get", "pods"], {"items": load("broken", "pods")}),
        *extra,
    ]
    return FakeCtx(answers, **kw)


def crash_ctx(extra):
    return FakeCtx(
        [
            (
                ["get", "deployments.apps", "crashloop"],
                named(load("broken", "deployments"), "crashloop"),
            ),
            (["get", "replicasets.apps"], {"items": load("broken", "replicasets")}),
            (["get", "pods"], {"items": load("broken", "pods")}),
            *extra,
        ],
        name="crashloop",
    )


def script(name):
    spec = importlib.util.spec_from_file_location(f"t_{name}", SKILL / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOGS = [
    (["logs", "noisy-7cc85fc74-bzrdb", "app"], text("noisy_app.log")),
    (["logs", "noisy-7cc85fc74-bzrdb", "quiet"], text("noisy_quiet.log")),
]
ALL_SCRIPTS = ["recent_logs", "error_patterns", "stacktraces"]


def test_error_patterns_script_end_to_end():
    ctx = noisy_ctx(LOGS)
    out = script("error_patterns").main(ctx, since="1h")
    assert {r["container"] for r in out["patterns"]} == {"app"}  # `quiet` printed nothing matching
    assert [s["container"] for s in out["scanned"]] == ["app", "quiet"]
    assert out["inspected"]["pods"] == 1 and out["unavailable"] == []
    logcalls = [c for c in ctx.calls if "logs" in c]
    assert all(
        "--since=3600s" in c and "--tail=5000" in c for c in logcalls
    )  # a duration kubectl accepts
    assert ctx.errors == []


def test_stacktraces_script_end_to_end():
    out = script("stacktraces").main(noisy_ctx(LOGS), since="2h")
    assert {t["language"] for t in out["traces"]} == {"python", "java", "go", "node", "dotnet"}


def test_recent_logs_script_writes_the_artifact_and_an_index():
    ctx = noisy_ctx(LOGS)
    out = script("recent_logs").main(ctx, tail_lines=50)
    body = ctx.artifacts.written["logs"]
    assert out["logs"] == {"$artifact": "art_logs"}
    assert "==> noisy-7cc85fc74-bzrdb/app [current] 120 lines\n" in body
    assert "==> noisy-7cc85fc74-bzrdb/quiet [current] 20 lines\n" in body
    assert "--tail=50" in next(c for c in ctx.calls if "logs" in c and "app" in c)
    assert [(s["container"], s["lines"], s["truncated"]) for s in out["streams"]] == [
        ("app", 120, False),
        ("quiet", 20, False),
    ]


def test_recent_logs_respects_max_lines():
    ctx = noisy_ctx(LOGS)
    ctx.settings.max_lines = 50
    out = script("recent_logs").main(ctx)
    assert sum(s["lines"] for s in out["streams"]) == 50
    assert ctx.artifacts.written["logs"].count("\n") == 50 + 2  # lines plus two headers


def test_recent_logs_container_input_reads_only_that_container():
    out = script("recent_logs").main(noisy_ctx(LOGS), container="quiet")
    assert [s["container"] for s in out["streams"]] == ["quiet"]


def test_crashloop_previous_log_is_read_and_current_is_reported_unavailable():
    ctx = crash_ctx([(["logs", "--previous"], text("crashloop_previous.log"))])
    out = script("error_patterns").main(ctx, since="1h")
    assert [(s["container"], s["previous"]) for s in out["scanned"]] == [("app", True)]
    assert [r["category"] for r in out["patterns"]] == ["fatal"] and out["patterns"][0][
        "previous"
    ] is True
    assert out["unavailable"] == [
        {
            "pod": "crashloop-77d67d4789-f6rbl",
            "container": "app",
            "init": False,
            "previous": False,
            "reason": "container is waiting: CrashLoopBackOff",
        }
    ]


def test_previous_log_that_is_gone_is_unavailable_not_an_error():
    gone = Failed(
        'Error from server (BadRequest): previous terminated container "app" in pod "x" not found'
    )
    ctx = crash_ctx([(["logs", "--previous"], gone)])
    out = script("stacktraces").main(ctx)
    assert out["traces"] == [] and ctx.errors == []
    assert {u["reason"] for u in out["unavailable"]} == {
        logs.PREVIOUS_GONE,
        "container is waiting: CrashLoopBackOff",
    }


def test_log_file_missing_message_is_not_log_content():
    # kubectl exits 0 and prints this one line when the node removed the dead container's log
    msg = "unable to retrieve container logs for containerd://fcb53adc0cff4999708b58585a0440f4c678d21e179512e8281ead6c681324cd"
    ctx = crash_ctx([(["logs", "--previous"], msg + "\n")])
    out = script("error_patterns").main(ctx)
    assert out["patterns"] == [] and out["scanned"] == []
    assert {u["reason"] for u in out["unavailable"]} == {
        logs.PREVIOUS_GONE,
        "container is waiting: CrashLoopBackOff",
    }
    assert not fetch.log_file_missing("unable to retrieve container logs for x\nreal line")


# classic false pass: a failed `kubectl logs` was dropped (returncode != 0 -> nothing appended) and the
# task reported "no errors" / "no stacktraces". Here a failing log call is an error or an exception.


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_every_log_call_failing_raises_instead_of_returning_empty(name):
    forbidden = Failed(
        'pods "noisy-x" is forbidden: User "rw-viewer" cannot get resource "pods/log"'
    )
    ctx = noisy_ctx([(["logs"], forbidden)])
    with pytest.raises(RwError, match="forbidden") as exc:
        script(name).main(ctx)
    assert exc.value.code == "permission_denied" and ctx.errors == []


def test_one_container_failing_is_an_error_on_the_result_and_the_rest_continue():
    boom = Failed("Unable to connect to the server: dial tcp: i/o timeout")
    ctx = noisy_ctx([(["logs", "quiet"], boom), *LOGS])
    out = script("error_patterns").main(ctx)
    assert [r["container"] for r in out["patterns"]] == ["app"] * len(out["patterns"]) and out[
        "patterns"
    ]
    assert ctx.errors[0][0] == "unreachable" and ctx.errors[0][1] == "noisy-7cc85fc74-bzrdb/quiet"
    assert [s["container"] for s in out["scanned"]] == ["app"]


# classic false pass: a Deployment with no ReplicaSets/pods passed the log checks; here the empty
# result carries inspected.pods == 0 so it cannot be read as clean logs.


def test_workload_without_pods_reports_zero_pods_inspected():
    ctx = FakeCtx(
        [
            (["get", "deployments.apps", "noisy"], named(load("broken", "deployments"), "noisy")),
            (["get", "replicasets.apps"], {"items": []}),
            (["get", "pods"], {"items": []}),
        ]
    )
    out = script("stacktraces").main(ctx)
    assert out["traces"] == [] and out["scanned"] == [] and out["inspected"]["pods"] == 0


def test_unsupported_target_type_raises_value_error():
    for rtype in ("kubernetes/pod", "kubernetes/job"):
        with pytest.raises(ValueError, match="does not apply"):
            script("recent_logs").main(FakeCtx([], resource_type=rtype))


def test_healthy_deployment_has_clean_logs():
    ctx = FakeCtx(
        [
            (["get", "deployments.apps", "web"], named(load("healthy", "deployments"), "web")),
            (["get", "replicasets.apps"], {"items": load("healthy", "replicasets")}),
            (["get", "pods"], {"items": load("healthy", "pods")}),
            (["logs"], text("web.log")),
        ],
        name="web",
        namespace="rw-healthy",
    )
    assert script("error_patterns").main(ctx)["patterns"] == []
    assert script("stacktraces").main(ctx)["traces"] == []
    assert ctx.errors == []


# ------------------------------------- failures of the primary fetch (3b) ----


@pytest.mark.parametrize("name", ALL_SCRIPTS)
@pytest.mark.parametrize(
    "stderr, code",
    [
        (
            'Error from server (Forbidden): deployments.apps "noisy" is forbidden: User "rw-viewer" cannot get resource "deployments" in namespace "rw-broken"',
            "permission_denied",
        ),
        ('Error from server (NotFound): deployments.apps "noisy" not found', "not_found"),
        (
            "Unable to connect to the server: dial tcp 10.0.0.1:6443: connect: connection refused",
            "unreachable",
        ),
    ],
)
def test_target_fetch_failure_raises_the_classified_error(name, stderr, code):
    ctx = FakeCtx([(["get", "deployments.apps", "noisy"], Failed(stderr))])
    with pytest.raises(RwError) as exc:
        script(name).main(ctx)
    assert exc.value.code == code
    assert not any("logs" in c for c in ctx.calls)


def test_pod_list_failure_raises_too():
    ctx = noisy_ctx()
    ctx.answers.insert(
        0, (["get", "pods"], Failed('pods is forbidden: User "x" cannot list resource "pods"'))
    )
    with pytest.raises(RwError) as exc:
        script("stacktraces").main(ctx)
    assert exc.value.code == "permission_denied"


def test_kubectl_timeout_is_a_timeout_error():
    class Slow(FakeCtx):
        def run(self, argv):
            raise subprocess.TimeoutExpired(argv, 60)

    with pytest.raises(RwError) as exc:
        script("recent_logs").main(Slow([]))
    assert exc.value.code == "timeout"


# ---------------------------------------------------------- invalid input ----


@pytest.mark.parametrize("since", ["soon", "10", "", "5x", "1h junk"])
@pytest.mark.parametrize("name", ["error_patterns", "stacktraces"])
def test_bad_since_is_invalid_input_before_any_kubectl_call(name, since):
    ctx = noisy_ctx(LOGS)
    with pytest.raises(RwError) as exc:
        script(name).main(ctx, since=since)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_since_accepts_milliseconds_and_rounds_up():
    assert workload.since_seconds("1500ms") == 2 and workload.since_seconds("100ms") == 1
    assert workload.since_seconds("1h30m") == 5400 and workload.since_seconds("2d") == 172800
    ctx = noisy_ctx(LOGS)
    script("error_patterns").main(ctx, since="90s")
    assert any("--since=90s" in c for c in ctx.calls)


@pytest.mark.parametrize("bad", [0, -1, 5001, "200", None])
def test_bad_tail_lines_is_invalid_input(bad):
    ctx = noisy_ctx(LOGS)
    with pytest.raises(RwError) as exc:
        script("recent_logs").main(ctx, tail_lines=bad)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_bad_pattern_setting_is_invalid_input_before_any_kubectl_call():
    ctx = noisy_ctx(LOGS)
    ctx.settings.patterns = {"broken": "(unclosed"}
    with pytest.raises(RwError) as exc:
        script("error_patterns").main(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_target_kind_falls_back_to_the_resource_path():
    ctx = noisy_ctx(LOGS)
    ctx.resource.type = None
    ctx.resource.path = "kubernetes/clusters/c/namespaces/rw-broken/deployments/noisy"
    assert script("stacktraces").main(ctx)["traces"]
    ctx.resource.path = "kubernetes/clusters/c/namespaces/rw-broken/pods/noisy"
    with pytest.raises(ValueError, match="does not apply"):
        script("stacktraces").main(ctx)


# ------------------------------------------------------------------ bounds ----


def test_top_level_lists_are_capped_by_max_items():
    ctx = noisy_ctx([(["logs"], "ERROR e1\ntimeout here\n")])
    ctx.settings.max_items = 1
    out = script("error_patterns").main(ctx)
    assert len(out["patterns"]) == len(out["scanned"]) == 1


def test_outputs_validate_against_their_schemas():
    for name, task in zip(["recent_logs", "error_patterns", "stacktraces"], TASKS["tasks"]):
        ctx = noisy_ctx(LOGS)
        out = script(name).main(ctx)
        assert set(out) == set(task["outputs"])
        for key, decl in task["outputs"].items():
            if "schema" in decl:
                schema = json.loads((SKILL / decl["schema"]).read_text())
                jsonschema.validate(out[key], schema, cls=jsonschema.Draft202012Validator)


def test_defaults_in_tasks_yaml_are_usable():
    assert set(logs.compile_patterns(SETTINGS["patterns"])) == {
        "error",
        "exception",
        "fatal",
        "oom",
        "connection",
        "timeout",
        "http5xx",
    }
    assert logs.compile_patterns({"x": SETTINGS["ignore"][0]})
