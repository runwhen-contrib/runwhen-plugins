"""No recorded fixture or stub scenario may hold a credential, a secret value or a real identifier.

A fixture keeps what `az` printed, and a scenario is what the stub prints. The Key Vault item lists
return names and attributes only (never a value), and no command here takes a secret on argv; this
scans every skill's `tests/**/*.yaml` fixture and every file under `tests/stub/scenarios` for the
shapes a leak would take, and pins every identifier that names something real to the synthetic ones
the stub invents: GUIDs, e-mail addresses and UPNs, IPv4 addresses, `*.vault.azure.net` hosts, and
Key Vault, resource group and workspace names. Re-recording against a real subscription puts real
ones in the files, and this test fails until they are sanitized: replace them with the stub's.
"""

import ipaddress
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
# the names the stub invents (compared without case: ARM does not distinguish them)
SYNTHETIC_VAULTS = {"kv-orders-dev", "kv-orders-prod", "nope"}
SYNTHETIC_GROUPS = {"rg-orders-dev", "rg-orders-prod", "rg-platform", "rg-observability"}
SYNTHETIC_WORKSPACES = {"law-central"}
SYNTHETIC_ENDPOINTS = {"pe-orders"}
SYNTHETIC_EMAILS = set()  # the stub invents none: a principal is a GUID
# IPv4 addresses are only the documentation ranges (RFC 5737)
DOCUMENTATION_NETS = [
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]
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
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
VAULT_HOST = re.compile(r"(?i)\b([a-z0-9-]+)\.vault\.azure\.net\b")
NAME = r"([A-Za-z0-9_.()-]+)"  # a name ends at the first character a resource name cannot hold
RESOURCE_NAMES = [  # (what, pattern of an ARM id or resource path, the stub's set)
    (
        "resource group",
        re.compile(r"(?i)/subscriptions/[^/\s]+/resourcegroups/" + NAME),
        SYNTHETIC_GROUPS,
    ),
    (
        "Key Vault",
        re.compile(
            r"(?i)(?:/providers/microsoft\.keyvault/vaults/|/keyvaults/|--vault-name\W+|--name\W+)"
            + NAME
        ),
        SYNTHETIC_VAULTS,
    ),
    (
        "workspace",
        re.compile(
            r"(?i)(?:/providers/microsoft\.operationalinsights/workspaces/|/v1/workspaces/)" + NAME
        ),
        SYNTHETIC_WORKSPACES,
    ),
    (
        "private endpoint",
        re.compile(r"(?i)/providers/microsoft\.network/privateendpoints/" + NAME),
        SYNTHETIC_ENDPOINTS,
    ),
]
# argument names that would put a secret on argv, alone (`--password x`) or joined to their value
# (`--password=x`, and the short `-pVALUE`)
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
        "a re-recording against a real subscription must be sanitized before it is committed: "
        "replace them with the stub's"
    )


def foreign(text):
    """(what, value) of everything in `text` that names something the stub does not invent."""
    out = []
    for email in EMAIL.findall(text):
        if email.lower() not in SYNTHETIC_EMAILS:
            out.append(("an e-mail address or UPN", email))
    for ip in IPV4.findall(text):
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue  # not an address (a version number such as 2026.11.04.1)
        if not any(address in net for net in DOCUMENTATION_NETS):
            out.append(("an IPv4 address outside the documentation ranges", ip))
    for host in VAULT_HOST.findall(text):
        if host.lower() not in SYNTHETIC_VAULTS:
            out.append(("a vault host name", f"{host}.vault.azure.net"))
    for what, pattern, allowed in RESOURCE_NAMES:
        for name in pattern.findall(text):
            if name.lower() not in allowed and not GUID.fullmatch(name):
                out.append((f"a {what} name", name))
    return out


@pytest.mark.parametrize("path", all_files(), ids=rel)
def test_no_file_names_a_real_looking_address_host_vault_group_or_workspace(path):
    problems = sorted({item for text in texts(path) for item in foreign(text)})
    assert not problems, (
        f"{rel(path)} holds names the stub does not invent: {problems}. "
        "Sanitize the recording: replace them with the stub's (tests/stub/README.md)"
    )


def test_the_name_scan_finds_what_it_looks_for_and_passes_the_stub_names():
    assert foreign("alice@contoso.com ops.team@example.org") == [
        ("an e-mail address or UPN", "alice@contoso.com"),
        ("an e-mail address or UPN", "ops.team@example.org"),
    ]
    assert foreign("from 10.1.2.3 and 203.0.113.0/24 and 2026.11.04.1") == [
        ("an IPv4 address outside the documentation ranges", "10.1.2.3")
    ]
    assert foreign(
        "https://payments-prod.vault.azure.net/secrets/x https://KV-Orders-Dev.vault.azure.net/"
    ) == [("a vault host name", "payments-prod.vault.azure.net")]
    ids = "/subscriptions/x/resourceGroups/prod-rg/providers/Microsoft.KeyVault/vaults/payments"
    assert foreign(ids) == [("a resource group name", "prod-rg"), ("a Key Vault name", "payments")]
    assert foreign("azure/subscriptions/x/resourcegroups/rg-orders-dev/keyvaults/payments") == [
        ("a Key Vault name", "payments")
    ]
    assert foreign('"--vault-name", "payments"') == [("a Key Vault name", "payments")]
    assert foreign("Microsoft.KeyVault/vaults/secrets/readMetadata/action") == []
    assert (
        foreign("azure/subscriptions/s/resourcegroups/RG-Orders-DEV/keyvaults/KV-ORDERS-DEV") == []
    )
    # an action name such as Microsoft.Resources/subscriptions/resourceGroups/read is not a group
    assert foreign("'Microsoft.Resources/subscriptions/resourceGroups/read'") == []
    assert foreign(
        "/providers/Microsoft.OperationalInsights/workspaces/real-ws "
        "/v1/workspaces/0b6c2f1e-3d4a-4e5f-9a7b-1c2d3e4f5a6b/query"
    ) == [("a workspace name", "real-ws")]


def secret_flag(arg):
    """The secret flag an argv entry is, alone or joined to its value (`--password=x`, `-pxyz`)."""
    if arg in SECRET_FLAGS:
        return arg
    for flag in SECRET_FLAGS:
        if flag.startswith("--") and arg.startswith(flag + "="):
            return flag
        if not flag.startswith("--") and arg.startswith(flag) and not arg.startswith("--"):
            return flag  # -pSECRET
    return None


def argv_lists(path):
    if path.name == "calls.yaml":
        text = "".join(
            line for line in path.read_text().splitlines(True) if not line.lstrip().startswith("#")
        )
        return [c["argv"] for c in json.loads(text)["calls"]]
    if path in fixtures():
        return [c["argv"][1:] for c in yaml.safe_load(path.read_text())["calls"]]
    return []


@pytest.mark.parametrize("path", [p for p in all_files() if argv_lists(p)], ids=rel)
def test_no_recorded_command_puts_a_secret_on_argv(path):
    for argv in argv_lists(path):
        flags = [f for f in map(secret_flag, argv) if f]
        assert not flags, f"{rel(path)}: {argv[:4]} puts {flags} on argv"


@pytest.mark.parametrize("path", fixtures(), ids=rel)
def test_every_fixture_records_the_az_calls_the_task_made(path):
    calls = yaml.safe_load(path.read_text())["calls"]
    assert calls, f"{rel(path)} records no call: a fixture that replays nothing proves nothing"
    for call in calls:
        assert call["argv"][0] == "az" and call["argv"][-2:] == ["--output", "json"]


def test_the_secret_flag_check_sees_every_joined_form():
    for arg in (
        "-p",
        "--password",
        "--password=x",
        "--client-secret=abc",
        "--token=eyJ",
        "--value=v",
        "-pSECRET",
        "--secret=s",
    ):
        assert secret_flag(arg), arg
    for arg in (
        "-g",
        "--password-file",
        "--subscription",
        "--passwords",
        "-o",
        "--output=json",
        "rg-orders-dev",
    ):
        assert secret_flag(arg) is None, arg


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
