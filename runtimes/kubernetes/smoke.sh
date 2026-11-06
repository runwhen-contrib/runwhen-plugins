# Smoke test for the kubernetes@1 runtime image. Run it inside the image:
#   docker run --rm -i --entrypoint sh <image> -s < runtimes/kubernetes/smoke.sh
set -eu

# Non-root, like the base image.
[ "$(id -u)" != "0" ] || { echo "image runs as root" >&2; exit 1; }

kubectl version --client
jq --version
curl --version | head -n 1
git --version
python3 --version
psql --version
# The kubernetes plugin's `gke` auth method (kubeconfig exec entry); a static binary, so no gcloud.
gke-gcloud-auth-plugin --version

# CronJob spec.timeZone is resolved with zoneinfo. The system files prove the apt tzdata install:
# the PyPI tzdata package in the venv would let the zoneinfo call below pass without it.
test -f /usr/share/zoneinfo/America/New_York
python3 -c "import zoneinfo; zoneinfo.ZoneInfo('America/New_York')"
# The SDK and its RE2 dependency.
python3 -c "import runwhen_capability, re2"

# The CLIs the plugins deliberately do not rely on stay out.
for cli in oc helm flux argocd; do
  if command -v "$cli" >/dev/null 2>&1; then
    echo "unexpected CLI in the image: $cli" >&2
    exit 1
  fi
done
echo "smoke ok"
