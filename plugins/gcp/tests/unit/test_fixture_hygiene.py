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

# The stub's invented identities. A fixture that mentions any other project, number or e-mail was
# not recorded from the stub.
PROJECTS = {"shop-prod-1234", "no-such-project-1"}
PROJECT_NUMBERS = {"123456789012", "987654321098"}
EMAIL_DOMAINS = {"shop-prod-1234.iam.gserviceaccount.com", "example.com"}

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
PROJECT_ARG = re.compile(r"(?:--project=|projects/|^)([a-z][a-z0-9-]{4,28}[a-z0-9])$")


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
    return problems


def test_the_scan_covers_fixtures_of_both_skills_and_the_stub_data():
    assert {p.parents[2].name for p in fixtures()} == {"gcp-base", "gcp-bucket-health"}
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


# ---- what each fixture must be ------------------------------------------------------------------


def tasks():
    out = []
    for skill in sorted(SKILLS.iterdir()):
        for task in sorted((skill / "tests").iterdir()) if (skill / "tests").is_dir() else []:
            out.append((skill.name, task.name))
    return out


@pytest.mark.parametrize("skill, task", tasks())
def test_every_task_has_the_four_scenarios_of_the_fixture_brief_plus_throttled(skill, task):
    names = {p.stem for p in (SKILLS / skill / "tests" / task).glob("*.yaml")}
    assert {"healthy", "broken", "forbidden", "not-found", "throttled"} <= names
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
    "partial-iam": ("partial", ["permission_denied"]),
    "healthy": ("ok", []),
    "broken": ("ok", []),
}


@pytest.mark.parametrize("path", fixtures(), ids=relative)
def test_each_fixture_expects_what_its_name_says_and_asserts_its_outputs(path):
    doc = yaml.safe_load(path.read_text())
    name = path.stem.removesuffix("-bucket")
    status, error_codes = EXPECTED[name]
    assert doc["expect"]["status"] == status and doc["expect"]["errorCodes"] == error_codes
    if status in ("ok", "partial"):
        assert doc["expect"]["outputs"], "a fixture that succeeds must assert what it shows"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", doc["now"])  # replays at the recorded time


@pytest.mark.parametrize("path", fixtures(), ids=relative)
def test_every_recorded_call_is_a_read_only_gcloud_call_against_the_stub_projects(path):
    doc = yaml.safe_load(path.read_text())
    assert doc["resource"].split("/")[2] in PROJECTS
    for call in doc["calls"]:
        argv = call["argv"]
        assert argv[0] == "gcloud" and argv[-1] == "--quiet", argv
        assert argv[1:3] in (["storage", "buckets"], ["storage", "du"], ["projects", "describe"])
        assert argv[3 if argv[1] == "storage" and argv[2] == "buckets" else 2] not in (
            "create", "delete", "update", "add-iam-policy-binding", "remove-iam-policy-binding",
        )  # fmt: skip
        assert not any("token" in a.lower() or a.startswith("--impersonate") for a in argv)
        for arg in argv:
            if arg.startswith("--project="):
                assert arg.split("=", 1)[1] in PROJECTS
        if call["rc"] == 0 and "--format=json" in argv:
            json.loads(call["stdout"])


def test_the_recordings_were_made_with_the_stub_not_a_real_account():
    # the one place a real project id or number would show is a recorded argv or stdout; the scan
    # above refuses it. Here: nothing in the fixtures names a bucket the stub does not have.
    stub_buckets = set()
    for scenario in STUB.iterdir():
        stub_buckets |= {p.name[7:-5] for p in scenario.glob("bucket-*.json")}
    used = set()
    for path in fixtures():
        for call in yaml.safe_load(path.read_text())["calls"]:
            used |= {a[5:] for a in call["argv"] if a.startswith("gs://")}
    assert used and used <= stub_buckets | {"nope"}
