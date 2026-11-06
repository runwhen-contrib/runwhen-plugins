"""Unit tests for k8s-workload-logs against JSON and log text captured from the
kind-rw-plugins scenarios (plugins/kubernetes/tests/kind, 20-broken.yaml and
30-k8s-workload-logs.yaml). The `noisy` Deployment prints one trace per language
in a loop; `crashloop` is waiting in CrashLoopBackOff with a previous run."""

import importlib.util
import json
from collections.abc import Mapping
import re
import subprocess
import types
from pathlib import Path

import jsonschema
import pytest
import yaml
from k8s_workload_logs import fetch, logs, traces, workload
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


def reduce(*pairs):
    return [logs.reduce_stream(s, t, len(t)) for s, t in pairs]


def test_recent_logs_keeps_the_newest_lines_and_flags_truncation():
    fetched = reduce(
        (stream("a"), "\n".join(f"a{i}" for i in range(10)) + "\n"),
        (stream("b"), "b0\nb1\n"),
    )
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
    sections, index = logs.recent_logs(reduce((stream(), "ok\n" + "y" * 3000 + "\n")), 100)
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
    assert set(by_cat) == {"fatal", "connection", "oom", "failed"}


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
    return traces.stacktraces([(stream("noisy", "app", previous=previous), logs.split_lines(log))])


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
    assert len(t["sample"]) == traces.TRACE_SAMPLE_LINES and t["sampleTruncated"]
    assert t["exception"] == "ValueError"


def test_traces_group_by_type_and_first_line_ignoring_numbers_and_uuids():
    one = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: order 17 for 6f1c3a52-0000-4000-8000-1234567890ab\n'
    two = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: order 99 for aaaaaaaa-0000-4000-8000-1234567890ab\n'
    other = 'Traceback (most recent call last):\n  File "a.py", line 1, in f\nKeyError: other\n'
    out = traces.stacktraces(
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
    out = traces.stacktraces([(stream(f"p{i}", f"c{i}"), logs.split_lines(one)) for i in range(80)])
    assert (
        out[0]["count"] == 80
        and len(out[0]["pods"]) == traces.MAX_TRACE_PODS
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
        self.timeouts = []
        self.errors = []
        self.resource = types.SimpleNamespace(
            type=resource_type, name=name, namespace=namespace, cluster="c", path=None
        )
        s = {
            "max_items": 200,
            "max_pods": 20,
            "max_lines": 2000,
            "max_scan_lines": 5000,
            "max_bytes_total": 64 << 20,
            "scan_budget_seconds": 60,
            "exclude_containers": SETTINGS["excludeContainers"],
            "patterns": SETTINGS["patterns"],
            "ignore": SETTINGS["ignore"],
        }
        self.settings = types.SimpleNamespace(**s)

    def run(self, argv, timeout=None):
        self.calls.append(argv)
        self.timeouts.append(timeout)
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


@pytest.fixture(autouse=True)
def rw_out(tmp_path, monkeypatch):
    """The runner gives a task its artifact outputs as files under $RW_OUT."""
    out = tmp_path / "rw-out"
    out.mkdir()
    monkeypatch.setenv("RW_OUT", str(out))
    return out


def artifact(rw_out, name="logs"):
    return (rw_out / name).read_text()


class AttrViewLike(Mapping):
    """A read-only Mapping that is not a dict, like the SDK's AttrView."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


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


def test_recent_logs_script_writes_the_artifact_file_and_an_index(rw_out):
    ctx = noisy_ctx(LOGS)
    out = script("recent_logs").main(ctx, tail_lines=50)
    body = artifact(rw_out)
    assert "logs" not in out  # an artifact output is the file, the script returns no value for it
    assert list(out) == ["streams", "unavailable", "inspected"]
    assert body.startswith("==> noisy-7cc85fc74-bzrdb/app [current] 120 lines\n")
    assert "==> noisy-7cc85fc74-bzrdb/app [current] 120 lines\n" in body
    assert "==> noisy-7cc85fc74-bzrdb/quiet [current] 20 lines\n" in body
    assert "--tail=50" in next(c for c in ctx.calls if "logs" in c and "app" in c)
    assert [(s["container"], s["lines"], s["truncated"]) for s in out["streams"]] == [
        ("app", 120, False),
        ("quiet", 20, False),
    ]


def test_recent_logs_respects_max_lines(rw_out):
    ctx = noisy_ctx(LOGS)
    ctx.settings.max_lines = 50
    out = script("recent_logs").main(ctx)
    assert sum(s["lines"] for s in out["streams"]) == 50
    assert artifact(rw_out).count("\n") == 50 + 2  # lines plus two headers


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
        def run(self, argv, timeout=None):
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
        assert set(out) == {k for k, d in task["outputs"].items() if "artifact" not in d}
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
        "auth",
        "resource",
        "failed",
    }
    assert logs.compile_patterns({"x": SETTINGS["ignore"][0]})


# ============================================================ review fixes ====
# H1: RBAC. `kubectl logs` GETs the pod first, so pods need `get` as well as `list`.


def rbac_needs(calls):
    """(apiGroup, resource, verb) triples the kubectl argv issued by a script require."""
    needs = set()
    for argv in calls:
        a = argv[1:]
        if "logs" in a:
            needs |= {
                ("", "pods", "get"),
                ("", "pods/log", "get"),
            }  # kubectl logs GETs the pod, then its log
        elif "get" in a:
            rest = a[a.index("get") + 1 :]
            resource, _, group = rest[0].partition(".")
            named = len(rest) > 1 and not rest[1].startswith("-")
            needs.add((group, resource, "get" if named and "-l" not in rest else "list"))
        else:
            raise AssertionError(f"unmapped kubectl call {argv}")
    return needs


def granted(task, triple):
    group, resource, verb = triple
    return any(
        group in p["apiGroups"] and resource in p["resources"] and verb in p["verbs"]
        for p in task["permissions"]
    )


@pytest.mark.parametrize(
    "name, task_name",
    [
        ("recent_logs", "recent-logs"),
        ("error_patterns", "error-patterns"),
        ("stacktraces", "stacktraces"),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(name, task_name):
    task = next(t for t in TASKS["tasks"] if t["name"] == task_name)
    ctx = noisy_ctx(LOGS)
    script(name).main(ctx)
    needs = rbac_needs(ctx.calls)
    assert {
        ("apps", "deployments", "get"),
        ("apps", "replicasets", "list"),
        ("", "pods", "list"),
    } <= needs
    assert ("", "pods", "get") in needs and ("", "pods/log", "get") in needs
    assert [n for n in needs if not granted(task, n)] == []


def test_previous_log_calls_need_the_same_rules():
    ctx = crash_ctx([(["logs", "--previous"], text("crashloop_previous.log"))])
    script("stacktraces").main(ctx)
    task = next(t for t in TASKS["tasks"] if t["name"] == "stacktraces")
    assert [n for n in rbac_needs(ctx.calls) if not granted(task, n)] == []


# H2: the default `ignore` setting must not hide real errors.

REAL_ERRORS = [
    'ERROR: null value in column "id" violates not-null constraint',
    "error: nonexistent file /x",
    "failure: none of the upstreams are healthy",
    "error: false positive in validator",
    "Error: 0x80070005",
]


@pytest.mark.parametrize("line", REAL_ERRORS)
def test_default_ignore_does_not_hide_real_error_lines(line):
    by_cat, ignored = rows_for(line + "\n")
    assert ignored == 0 and by_cat, line


@pytest.mark.parametrize(
    "line",
    [
        "error: 0",
        '"error":0}',
        '{"error":null,"msg":"x"}',
        "failures=false",
        "errors: []",
        "error=0 retries=3",
        'errors: ""',
    ],
)
def test_default_ignore_still_hides_zero_error_lines(line):
    by_cat, ignored = rows_for(line + "\n")
    assert ignored == 1 and "error" not in by_cat, line


# M1: a `container` input that matches nothing says what the pods have.


def test_container_input_matching_nothing_is_invalid_input():
    ctx = noisy_ctx(LOGS)
    with pytest.raises(RwError) as exc:
        script("recent_logs").main(ctx, container="nginx")
    assert exc.value.code == "invalid_input"
    assert "no container named 'nginx'" in exc.value.message and "app, quiet" in exc.value.message
    assert not any("logs" in c for c in ctx.calls)


# M2: default categories.


@pytest.mark.parametrize(
    "line, category",
    [
        ("3 errors while parsing", "error"),
        ("java.lang.OutOfMemoryError: Java heap space", "oom"),
        ("statusCode: 503", "http5xx"),
        ("status_code=500", "http5xx"),
        ("upstream returned HTTP 502", "http5xx"),
        ('10.0.0.1 - - "GET /x HTTP/1.1" 504 12', "http5xx"),
        ('10.0.0.1 "GET /x" 500 12', "http5xx"),
        ("401 Unauthorized", "auth"),
        ("token expired for user bob", "auth"),
        ("tls: x509: certificate has expired", "auth"),
        ("rpc error: resource exhausted", "resource"),
        ("write failed: no space left on device", "resource"),
        ("too many open files", "resource"),
        ("failed to connect to db", "failed"),
        ("worker crashed", "failed"),
        ("open /x: permission denied", "failed"),
    ],
)
def test_default_categories_match_real_lines(line, category):
    by_cat, _ = rows_for(line + "\n")
    assert category in by_cat, line


def test_default_categories_stay_quiet_on_ordinary_lines():
    ordinary = "GET /orders 200 41ms\nhealth ok\nlatency 503ms p99\nid 500123\nport 4030\n"
    assert rows_for(ordinary)[0] == {}


# M3: bounded regular expressions and a wall-clock budget.


def test_pattern_count_and_length_are_capped():
    with pytest.raises(RwError) as exc:
        logs.compile_patterns({f"p{i}": "x" for i in range(21)})
    assert exc.value.code == "invalid_input"
    with pytest.raises(RwError) as exc:
        logs.compile_patterns({"long": "x" * 201})
    assert exc.value.code == "invalid_input" and "'long'" in exc.value.message
    assert logs.compile_patterns({f"p{i}": "x" * 200 for i in range(20)})
    with pytest.raises(RwError):
        logs.compile_ignore(["x"] * 21)


def test_patterns_only_see_the_first_8192_characters_of_a_line():
    assert logs.split_lines("a" * 9000 + "ERROR\n") == ["a" * 8192]
    assert rows_for("a" * 9000 + " ERROR\n")[0] == {}
    assert "error" in rows_for("a" * 100 + " ERROR\n")[0]


class Ticker:
    """A clock that advances one second per reading."""

    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return self.t


def test_scan_stops_when_the_budget_runs_out_and_counts_are_partial():
    scanner = logs.PatternScanner(SETTINGS["patterns"], [])
    budget = logs.Budget(3, clock=Ticker())  # the deadline reading, then two line-batch checks
    rows, complete = scanner.scan(stream(), ["ERROR boom"] * 2000, budget)
    assert complete is False
    assert 0 < rows[0]["count"] < 2000 and rows[0]["count"] % logs.CHECK_EVERY == 0


def test_trace_search_stops_when_the_budget_runs_out():
    found, complete = traces.find_traces(["plain line"] * 2000, logs.Budget(2, clock=Ticker()))
    assert complete is False and found == []


def test_script_reports_a_scan_timeout_and_the_streams_it_did_not_read(monkeypatch):
    ticker = Ticker()
    monkeypatch.setattr(logs, "time", types.SimpleNamespace(monotonic=ticker))
    ctx = noisy_ctx(LOGS)
    ctx.settings.scan_budget_seconds = 2
    out = script("error_patterns").main(ctx)
    assert any(code == "timeout" and scope == "scan" for code, scope, _m in ctx.errors)
    assert [u["reason"] for u in out["unavailable"]] == [logs.SKIPPED_BUDGET]
    assert [s["container"] for s in out["scanned"]] == ["app"]


def test_dotnet_frame_pattern_is_linear():
    import time

    nasty = "   at " + "a" * 60000 + "(" * 2000
    t0 = time.perf_counter()
    assert traces._DOTNET_FRAME.match(nasty) is None
    assert traces._JAVA_OR_DOTNET_FRAME.match(nasty) is None
    assert time.perf_counter() - t0 < 0.5


# M4: the real SDK Context and LocalArtifactSink.


def test_recent_logs_writes_to_rw_out_and_fails_clearly_without_it(tmp_path, monkeypatch):
    out_dir = tmp_path / "elsewhere"
    out_dir.mkdir()
    monkeypatch.setenv("RW_OUT", str(out_dir))
    script("recent_logs").main(noisy_ctx(LOGS))
    assert (out_dir / "logs").read_text().startswith("==> noisy-7cc85fc74-bzrdb/app [current]")
    monkeypatch.delenv("RW_OUT")
    ctx = noisy_ctx(LOGS)
    with pytest.raises(RwError) as exc:
        script("recent_logs").main(ctx)
    assert exc.value.code == "internal" and "RW_OUT" in exc.value.message


@pytest.mark.parametrize(
    "setting, value",
    [
        ("max_pods", 0),
        ("max_lines", 0),
        ("max_scan_lines", 0),
        ("max_items", 0),
        ("scan_budget_seconds", 0),
        ("max_bytes_total", 10),
        ("max_pods", 10**6),
        ("max_pods", "20"),
        ("max_lines", True),
    ],
)
@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_out_of_range_settings_are_invalid_input_before_any_kubectl_call(name, setting, value):
    ctx = noisy_ctx(LOGS)
    setattr(ctx.settings, setting, value)
    with pytest.raises(RwError) as exc:
        script(name).main(ctx)
    assert exc.value.code == "invalid_input" and ctx.calls == []


def test_tasks_yaml_declares_ranges_for_the_numeric_settings():
    for key in (
        "maxItems",
        "maxPods",
        "maxLines",
        "maxScanLines",
        "maxBytesTotal",
        "scanBudgetSeconds",
    ):
        spec = TASKS["settings"][key]
        assert spec["min"] <= spec["default"] <= spec["max"], key


# M5: redaction in everything that leaves the skill.


@pytest.mark.parametrize(
    "line, secret",
    [
        ("Authorization: Bearer eyJhbGciOi.abc.def", "eyJhbGciOi"),
        ("authorization=basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("connecting with password=hunter2 to db", "hunter2"),
        ('{"passwd": "s3cr3t-value", "ok": 1}', "s3cr3t-value"),
        ("client_secret=abcDEF123", "abcDEF123"),
        ("token=ghp_0123456789abcdef", "ghp_0123456789abcdef"),
        ("using key AKIAIOSFODNN7EXAMPLE now", "AKIAIOSFODNN7EXAMPLE"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
        ("-----BEGIN PRIVATE KEY-----", "BEGIN PRIVATE KEY"),
    ],
)
def test_clip_redacts_credentials(line, secret):
    clipped, _ = logs.clip(line)
    assert "[REDACTED]" in clipped and secret not in clipped


def test_clip_leaves_ordinary_text_alone():
    assert logs.clip("token expired for user bob")[0] == "token expired for user bob"
    assert logs.clip("GET /secrets 200")[0] == "GET /secrets 200"


def test_redaction_reaches_every_output(rw_out):
    log = (
        "ERROR login failed password=hunter2\n"
        'Traceback (most recent call last):\n  File "a.py", line 1, in f\n'
        "ValueError: token=abc123\n"
    )
    ctx = noisy_ctx([(["logs"], log)])
    patterns = script("error_patterns").main(ctx)["patterns"]
    sample = script("stacktraces").main(ctx)["traces"][0]["sample"]
    script("recent_logs").main(noisy_ctx([(["logs"], log)]))
    for blob in (json.dumps(patterns), json.dumps(sample), artifact(rw_out)):
        assert "hunter2" not in blob and "abc123" not in blob and "[REDACTED]" in blob


# M6: one stream at a time, a byte budget, and a trace output budget.


def test_reader_stops_at_the_byte_budget_and_lists_the_rest_as_unavailable():
    body = "x" * 600 + "\n"
    ctx = noisy_ctx([(["logs"], body)])
    plan = logs.plan_streams(owned("broken", "Deployment", "noisy"))
    reader = fetch.StreamReader(ctx, plan["streams"], max_bytes=500)
    got = [s["container"] for s, _t, _n in reader]
    assert got == ["app"] and reader.bytes_read == 601
    assert [(u["container"], u["reason"]) for u in reader.unavailable()] == [
        ("quiet", logs.SKIPPED_BYTES)
    ]
    assert len([c for c in ctx.calls if "logs" in c]) == 1


def test_reader_does_not_keep_the_text():
    ctx = noisy_ctx(LOGS)
    plan = logs.plan_streams(owned("broken", "Deployment", "noisy"))
    reader = fetch.StreamReader(ctx, plan["streams"])
    for _s, _text, _n in reader:
        assert not any(isinstance(v, str) and len(v) > 1000 for v in vars(reader).values())
    assert reader.read == 2


def test_trace_samples_are_limited_to_the_fifty_most_frequent_groups():
    blocks = "".join(
        f'Traceback (most recent call last):\n  File "a.py", line 1, in f\n'
        f"Error{chr(65 + i // 26)}{chr(65 + i % 26)}: k\n"
        for i in range(60)
    )
    out = traces_for(blocks)
    assert len(out) == 60
    assert len([t for t in out if t["sample"]]) == traces.MAX_SAMPLES
    assert sum(len(t["sample"]) for t in out) <= 2000
    assert all(t["sampleTruncated"] for t in out if not t["sample"])


def test_trace_groups_are_limited_and_the_rest_counted():
    collector = traces.TraceCollector()
    for i in range(traces.MAX_GROUPS + 5):
        collector.add(
            stream(), ["Traceback (most recent call last):", '  File "a", line 1', f"E{i}x: k"]
        )
    assert len(collector.result()) == traces.MAX_GROUPS and collector.ungrouped == 5


def test_script_outputs_carry_totals_and_omissions():
    ctx = noisy_ctx([(["logs"], "ERROR a\ntimeout b\nfatal c\n")])
    ctx.settings.max_items = 2
    out = script("error_patterns").main(ctx)
    ins = out["inspected"]
    assert len(out["patterns"]) == 2 and ins["total"]["patterns"] > 2
    assert ins["omitted"]["patterns"] == ins["total"]["patterns"] - 2
    assert ins["total"]["scanned"] == 2 and ins["omitted"]["scanned"] == 0 and ins["bytesRead"] > 0


# L-fixes.


def test_split_lines_splits_on_newline_only():
    assert logs.split_lines("a\x0cb\r\nc d\n") == ["a\x0cb", "c d"]
    assert logs.split_lines("") == [] and logs.split_lines("one") == ["one"]


def test_trace_groups_ignore_hex_ids():
    head = 'Traceback (most recent call last):\n  File "a", line 1, in f\n'
    a = head + "KeyError: trace 0xdeadbeef12 req 9f8e7d6c5b4a\n"
    b = head + "KeyError: trace 0xcafebabe99 req 1a2b3c4d5e6f\n"
    assert [t["count"] for t in traces_for(a + b)] == [2]


def test_every_kubectl_logs_call_has_a_timeout_and_a_slow_stream_is_an_error_not_a_stop():
    class SlowQuiet(FakeCtx):
        def run(self, argv, timeout=None):
            if "quiet" in argv and "logs" in argv:
                self.calls.append(argv)
                self.timeouts.append(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)
            return super().run(argv, timeout)

    ctx = SlowQuiet(noisy_ctx(LOGS).answers)
    out = script("error_patterns").main(ctx)
    log_timeouts = [t for c, t in zip(ctx.calls, ctx.timeouts) if "logs" in c]
    assert log_timeouts == [fetch.LOG_CALL_TIMEOUT] * 2 and fetch.LOG_CALL_TIMEOUT == 20
    assert ctx.errors[0][0] == "timeout" and ctx.errors[0][1].endswith("/quiet")
    assert [s["container"] for s in out["scanned"]] == ["app"] and out["patterns"]


# The SDK runner hands settings over as read-only Mappings that are not dicts, with every key
# (at every depth) snake_cased.


def test_error_patterns_accepts_mapping_settings_and_keeps_category_names():
    ctx = noisy_ctx([(["logs"], "ERROR a\nconn refused here\n")])
    ctx.settings.patterns = AttrViewLike({"conn_refused": "conn refused", "error": r"\berror\b"})
    ctx.settings.ignore = ["x"]
    out = script("error_patterns").main(ctx)
    assert {r["category"] for r in out["patterns"]} == {"conn_refused", "error"}


def test_compile_patterns_accepts_any_mapping_and_still_rejects_other_types():
    assert set(logs.compile_patterns(AttrViewLike({"a": "x"}))) == {"a"}
    for bad in (["x"], "x", None, {"a": 1}):
        with pytest.raises(RwError) as exc:
            logs.compile_patterns(bad)
        assert exc.value.code == "invalid_input"


def test_the_patterns_setting_documents_snake_case_category_names():
    assert "snake_case" in TASKS["settings"]["patterns"]["description"]
