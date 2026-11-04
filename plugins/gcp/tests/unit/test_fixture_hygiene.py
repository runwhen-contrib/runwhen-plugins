"""No recorded fixture or canned stub answer may hold a credential, and every fixture is what the
fixture brief asks for.

The fixtures of this plugin are recorded against the gcloud stub (tests/stub), whose answers are
invented, so the real risks are a key or token pasted in by hand, a path or an e-mail of the
machine that recorded, and a fixture re-recorded against a real project: the scan below refuses
anything that is not the stub's synthetic data. A fixture that needs real data goes through review
of the recording (docs/fixture-brief.md), not through an allowlist edit here."""

import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]  # plugins/gcp
SKILLS = PLUGIN / "skills"
STUB = PLUGIN / "tests" / "stub" / "scenarios"

# The stub's invented identities (tests/stub/identities.json: the one list, also read by the stubs).
# A fixture that mentions any other project, number, e-mail domain, bucket or token was not
# recorded from the stub.
IDENTITIES = json.loads((PLUGIN / "tests" / "stub" / "identities.json").read_text())
PROJECTS = set(IDENTITIES["projects"])
PROJECT_NUMBERS = set(IDENTITIES["projectNumbers"])
EMAIL_DOMAINS = set(IDENTITIES["emailDomains"])
BUCKETS = set(IDENTITIES["buckets"])
FAKE_TOKEN = IDENTITIES["fakeToken"]

SECRET_PATTERNS = {
    "PEM private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "private_key field": re.compile(r'"private_key"'),
    "service-account key": re.compile(r'"type"\s*:\s*"service_account"'),
    "OAuth access token": re.compile(r"\bya29\.[A-Za-z0-9._-]{10,}"),
    "bearer token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "refresh token": re.compile(r'(?i)"?refresh_token"?\s*[:=]'),
    "client secret": re.compile(r'(?i)"?client_secret"?\s*[:=]'),
    "API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    "machine path": re.compile(r"/Users/|/home/|\.config/gcloud|application_default_credentials"),
    "identity token": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"),
}
# a project number: the digits after projectNumber, project_number: or projects/ (byte counts are
# also long digit runs and are not project numbers)
NUMBER = re.compile(r"(?i)(?:projectnumber\W+|project_number:|projects/)(\d{6,})")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# any `projects/<id>` (a resource name, a KMS key, a consumer) and `--project=<id>`
PROJECT_TOKEN = re.compile(
    r"(?:(?<![\w/.-])projects/|/v[13]/projects/|--project=)([a-z][a-z0-9-]{4,28}[a-z0-9])(?![a-z0-9-])"
)
# a bucket in a `gs://` URL
GS_URL = re.compile(r"gs://([a-z0-9][a-z0-9._-]{1,220}[a-z0-9])")


def fixtures():
    return sorted(SKILLS.glob("*/tests/*/*.yaml"))


def stub_files():
    return sorted(p for p in STUB.glob("*/*") if p.is_file())


def relative(path):
    return str(path.relative_to(PLUGIN))


def text_problems(text):
    problems = [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
    for domain in sorted({m.group(1) for m in EMAIL.finditer(text)} - EMAIL_DOMAINS):
        problems.append(f"e-mail at {domain}")
    for number in sorted({m.group(1) for m in NUMBER.finditer(text)} - PROJECT_NUMBERS):
        problems.append(f"project number {number}")
    for project in sorted({m.group(1) for m in PROJECT_TOKEN.finditer(text)} - PROJECTS):
        problems.append(f"project {project}")
    for bucket in sorted({m.group(1) for m in GS_URL.finditer(text)} - BUCKETS):
        problems.append(f"bucket {bucket}")
    for token in sorted(set(re.findall(r"fake-access-token\S*", text)) - {FAKE_TOKEN}):
        problems.append(f"token {token}")
    if "127.0.0.1" in text or "BaseHTTP" in text:
        problems.append("the address or banner of the capture's local server")
    return problems


def bucket_names_in(stdout):
    """The bucket names a recorded or canned stdout names: `gs://` URLs (du lines, URLs in error
    text) and, in JSON, a bucket resource's `name` and a `bucket`/`bucket_name` field."""
    names = {m.group(1) for m in GS_URL.finditer(stdout)}
    try:
        stack = [json.loads(stdout)]
    except ValueError:
        return names
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("kind") == "storage#bucket" and isinstance(node.get("name"), str):
                names.add(node["name"])
            names |= {node[k] for k in ("bucket", "bucket_name") if isinstance(node.get(k), str)}
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return names


def test_the_scan_covers_fixtures_of_both_skills_and_the_stub_data():
    assert {p.parents[2].name for p in fixtures()} == {
        "gcp-base",
        "gcp-bucket-health",
        "gcp-functions-health",
    }
    assert len(fixtures()) >= 20 and len(stub_files()) >= 50


@pytest.mark.parametrize("path", fixtures() + stub_files(), ids=relative)
def test_no_fixture_or_canned_answer_holds_a_credential_or_a_real_identity(path):
    problems = text_problems(path.read_text())
    assert not problems, f"{relative(path)}: {problems}"


def test_the_scanner_finds_each_kind_of_leak():
    leaks = {
        "PEM private key": "-----BEGIN PRIVATE KEY-----\nMIIE",
        "private_key field": '{"private_key": "x"}',
        "service-account key": '{"type": "service_account"}',
        "OAuth access token": "ya29.a0AfH6SMBxxxxxxxxxxxx",
        "bearer token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "refresh token": '"refresh_token": "1//0g"',
        "client secret": "client_secret=abc",
        "API key": "AIza" + "x" * 35,
        "machine path": "/Users/someone/.config/gcloud/credentials.db",
        "identity token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
    }
    for name, text in leaks.items():
        assert name in text_problems(text), name
    assert any("e-mail at" in p for p in text_problems("me@gmail.com"))
    assert any("project number" in p for p in text_problems('"projectNumber": "111122223333"'))
    assert text_problems("214748364800  gs://shop-backups/") == []  # a byte count is not a number
    assert text_problems("rw-reader@shop-prod-1234.iam.gserviceaccount.com 123456789012") == []


def test_the_scanner_finds_other_identities_buckets_and_tokens():
    assert "project some-real-project-9" in text_problems("--project=some-real-project-9")
    assert "project some-real-project-9" in text_problems(
        "projects/some-real-project-9/locations/us"
    )
    assert "bucket real-customer-data" in text_problems("ERROR: gs://real-customer-data not found")
    assert "token fake-access-token-other" in text_problems("fake-access-token-other")
    assert (
        text_problems(f"--project=shop-prod-1234 gs://nope projects/{'123456789012'} {FAKE_TOKEN}")
        == []
    )
    assert bucket_names_in("1649267441664gs://shop-archive\n") == {"shop-archive"}
    resource = {"kind": "storage#bucket", "name": "real-bucket", "acl": [{"bucket": "real-bucket"}]}
    assert bucket_names_in(json.dumps([resource])) == {"real-bucket"}
    series = {"timeSeries": [{"resource": {"labels": {"bucket_name": "other-bucket"}}}]}
    assert bucket_names_in(json.dumps(series)) == {"other-bucket"}
    assert bucket_names_in('{"name": "roles/storage.admin"}') == set()  # not a bucket


def test_the_stdout_of_every_recorded_call_and_canned_file_names_only_the_stubs_buckets():
    seen = set()
    for path in fixtures():
        for call in yaml.safe_load(path.read_text())["calls"]:
            bad = bucket_names_in(call["stdout"]) - BUCKETS
            assert not bad, f"{relative(path)}: {sorted(bad)}"
            seen |= bucket_names_in(call["stdout"])
    for path in stub_files():
        if path.suffix in (".json", ".txt"):
            bad = bucket_names_in(path.read_text()) - BUCKETS
            assert not bad, f"{relative(path)}: {sorted(bad)}"
    assert seen, "the scan found no bucket name at all: it is looking in the wrong place"


def test_every_projects_token_in_a_recorded_call_is_a_stub_project():
    seen = set()
    for path in fixtures() + stub_files():
        found = {m.group(1) for m in PROJECT_TOKEN.finditer(path.read_text())}
        assert found <= PROJECTS, f"{relative(path)}: {sorted(found - PROJECTS)}"
        seen |= found
    assert seen == PROJECTS


# ---- what each fixture must be ------------------------------------------------------------------

BASE_SCENARIOS = {"healthy", "broken", "forbidden", "throttled", "api-disabled"}
# A missing project answers 403 (the existence is hidden), so a project-level "not-found" is no real
# answer: the skills that read a project have no not-found fixture; the bucket tasks have one
# (a bucket that does not exist is a real 404).
REQUIRED = {
    "gcp-base": BASE_SCENARIOS | {"unauthenticated", "project-denied"},
    "gcp-bucket-health": BASE_SCENARIOS | {"not-found"},
    "gcp-functions-health": BASE_SCENARIOS | {"not-found"},
}


def tasks():
    out = []
    for skill in sorted(SKILLS.iterdir()):
        for task in sorted((skill / "tests").iterdir()) if (skill / "tests").is_dir() else []:
            out.append((skill.name, task.name))
    return out


@pytest.mark.parametrize("skill, task", tasks())
def test_every_task_has_its_required_scenarios(skill, task):
    names = {p.stem for p in (SKILLS / skill / "tests" / task).glob("*.yaml")}
    assert REQUIRED[skill] <= names, REQUIRED[skill] - names
    # and every task of the skill has a directory of fixtures
    declared = {
        t["name"] for t in yaml.safe_load((SKILLS / skill / "tasks.yaml").read_text())["tasks"]
    }
    assert {t for s, t in tasks() if s == skill} == declared


EXPECTED = {
    "forbidden": ("failed", ["permission_denied"]),
    "not-found": ("failed", ["not_found"]),
    "throttled": ("failed", ["rate_limited"]),
    "unauthenticated": ("failed", ["permission_denied"]),
    "api-disabled": ("failed", ["dependency"]),
    "project-denied": ("partial", ["permission_denied"]),
    "partial-throttled": ("partial", ["rate_limited"]),
    "monitoring-disabled": ("failed", ["dependency"]),
    "du-fallback": ("ok", []),
    "build-denied": ("partial", ["permission_denied"]),
    "idle": ("ok", []),
    "partial-metrics": ("partial", ["rate_limited"]),
    "leaky": ("ok", []),
    "buckets-denied": ("partial", ["permission_denied"]),
    "functions-denied": ("partial", ["permission_denied"]),
    "functions-disabled": ("partial", ["dependency"]),
    "functions-unreachable": ("partial", ["unreachable"]),
    "functions-split": ("ok", []),
    "healthy": ("ok", []),
    "broken": ("ok", []),
}


@pytest.mark.parametrize("path", fixtures(), ids=relative)
def test_each_fixture_expects_what_its_name_says_and_asserts_its_outputs(path):
    doc = yaml.safe_load(path.read_text())
    name = path.stem
    for suffix in ("-bucket", "-gen1", "-no-region"):  # a second fixture of the same scenario
        name = name.removesuffix(suffix)
    status, error_codes = EXPECTED[name]
    assert doc["expect"]["status"] == status and doc["expect"]["errorCodes"] == error_codes
    if status in ("ok", "partial"):
        assert doc["expect"]["outputs"], "a fixture that succeeds must assert what it shows"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", doc["now"])  # replays at the recorded time


READ_ONLY_GCLOUD = {
    ("storage", "buckets", "list"), ("storage", "buckets", "describe"),
    ("storage", "buckets", "get-iam-policy"), ("storage", "du"), ("projects", "describe"),
    ("auth", "print-access-token"), ("functions", "list"), ("functions", "describe"),
    ("builds", "describe"), ("logging", "read"),
}  # fmt: skip


@pytest.mark.parametrize("path", fixtures(), ids=relative)
def test_every_recorded_call_is_a_read_only_call_against_the_stub_projects(path):
    doc = yaml.safe_load(path.read_text())
    assert doc["resource"].split("/")[2] in PROJECTS
    for call in doc["calls"]:
        argv = call["argv"]
        if argv[0] == "curl":
            assert argv[1:6] == ["-sS", "--fail-with-body", "--max-redirs", "0", "-H"]
            assert argv[6].startswith("@") and not argv[6].startswith("@/"), (
                "a relative header file"
            )
            assert argv[7].startswith("https://monitoring.googleapis.com/v3/projects/")
            assert FAKE_TOKEN not in " ".join(argv) and "Bearer" not in " ".join(argv)
            assert "-X" not in argv and "--data" not in argv and "-d" not in argv
            assert argv[7].split("?")[0].endswith("/timeSeries")  # the one read-only endpoint
        else:
            assert argv[0] == "gcloud" and argv[-1] == "--quiet", argv
            assert any(tuple(argv[1 : 1 + n]) in READ_ONLY_GCLOUD for n in (2, 3)), argv
            if argv[1:3] != ["auth", "print-access-token"]:  # a function target pays nothing there
                assert f"--billing-project={doc['resource'].split('/')[2]}" in argv
            assert not any(a.startswith(("--impersonate", "--access-token")) for a in argv)
        for arg in argv:
            if arg.startswith("--project="):
                assert arg.split("=", 1)[1] in PROJECTS
        if call["rc"] == 0 and "--format=json" in argv:
            json.loads(call["stdout"])


def test_the_recorded_error_messages_name_no_server_of_the_capture():
    for path in fixtures() + stub_files():
        text = path.read_text()
        assert (
            "127.0.0.1" not in text
            and "BaseHTTP" not in text
            and "CLOUDSDK_AUTH_ACCESS_TOKEN" not in text
        ), path


# ---- canaries: synthetic secrets that prove a task returns none ---------------------------------
CANARY = "RWCANARY"
CANARY_HOMES = {"functions-leaky"}  # the one stub scenario whose answers hold them


def canary_files():
    return sorted(
        p for p in PLUGIN.rglob("*") if p.is_file() and CANARY in p.read_text(errors="ignore")
    )


def test_canaries_are_only_where_they_are_allowed():
    """In the leaky stub scenario, in the leaky fixtures' recorded stdout, and in the code that
    makes or checks them (the capture script, the unit tests); nowhere in a skill's code, schemas
    or docs, in any other scenario or fixture, or in a fixture's `expect`."""
    found = canary_files()
    assert any(p.parent.name == "functions-leaky" for p in found), (
        "the leaky scenario has no canary"
    )
    for path in found:
        rel = path.relative_to(PLUGIN)
        parts = rel.parts
        if parts[:3] == ("tests", "stub", "scenarios"):
            assert parts[3] in CANARY_HOMES, f"{rel}: a canary outside {sorted(CANARY_HOMES)}"
        elif parts[:3] == ("tests", "stub", "capture") or parts[:2] == ("tests", "unit"):
            continue
        elif parts[0] == "skills" and "tests" in parts and path.suffix == ".yaml":
            assert path.stem == "leaky", f"{rel}: a canary in a fixture that is not a leaky one"
            doc = yaml.safe_load(path.read_text())
            assert CANARY not in json.dumps(doc["expect"]), f"{rel}: a canary in expect"
            assert CANARY not in json.dumps(
                {k: v for k, v in doc.items() if k not in ("calls", "expect")}
            )
            for call in doc["calls"]:
                assert CANARY not in json.dumps(call["argv"]), f"{rel}: a canary in an argv"
        else:
            raise AssertionError(f"{rel}: a canary where none may be")


def test_the_leaky_fixtures_exist_and_are_the_only_ones_with_canaries():
    leaky = sorted(p.relative_to(PLUGIN).as_posix() for p in fixtures() if p.stem == "leaky")
    assert leaky, "no leaky fixture"
    holding = {p.relative_to(PLUGIN).as_posix() for p in canary_files() if p.stem == "leaky"}
    assert holding == set(leaky)
