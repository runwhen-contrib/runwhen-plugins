import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_check import check_tag, parse_tag  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugins" / "kubernetes"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: kubernetes\nversion: 0.1.0\ndescription: x\nrequires: {}\n"
    )
    return tmp_path


def test_parse_tag_release():
    assert parse_tag("kubernetes/v0.1.0") == ("kubernetes", "0.1.0")


def test_parse_tag_rc():
    assert parse_tag("postgres/v1.2.3-rc.1") == ("postgres", "1.2.3-rc.1")


@pytest.mark.parametrize(
    "tag",
    ["kubernetes/0.1.0", "v0.1.0", "kubernetes/v1.2", "Kube/v1.0.0"],
)
def test_parse_tag_rejects_malformed(tag):
    with pytest.raises(ValueError):
        parse_tag(tag)


def test_check_tag_ok(repo):
    check_tag("kubernetes/v0.1.0", repo)


def test_check_tag_unknown_plugin_dir(repo):
    with pytest.raises(ValueError):
        check_tag("nope/v1.0.0", repo)


def test_check_tag_version_mismatch(repo):
    with pytest.raises(ValueError, match="0.2.0"):
        check_tag("kubernetes/v0.2.0", repo)
