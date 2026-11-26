"""The universal runtime's Dockerfile, runtime.yaml, smoke.sh and lock files say the same thing."""

import re
from pathlib import Path

import pytest
import yaml

DIR = Path(__file__).resolve().parents[2] / "runtimes" / "universal"
RUNTIME = yaml.safe_load((DIR / "runtime.yaml").read_text())
DOCKERFILE = (DIR / "Dockerfile").read_text()
SMOKE = (DIR / "smoke.sh").read_text()


def arg(name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert m, f"Dockerfile has no ARG {name}"
    return m.group(1)


def test_label_version_default_is_the_runtime_version():
    assert arg("RUNTIME_VERSION") == RUNTIME["version"]


def test_sdk_version_default_is_the_runtime_tool():
    assert arg("SDK_VERSION") == RUNTIME["tools"]["runwhen-capability"]


@pytest.mark.parametrize(
    "tool,dockerfile_arg",
    [
        ("helm", "HELM_VERSION"),
        ("oc", "OC_VERSION"),
        ("flux", "FLUX_VERSION"),
        ("argocd", "ARGOCD_VERSION"),
        ("aws", "AWSCLI_VERSION"),
        ("az", "AZURE_CLI_VERSION"),
        ("gcloud", "GCLOUD_VERSION"),
    ],
)
def test_pinned_tool_versions_match_the_dockerfile(tool, dockerfile_arg):
    assert RUNTIME["tools"][tool] == arg(dockerfile_arg)


@pytest.mark.parametrize("tool", sorted(RUNTIME["tools"]))
def test_smoke_checks_every_tool_at_its_version(tool):
    version = RUNTIME["tools"][tool]
    if tool in {"jq", "tzdata", "git", "curl", "gke-gcloud-auth-plugin"}:
        pytest.skip(
            "not version-checked in smoke.sh (jq prints jq-1.7; tzdata is a file check; the plugin prints the client-go version)"
        )
    assert version in SMOKE, f"smoke.sh never mentions {tool} {version}"


def test_every_downloaded_binary_has_a_checksum_per_arch():
    for name in ("GKE_PLUGIN", "HELM", "OC", "FLUX", "ARGOCD", "AWSCLI"):
        for arch in ("AMD64", "ARM64"):
            assert re.search(
                rf"^ARG {name}_SHA256_{arch}=[0-9a-f]{{64}}$", DOCKERFILE, re.MULTILINE
            ), (
                name,
                arch,
            )


def test_python_lock_is_hash_pinned():
    for lock in ("requirements.txt", "requirements-az.txt"):
        text = (DIR / lock).read_text()
        pins = re.findall(r"^[A-Za-z0-9_.-]+==\S+ \\$", text, re.MULTILINE)
        hashes = text.count("--hash=sha256:")
        assert pins and hashes >= len(pins), lock
