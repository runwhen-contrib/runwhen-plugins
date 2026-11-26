import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
LIB = PLUGIN / "skills" / "plugin-tools" / "lib"
# At run time a skill's lib/ is on sys.path (CAP-12); its package is named after the skill.
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).parent))  # gitrepo, fixtures, relayfake

# The unit tests read local repositories over file://, which GitRunner refuses unless this is set
# (see plugin_tools/gitrun.py, "Test-only"). The executor never sets it.
os.environ["PLUGIN_TOOLS_ALLOW_TEST_PROTOCOLS"] = "1"

# The git and relay helpers are imported where they are used, so a test of the archive layer does
# not need them.


@pytest.fixture
def run_dir(tmp_path):
    from plugin_tools.gitrun import RunDir

    with RunDir(tmp_path) as rd:
        yield rd


@pytest.fixture
def repo(tmp_path):
    from gitrepo import GitRepo

    return GitRepo(tmp_path / "origin")


@pytest.fixture
def remote(tmp_path):
    from gitrepo import BareRemote

    return BareRemote(tmp_path / "remote.git", tmp_path / "seed")


def make_source(repo_url, run_dir, **spec):
    """A GitSource on a hardened runner that may read file:// (tests only)."""
    from plugin_tools.gitrun import GitRunner
    from plugin_tools.gitsource import GitSource, SourceSpec

    cfg = {"id": "t", "url": repo_url, **spec}
    parsed = SourceSpec.parse(cfg, allow_any_scheme=True)
    return GitSource(parsed, GitRunner(run_dir, url=repo_url, test_allow_protocols=("file",)))


@pytest.fixture
def source(repo, run_dir):
    return make_source(repo.url, run_dir)


@pytest.fixture
def relay():
    from relayfake import FakeRelay

    fake = FakeRelay().start()
    yield fake
    fake.stop()
