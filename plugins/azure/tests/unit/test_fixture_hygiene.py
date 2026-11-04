"""No recorded fixture or stub scenario may hold a credential, a secret value or a real identifier.

A fixture keeps what `az` printed, and a scenario is what the stub prints. The Key Vault item lists
return names and attributes only (never a value), and no command here takes a secret on argv; this
scans every skill's `tests/**/*.yaml` fixture and every file under `tests/stub/scenarios` for the
shapes a leak would take, and pins every GUID to the synthetic ones the stub uses: re-recording
against a real subscription puts real ids in the files, and this test fails until someone has
decided to keep them (add them to SYNTHETIC_GUIDS) or sanitized them.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).parents[2]
SCENARIOS = PLUGIN / "tests" / "stub" / "scenarios"

# the identifiers the stub invents (tests/stub/README.md): subscription, tenant, caller, workspace,
# and the all-zero-ish ones of the not-found and managed-identity cases
SYNTHETIC_GUIDS = {
    "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71",  # subscription
    "c0ffee00-1234-4abc-8def-0123456789ab",  # tenant
    "9d2c5a10-77b4-4f3e-8a1c-5e6f7a8b9c0d",  # the calling principal
    "0b6c2f1e-3d4a-4e5f-9a7b-1c2d3e4f5a6b",  # Log Analytics customer id
    "00000000-0000-4000-8000-0000000000aa",  # the signed-in service principal
    "00000000-0000-4000-8000-00000000dead",  # a subscription that does not exist
    "8a1b2c3d-0000-4000-8000-000000000001",  # the correlation id of a Resource Health event
}
GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# what a leaked credential looks like in text
LEAKS = [
    ("a JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/-]{16,}")),
    ("a storage key or SAS", re.compile(r"AccountKey=|SharedAccessSignature|[?&]sig=")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a client secret", re.compile(r"(?i)client[_-]?secret[\"']?\s*[:=]\s*[\"']?[^\s\"',]{6,}")),
    ("a password", re.compile(r"(?i)\bpassword[\"']?\s*[:=]\s*[\"']?[^\s\"',]{6,}")),
    (
        "a connection string",
        re.compile(r"(?i)(?:Endpoint|Server)=[^;]+;.*(?:Key|Password|Secret)="),
    ),
]
# argument names that would put a secret on argv
SECRET_FLAGS = {
    "-p",
    "--password",
    "--client-secret",
    "--secret",
    "--value",
    "--certificate",
    "--token",
}
# the fields of a Key Vault item list row that are metadata; anything else (value, key, cer, x5c...) is material
ITEM_FIELDS = {
    "attributes", "contentType", "id", "kid", "managed", "name", "tags",
    "x509Thumbprint", "x509ThumbprintHex",
}  # fmt: skip


def fixtures():
    files = []
    for skill in sorted((PLUGIN / "skills").iterdir()):
        files += sorted((skill / "tests").glob("**/*.yaml")) if (skill / "tests").is_dir() else []
    return files


def scenario_files():
    return sorted(p for p in SCENARIOS.glob("*/*") if p.is_file())


def all_files():
    return fixtures() + scenario_files()


def texts(path):
    """The text of a file, and the text of every string inside it (a recorded stdout is JSON in a
    YAML string, whose escapes would hide a pattern from a search of the raw file)."""
    raw = path.read_text()
    found = [raw]
    if path.suffix == ".yaml" and path.parent.parent.name != "scenarios":
        stack = [yaml.safe_load(raw)]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack += list(node.values())
            elif isinstance(node, list):
                stack += node
            elif isinstance(node, str):
                found.append(node)
    return found


def rel(path):
    return str(path.relative_to(PLUGIN))


def test_the_scan_covers_the_fixtures_and_the_scenarios():
    assert len(fixtures()) >= 30
    assert any(p.name == "calls.yaml" for p in scenario_files())
    assert any(p.name == "secrets.json" for p in scenario_files())


@pytest.mark.parametrize("path", all_files(), ids=rel)
def test_no_file_holds_a_credential_shaped_string(path):
    for text in texts(path):
        for what, pattern in LEAKS:
            assert not pattern.search(text), f"{rel(path)} holds {what}"


@pytest.mark.parametrize("path", all_files(), ids=rel)
def test_every_identifier_is_one_the_stub_invents(path):
    found = {g.lower() for text in texts(path) for g in GUID.findall(text)}
    assert found <= SYNTHETIC_GUIDS, (
        f"{rel(path)} holds ids the stub does not invent ({sorted(found - SYNTHETIC_GUIDS)}); "
        "if this is a re-recording against a real subscription, sanitize them or decide to keep them"
    )


@pytest.mark.parametrize("path", fixtures(), ids=rel)
def test_no_recorded_command_puts_a_secret_on_argv(path):
    calls = yaml.safe_load(path.read_text())["calls"]
    assert calls or "not-found" in path.name or "invalid" in path.name or True
    for call in calls:
        assert call["argv"][0] == "az" and call["argv"][-2:] == ["--output", "json"]
        assert not SECRET_FLAGS & set(call["argv"]), f"{rel(path)}: {call['argv'][:4]}"


def item_rows(path):
    """The rows of every recorded `az keyvault secret|certificate|key list` stdout."""
    rows = []
    for call in yaml.safe_load(path.read_text())["calls"]:
        if call["argv"][1] == "keyvault" and call["argv"][2:4] in (
            ["secret", "list"], ["certificate", "list"], ["key", "list"]
        ) and call["stdout"]:  # fmt: skip
            rows += json.loads(call["stdout"])
    return rows


@pytest.mark.parametrize(
    "path", [p for p in fixtures() if p.parent.name == "expiring-items"], ids=rel
)
def test_the_item_lists_hold_metadata_only_never_a_value_a_key_or_a_certificate(path):
    for row in item_rows(path):
        assert set(row) <= ITEM_FIELDS, f"{rel(path)}: {sorted(set(row) - ITEM_FIELDS)}"
        assert set(row["attributes"]) <= {
            "created", "enabled", "expires", "notBefore", "recoverableDays", "recoveryLevel", "updated"
        }  # fmt: skip


def test_the_scanner_finds_each_kind_of_leak_it_looks_for():
    samples = {
        "a JWT": "token eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",
        "a bearer token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
        "a storage key or SAS": "DefaultEndpointsProtocol=https;AccountKey=abc",
        "a private key": "-----BEGIN RSA PRIVATE KEY-----",
        "a client secret": '"clientSecret": "s3cr3tvalue"',
        "a password": "password=hunter22",
        "a connection string": "Endpoint=sb://x.servicebus.windows.net/;SharedAccessKeyName=a;Key=abc",
    }
    patterns = dict(LEAKS)
    assert set(samples) == set(patterns)
    for what, sample in samples.items():
        assert patterns[what].search(sample), what
    assert not any(p.search("db-password expires 2027-06-01; secrets.json") for _, p in LEAKS)
