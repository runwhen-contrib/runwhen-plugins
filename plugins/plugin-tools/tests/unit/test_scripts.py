"""The two task scripts, run the way the executor runs them (SDK test helpers) against local
repositories."""

import hashlib
import importlib.util
import json

import jsonschema
import pytest
from conftest import PLUGIN
from fixtures import make_plugin_tree
from plugin_tools import gitrun, gitsource
from plugin_tools.credentials import GitCredential
from runwhen_capability.testing import make_context, run_main

SKILL = PLUGIN / "skills" / "plugin-tools"
SCHEMA = json.loads((SKILL / "schemas" / "sync-result.v1.json").read_text())
TOKEN = "ghs_0123456789abcdefghij"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"plugin_tools_script_{name}", SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sync_main():
    return _load("sync").main


@pytest.fixture
def validate_main():
    return _load("validate_upload").main


def _codes(env):
    return [e["code"] for e in env["errors"]]


def test_sync_rejects_a_bad_source_before_anything_else(sync_main):
    with make_context() as tc:
        env = run_main(sync_main, tc, source={"id": "a", "url": "http://h/r.git"})
    assert env["status"] == "failed" and _codes(env) == ["invalid_input"]


def test_sync_with_an_unreachable_host_fails_and_cleans_up(sync_main, monkeypatch):
    monkeypatch.setenv("GIT_TOKEN", TOKEN)
    with make_context() as tc:
        env = run_main(sync_main, tc, source={"id": "a", "url": "https://127.0.0.1:1/r.git"})
        assert env["status"] == "failed" and _codes(env) == ["unreachable"]
        assert TOKEN not in json.dumps(env)
        assert (
            list(tc.ctx.workdir.glob("plugin-tools-*")) == []
        )  # the run's HOME and work dir are gone


def test_sync_end_to_end_through_the_script(sync_main, repo, monkeypatch):
    repo.publish("plugins/kube", make_plugin_tree("kube", "0.1.0"), "kube/v0.1.0")
    # the script only takes https URLs and a hardened runner; open file:// for this test the way
    # the unit tests do, without touching the script
    monkeypatch.setattr(gitsource, "_check_url", lambda url: None)
    monkeypatch.setenv("GIT_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_USERNAME", "bot")
    seen = []
    real_init = gitrun.GitRunner.__init__

    def init(self, run_dir, **kw):
        real_init(self, run_dir, test_allow_protocols=("file",), **kw)
        seen.append(self.credential)

    monkeypatch.setattr(gitrun.GitRunner, "__init__", init)
    with make_context(now=None) as tc:
        env = run_main(sync_main, tc, source={"id": "pub", "url": repo.url}, max_versions=5)
        assert list(tc.ctx.workdir.glob("plugin-tools-*")) == []
        archive = tc.artifact("archive1").read_bytes()
        assert tc.artifact("archive2") is None  # unused slots are not written
    assert env["status"] == "ok"
    manifest = env["outputs"]["manifest"]
    jsonschema.validate(manifest, SCHEMA)
    assert [v["tag"] for v in manifest["versions"]] == ["kube/v0.1.0"]
    (entry,) = manifest["versions"]
    assert entry["artifact"] == "archive1"
    assert entry["archive"]["sha256"] == "sha256:" + hashlib.sha256(archive).hexdigest()
    assert (seen[0].username, seen[0].password) == ("bot", TOKEN)  # from the connection's env
    assert TOKEN not in json.dumps(manifest) and isinstance(seen[0], GitCredential)


def test_validate_upload_is_not_runnable_yet(validate_main):
    with make_context() as tc:
        env = run_main(validate_main, tc, upload_id="up_1")
    assert env["status"] == "failed" and _codes(env) == ["dependency"]
    assert "not runnable yet" in env["errors"][0]["message"]
