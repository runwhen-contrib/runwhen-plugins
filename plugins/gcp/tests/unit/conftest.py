import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runwhen_capability.testing import make_context, run_main

# At run time a skill's lib/ is on sys.path (CAP-12); its package is named after the skill.
for lib in sorted((Path(__file__).parents[2] / "skills").glob("*/lib")):
    sys.path.insert(0, str(lib))

PLUGIN = Path(__file__).parents[2]
STUB = PLUGIN / "tests" / "stub"

SETTINGS = {"maxItems": 200, "commandTimeout": "60s"}
PROJECT = {
    "path": "gcp/projects/shop-prod-1234",
    "type": "gcp/project",
    "name": "shop-prod-1234",
}


def bucket_target(name, project="shop-prod-1234"):
    return {
        "path": f"gcp/projects/{project}/buckets/{name}",
        "type": "gcp/bucket",
        "project": project,
        "name": name,
    }


def load_script(name, skill):
    spec = importlib.util.spec_from_file_location(
        f"{skill.replace('-', '_')}_{name.replace('-', '_')}",
        PLUGIN / "skills" / skill / "scripts" / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_module():
    loader = importlib.machinery.SourceFileLoader("gcloud_stub", str(STUB / "bin" / "gcloud"))
    spec = importlib.util.spec_from_loader("gcloud_stub", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class StubRun:
    """Stands in for ctx.run: answers gcloud from a scenario of the stub (the same canned data a
    fixture is recorded from), exactly as the stub would, plus per-test overrides.

    Like the SDK's ctx.run under v13 it would raise on a non-zero exit, but gcloud_json must ask
    for check=False and classify itself, so a call that does not pass check=False fails here."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.stub = stub_module()
        directory = STUB / "scenarios" / scenario
        self.directory = directory
        self.calls = self.stub.parse_calls((directory / "calls.yaml").read_text())
        self.log = []  # {"argv", "asked", "env", "timeout"}
        self.overrides = []

    def set(self, asked, rc=0, stdout="", stderr=""):
        """Answer the (normalised) argv `asked` with this, ahead of the scenario."""
        self.overrides.append((list(asked), rc, stdout, stderr))
        return self

    def asked(self):
        return [c["asked"] for c in self.log]

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False, "gcloud_json must pass check=False and classify the exit itself"
        assert argv[0] == "gcloud" and argv[-1] == "--quiet", argv
        asked = self.stub.normalise(argv[1:])
        self.log.append({"argv": list(argv), "asked": asked, "env": env, "timeout": timeout})
        for expected, rc, stdout, stderr in self.overrides:
            if expected == asked:
                return subprocess.CompletedProcess(argv, rc, stdout, stderr)
        for call in self.calls:
            if call["argv"] == asked:
                stdout = ""
                if call.get("stdout"):
                    stdout = (self.directory / call["stdout"]).read_text()
                return subprocess.CompletedProcess(
                    argv, call.get("rc", 0), stdout, call.get("stderr", "")
                )
        raise AssertionError(f"scenario {self.scenario} has no answer for {asked}")


@pytest.fixture
def stub_run():
    def make(scenario):
        return StubRun(scenario)

    return make


@pytest.fixture
def run_script():
    """run_script("discover", skill="gcp-base", stub=StubRun, resource=..., **inputs): the script's
    main under the real v13 context, gcloud answered by `stub`. Returns the result envelope, the
    errors and the files written under $RW_OUT."""

    def run(script, *, skill, stub, resource=PROJECT, settings=None, **inputs):
        main = load_script(script, skill).main
        with make_context(resource=resource, settings={**SETTINGS, **(settings or {})}) as tc:
            tc.ctx.run = stub
            envelope = run_main(main, tc, **inputs)
            files = {p.name: p.read_text() for p in Path(tc.out_dir).iterdir()}
            return SimpleNamespace(envelope=envelope, errors=list(tc.errors), files=files)

    return run


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "RW_SYNC_CREDENTIAL",
        "RW_SYNC_ALLOW_HTTP",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    ):
        monkeypatch.delenv(name, raising=False)
