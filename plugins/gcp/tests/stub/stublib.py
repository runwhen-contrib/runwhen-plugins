"""What the stub programs (bin/gcloud, bin/curl) share: find the scenario, look the argv up in its
calls.yaml, print the recorded answer. Python 3 standard library only; loaded by path from the
programs, which run with `python3 -I` (no script directory on sys.path).

A call in calls.yaml is `tool` (gcloud unless it says curl), `argv` (without the program name),
`rc`, `stdout` (a file in the scenario directory), `stderr`, `source` (the REST reference its shape
follows) and `cli_source` (the file of the pinned SDK that formats it, or the curl behaviour).
"""

import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SCENARIOS = os.path.join(HERE, "scenarios")
IDENTITIES = os.path.join(HERE, "identities.json")
HARNESS_ENV = "GCP_STUB_HARNESS"  # harness.py sets it to "1"
SCENARIO_ENV = "GCP_STUB_SCENARIO"
# gcloud flags that change the output encoding or the prompting but not the question asked
NOISE = {"--format=json", "--quiet", "-q"}
REQUIRED = ("source", "argv")
# the Monitoring query's time window moves with the run's reference time; the recorded argv keeps
# the real values, the canned one has a placeholder
TIME = re.compile(r"(interval\.(?:start|end)Time=)[^&]*")
# so does the window of a log filter (`timestamp>="2026-11-04T00:00:00Z"`)
LOG_TIME = re.compile(r'(timestamp[<>]=")[^"]*(")')


def fail(program, message, rc=2):
    sys.stderr.write(f"{program} (stub): {message}\n")
    sys.exit(rc)


def normalise(tool, argv):
    """argv without what does not change the question. gcloud: `--format=json`, `--format json`,
    `--quiet`, `-q` (nothing is sorted: flag order is part of the question), the time window of a
    log filter. curl: the query's time window."""
    if tool == "curl":
        return [TIME.sub(r"\1<T>", a) for a in argv]
    out, skip = [], False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
        elif arg == "--format" and argv[index + 1 : index + 2] == ["json"]:
            skip = True
        elif arg not in NOISE:
            out.append(LOG_TIME.sub(r"\1<T>\2", arg))
    return out


def parse_calls(text):
    """calls.yaml: a list of `- key: value` entries whose values are JSON (strings in double
    quotes, argv a JSON array on one line); `#` lines and blank lines are skipped. This is valid
    YAML too (tests/unit/test_stub_harness.py loads it with a real YAML parser to prove it)."""
    calls, current = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        body = line[2:] if line.startswith("- ") else line[2:] if line.startswith("  ") else None
        if body is None:
            fail("calls.yaml", f"line {number}: expected '- key: value' or '  key: value'")
        if line.startswith("- "):
            current = {}
            calls.append(current)
        if current is None:
            fail("calls.yaml", f"line {number}: a key before the first '- '")
        key, sep, value = body.partition(": ")
        if not sep:
            fail("calls.yaml", f"line {number}: expected 'key: <json>'")
        try:
            current[key.strip()] = json.loads(value)
        except ValueError:
            fail("calls.yaml", f"line {number}: the value of {key.strip()} is not JSON")
    return calls


def fake_token():
    with open(IDENTITIES, encoding="utf-8") as handle:
        return json.load(handle)["fakeToken"]


def check_curl_header(argv):
    """The call must pass the credential as `-H @<file>`: a file, mode 0600, that holds the fake
    token (and so never a token on the command line)."""
    refs = [a[1:] for i, a in enumerate(argv) if i and argv[i - 1] == "-H" and a.startswith("@")]
    if len(refs) != 1:
        fail(
            "curl", "the credential must be passed as one `-H @<file>` (never on the command line)"
        )
    try:
        info = os.stat(refs[0])
        with open(refs[0], encoding="ascii") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        fail("curl", f"the header file {refs[0]!r} cannot be read")
    if stat.S_IMODE(info.st_mode) != 0o600:
        fail("curl", f"the header file {refs[0]!r} is mode {stat.S_IMODE(info.st_mode):o}, not 600")
    if f"Authorization: Bearer {fake_token()}\n" not in text:
        fail("curl", "the header file does not hold the stub's fake token")
    if any(fake_token() in a for a in argv):
        fail("curl", "a token is on the command line")


def serve(tool, argv):
    """Answer `tool argv` from the scenario in GCP_STUB_SCENARIO, then exit."""
    if os.environ.get(HARNESS_ENV) != "1":
        fail(
            tool,
            "refusing to run: not started by plugins/gcp/tests/stub/harness.py, which makes sure "
            "no real gcloud account can be used.",
        )
    scenario = os.environ.get(SCENARIO_ENV, "")
    if not scenario or "/" in scenario or scenario.startswith("."):
        fail(tool, f"{SCENARIO_ENV} is not set to a scenario name")
    directory = os.path.join(SCENARIOS, scenario)
    path = os.path.join(directory, "calls.yaml")
    if not os.path.isfile(path):
        fail(tool, f"no scenario {scenario!r}: there is no {path}")
    if tool == "curl":
        check_curl_header(argv)
    with open(path, encoding="utf-8") as handle:
        calls = parse_calls(handle.read())
    asked = normalise(tool, argv)
    for call in calls:
        missing = [k for k in REQUIRED if k not in call]
        if missing:
            fail(tool, f"scenario {scenario}: a call lacks {', '.join(missing)}")
        if call.get("tool", "gcloud") == tool and call["argv"] == asked:
            stdout_file = call.get("stdout")
            if stdout_file:
                with open(os.path.join(directory, stdout_file), "rb") as handle:
                    sys.stdout.buffer.write(handle.read())
                sys.stdout.buffer.flush()
            sys.stderr.write(call.get("stderr", ""))
            sys.exit(int(call.get("rc", 0)))
    fail(
        tool,
        f"scenario {scenario}: no canned answer for argv {json.dumps(asked)}. "
        f"Add the call to {path} (and a cited file) or fix the task's argv.",
    )
