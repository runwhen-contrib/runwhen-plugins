# Smoke test for the universal@1 runtime image, and the conformance check for custom images built
# on it. Run it inside the image:
#   docker run --rm -i --entrypoint sh <image> -s < runtimes/universal/smoke.sh
# It never contacts a cloud provider: `--version` and `help` read the installed CLI only, and every
# CLI's state goes to a throwaway directory, so no credential of the machine running it is used.
# A custom image may add tools; it must keep every tool and version below.
set -eu

# Non-root, like the base image.
[ "$(id -u)" != "0" ] || { echo "image runs as root" >&2; exit 1; }

# The image sets no per-CLI state directory: the SDK points each CLI at a private one per task run.
for var in CLOUDSDK_CONFIG AZURE_CONFIG_DIR AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE; do
  eval "val=\${$var:-}"
  if [ -n "$val" ]; then
    echo "image sets $var=$val" >&2
    exit 1
  fi
done

# The runner mounts only /work writable (read-only root), so use scratch directories under it.
scratch=/work
[ -w "$scratch" ] || scratch=/tmp
TMPDIR="$scratch/smoke.$$"
HOME="$TMPDIR/home"
mkdir -p "$TMPDIR" "$HOME"
export TMPDIR HOME
export CLOUDSDK_CONFIG="$TMPDIR/gcloud" AZURE_CONFIG_DIR="$TMPDIR/az" XDG_CONFIG_HOME="$HOME/.config" XDG_CACHE_HOME="$HOME/.cache"
trap 'rm -rf "$TMPDIR"' EXIT

# expect <what> <expected substring> <command output>: the pinned version, as runtime.yaml says.
expect() {
  case "$3" in
    *"$2"*) echo "$1 $2" ;;
    *) echo "$1: expected $2 in: $3" >&2; exit 1 ;;
  esac
}

expect kubectl v1.37.1 "$(kubectl version --client)"
expect helm v4.3.2 "$(helm version --short)"
expect oc 5.0.1 "$(oc version --client)"
expect flux 2.10.1 "$(flux --version)"
expect argocd v3.6.3 "$(argocd version --client)"
expect aws aws-cli/2.39.3 "$(aws --version)"
# `az version` asks PyPI for updates; read the installed package instead, so the check stays offline.
expect az 2.92.0 "$(/opt/az/bin/python -c 'import importlib.metadata as m; print(m.version("azure-cli"))')"
expect gcloud 592.0.0 "$(gcloud --version)"
expect yq v4.53.6 "$(yq --version)"
expect jq jq-1.7.1 "$(jq --version)"
expect curl 8.14.1 "$(curl --version | head -n 1)"
expect git 2.47.3 "$(git --version)"
expect psql 17.11 "$(psql --version)"
expect redis-cli 8.0.2 "$(redis-cli --version)"
expect python 3.12.14 "$(python3 --version)"
# The kubernetes plugin's `gke` auth method (kubeconfig exec entry); a static binary, so no gcloud.
gke-gcloud-auth-plugin --version

# No login happened: asking for the account must fail, not hang on a prompt or a device code.
if az account show >/dev/null 2>&1; then
  echo "az account show succeeded without a login" >&2
  exit 1
fi
az keyvault list --help >/dev/null
az rest --help >/dev/null
gcloud storage --help >/dev/null
gcloud config list --format=json >/dev/null
[ "${CLOUDSDK_PYTHON:-}" = "python3" ] || { echo "CLOUDSDK_PYTHON is not python3" >&2; exit 1; }
[ -z "${AWS_PAGER-unset}" ] || { echo "AWS_PAGER is not empty" >&2; exit 1; }
# gsutil, bq and gcloud's bundled python are not in the image.
for tool in gsutil bq; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "unexpected tool in the image: $tool" >&2
    exit 1
  fi
done
[ ! -e /usr/lib/google-cloud-sdk/platform/bundledpythonunix ] || { echo "bundled python is in the image" >&2; exit 1; }

# CronJob spec.timeZone is resolved with zoneinfo. The system files prove the apt tzdata install:
# the PyPI tzdata package in the venv would let the zoneinfo call below pass without it.
test -f /usr/share/zoneinfo/America/New_York
python3 -c "import zoneinfo; zoneinfo.ZoneInfo('America/New_York')"

# The SDK, its RE2 dependency and the libraries skills import, at the pinned versions.
python3 - <<'PY'
import importlib.metadata as md

import boto3, dateutil, jmespath, kubernetes, re2, requests, runwhen_capability, yaml  # noqa: F401,E401

want = {
    "runwhen-capability": "0.4.0",
    "requests": "2.34.2",
    "PyYAML": "6.0.3",
    "jmespath": "1.1.0",
    "python-dateutil": "2.9.0.post0",
    "kubernetes": "37.0.3",
    "boto3": "1.44.12",
}
for name, version in want.items():
    got = md.version(name)
    if got != version:
        raise SystemExit(f"{name}: expected {version}, found {got}")
    print(name, got)
PY

# The task host the runner starts must still be on PATH.
command -v rwtask >/dev/null || { echo "rwtask is not on PATH" >&2; exit 1; }
echo "smoke ok"
