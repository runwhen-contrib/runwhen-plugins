"""Run a command with the gcloud stub first on PATH and no way to reach a real Google account.

    python3 plugins/gcp/tests/stub/harness.py <scenario> -- <command> [args...]

recording a fixture goes through record.sh, which calls this (see README.md).

`stub_env()` builds the environment; `run()` runs the command in it. The guarantees, each tested in
tests/unit/test_stub_harness.py:

* CLOUDSDK_CONFIG is a new empty temporary directory (named gcp-stub-config-*), removed afterwards,
  so a call that missed the stub and reached a real gcloud would find no account, no credential
  and no property of this machine's gcloud (the plugin gives each gcloud child its own empty
  config directory anyway; this one covers everything else);
* every GOOGLE_*, GCLOUD_* and CLOUDSDK_* variable of the caller (credential files, projects,
  access tokens, active configuration) is dropped; the metadata server is pointed at a closed
  local port (GCE_METADATA_ROOT, the variable gcloud reads) and gcloud is told not to look for
  one (CLOUDSDK_CORE_CHECK_GCE_METADATA=false);
* the stub bin directory is first on PATH, and the harness refuses to run if `gcloud`, `curl`,
  `gsutil` or `bq` does not resolve into it (the stubs serve gcloud and curl; gsutil and bq are
  guards that exit 2);
* the stubs themselves refuse to answer unless GCP_STUB_HARNESS=1, which only this file sets.

Never record against the real gcloud of this machine: it is logged in to shared infrastructure.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STUB_DIR = Path(__file__).resolve().parent
BIN = STUB_DIR / "bin"
SCENARIOS = STUB_DIR / "scenarios"
CONFIG_PREFIX = "gcp-stub-config-"  # bin/gcloud refuses a CLOUDSDK_CONFIG without this prefix
DROPPED_PREFIXES = ("GOOGLE_", "GCLOUD_", "CLOUDSDK_")
# an address nothing listens on: gcloud (GCE_METADATA_ROOT) finds no metadata server and says so
NO_METADATA = "127.0.0.1:1"
STUBBED = ("gcloud", "curl", "gsutil", "bq")


def scenario_names():
    return sorted(p.name for p in SCENARIOS.iterdir() if (p / "calls.yaml").is_file())


def stub_env(scenario, config_dir, base=None):
    """The environment for a stubbed run: `base` (default os.environ) minus every Google
    variable, with the stub first on PATH, CLOUDSDK_CONFIG = `config_dir` (an empty directory the
    caller made) and the scenario selected. Raises ValueError for an unknown scenario or when
    one of STUBBED would not be the stub."""
    if not (SCENARIOS / scenario / "calls.yaml").is_file():
        raise ValueError(f"no scenario {scenario!r}; have {', '.join(scenario_names())}")
    config = Path(config_dir)
    if not config.name.startswith(CONFIG_PREFIX) or not config.is_dir() or any(config.iterdir()):
        raise ValueError(f"{config_dir} is not an empty {CONFIG_PREFIX}* directory")
    source = dict(os.environ if base is None else base)
    env = {k: v for k, v in source.items() if not k.startswith(DROPPED_PREFIXES)}
    env["PATH"] = os.pathsep.join(
        [str(BIN), *(p for p in env.get("PATH", "").split(os.pathsep) if p)]
    )
    env.update(
        {
            "CLOUDSDK_CONFIG": str(config),
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
            "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "true",
            "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
            "GCE_METADATA_ROOT": NO_METADATA,
            "CLOUDSDK_CORE_CHECK_GCE_METADATA": "false",
            "GCP_STUB_HARNESS": "1",
            "GCP_STUB_SCENARIO": scenario,
        }
    )
    for tool in STUBBED:
        found = shutil.which(tool, path=env["PATH"])
        if not found or Path(found).resolve().parent != BIN.resolve():
            raise ValueError(f"{tool} does not resolve to the stub ({found})")
    return env


def run(scenario, command, base=None):
    """Run `command` with the stub environment and a fresh CLOUDSDK_CONFIG; returns its exit code."""
    with tempfile.TemporaryDirectory(prefix=CONFIG_PREFIX) as config:
        env = stub_env(scenario, config, base)
        return subprocess.run(command, env=env).returncode


def main(argv):
    if len(argv) < 3 or argv[1] != "--":
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        print(f"scenarios: {', '.join(scenario_names())}", file=sys.stderr)
        return 2
    return run(argv[0], argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
