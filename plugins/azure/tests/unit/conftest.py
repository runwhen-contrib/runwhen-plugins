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
STUB = PLUGIN / "tests" / "stub" / "bin" / "az"
SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
SUBSCRIPTION = {
    "path": f"azure/subscriptions/{SUB}",
    "type": "azure/subscription",
    "name": SUB,
}


GROUP_OF = {
    "kv-orders-dev": "rg-orders-dev",
    "kv-orders-prod": "rg-orders-prod",
    "nope": "rg-orders-dev",
}


def vault(name, group=None):
    """The target of a Key Vault run, as the SDK builds it from the resource path: the identity
    fields are the ancestors by type (subscription, resourcegroup) and `name`."""
    group = group or GROUP_OF.get(name, "rg-orders-dev")
    return {
        "path": f"azure/subscriptions/{SUB}/resourcegroups/{group}/keyvaults/{name}",
        "type": "azure/keyvault",
        "subscription": SUB,
        "resourcegroup": group,
        "name": name,
    }


SETTINGS = {
    "maxItems": 200,
    "commandTimeout": "60s",
    # declared by azure-kv-health only; harmless elsewhere
    "availabilityMinPercent": 100,
    "requestThreshold": 1000,
    "latencyThresholdMs": 500,
    "metricInterval": "PT1H",
}


def load_script(name, skill):
    spec = importlib.util.spec_from_file_location(
        f"{skill.replace('-', '_')}_{name}", PLUGIN / "skills" / skill / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubAz:
    """Stands in for ctx.run: answers `az` with the plugin's stub script, scenario by scenario.

    The skills call az with check=False and classify the exit themselves (the SDK's ctx.run would
    raise on a non-zero exit under v13), so a call that does not pass check=False fails here."""

    def __init__(self, scenario, log):
        self.scenario = scenario
        self.calls = []  # the argv of every call, without "az" and "--output json"
        self.timeouts = []
        self.log = log

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False, "az_json must pass check=False and classify the exit itself"
        assert argv[0] == "az" and argv[-2:] == ["--output", "json"]
        self.calls.append(argv[1:-2])
        self.timeouts.append(timeout)
        done = subprocess.run(
            [str(STUB), *argv[1:]],
            env={"PATH": "/usr/bin:/bin", "AZURE_STUB_SCENARIO": self.scenario},
            capture_output=True,
            text=True,
        )
        assert done.returncode != 2, done.stderr  # the stub has no answer: the test is wrong
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)


class Replies:
    """Like StubAz, but answers from a dict of argv tuple -> (rc, stdout, stderr)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        assert check is False
        key = tuple(argv[1:-2])
        self.calls.append(list(key))
        rc, out, err = self.answers[key]
        return subprocess.CompletedProcess(argv, rc, out, err)


@pytest.fixture
def run_script():
    """run_script(skill, task_script, scenario=..., resource=..., **inputs): the script's main
    under the real v13 context with az answered by the stub. Returns the result envelope, the
    errors, the files written under $RW_OUT and the az calls made."""

    def run(
        skill,
        script,
        *,
        scenario="healthy",
        resource=SUBSCRIPTION,
        settings=None,
        runner=None,
        now=None,
        **inputs,
    ):
        main = load_script(script, skill).main
        kwargs = {"now": now} if now is not None else {}
        with make_context(
            resource=resource, settings={**SETTINGS, **(settings or {})}, **kwargs
        ) as tc:
            fake = runner or StubAz(scenario, None)
            tc.ctx.run = fake
            envelope = run_main(main, tc, **inputs)
            files = {p.name: p.read_text() for p in Path(tc.out_dir).iterdir()}
            return SimpleNamespace(
                envelope=envelope, errors=list(tc.errors), files=files, calls=fake.calls
            )

    return run
