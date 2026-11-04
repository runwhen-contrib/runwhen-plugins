# Smoke test for the azure@1 runtime image. Run it inside the image:
#   docker run --rm -i --entrypoint sh <image> -s < runtimes/azure/smoke.sh
set -eu

# Non-root, like the base image.
[ "$(id -u)" != "0" ] || { echo "image runs as root" >&2; exit 1; }

# az keeps its config, logs and token cache under AZURE_CONFIG_DIR, and Python needs a TMPDIR. The
# runner mounts only /work writable (the executor gives each task a private TMPDIR there), so use
# scratch directories under it, the way the auth scripts put AZURE_CONFIG_DIR under
# $RW_CREDENTIALS_DIR.
scratch=/work
[ -w "$scratch" ] || scratch=/tmp
TMPDIR="$scratch/smoke.$$"
AZURE_CONFIG_DIR="$TMPDIR/az"
mkdir -p "$AZURE_CONFIG_DIR"
export TMPDIR AZURE_CONFIG_DIR
trap 'rm -rf "$TMPDIR"' EXIT

az version --output json
# No login happened: asking for the account must fail, not hang on a prompt or a device code.
if az account show >/dev/null 2>&1; then
  echo "az account show succeeded without a login" >&2
  exit 1
fi
# A command group the health skills use is present in the core CLI (no extension needed).
az keyvault list --help >/dev/null
az monitor metrics list --help >/dev/null
az rest --help >/dev/null
jq --version
curl --version | head -n 1
python3 --version

# The SDK and its RE2 dependency are in the base's venv, untouched by az's own environment.
python3 -c "import runwhen_capability, re2"

# The CLIs the other runtimes carry stay out.
for cli in aws gcloud oc helm flux argocd; do
  if command -v "$cli" >/dev/null 2>&1; then
    echo "unexpected CLI in the image: $cli" >&2
    exit 1
  fi
done
echo "smoke ok"
