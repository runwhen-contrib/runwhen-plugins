# Smoke test for the gcp@1 runtime image. Run it inside the image:
#   docker run --rm -i --entrypoint sh <image> -s < runtimes/gcp/smoke.sh
# It never contacts Google: `--version` and `help` read the installed CLI only, and the config
# directory is a throwaway one, so no credential or account of the machine running it is used.
set -eu

# Non-root, like the base image.
[ "$(id -u)" != "0" ] || { echo "image runs as root" >&2; exit 1; }

# A fresh config directory (the image default, /work/.config/gcloud, is only writable at run time
# when /work is mounted) so nothing from the host can leak in. The base's TMPDIR (/work/tmp) may
# not exist here, so pick the first writable directory of TMPDIR, /work and /tmp.
for dir in "${TMPDIR:-}" /work /tmp; do
  if [ -n "$dir" ] && [ -d "$dir" ] && [ -w "$dir" ]; then break; fi
done
CLOUDSDK_CONFIG="$(mktemp -d "$dir/gcloud.XXXXXX")"
export CLOUDSDK_CONFIG

gcloud --version
# gcloud storage replaces gsutil; gsutil still ships in the package and is listed for the record.
gcloud storage --help >/dev/null
gsutil version
jq --version
curl --version | head -n 1
python3 --version
# The SDK and its RE2 dependency.
python3 -c "import runwhen_capability, re2"

# Output format the skills rely on: JSON from any list verb.
gcloud config list --format=json >/dev/null

# The other cloud CLIs stay out of this runtime (kubectl and psql come with the base image; the
# plugin does not rely on them).
for cli in az aws oc helm flux argocd; do
  if command -v "$cli" >/dev/null 2>&1; then
    echo "unexpected CLI in the image: $cli" >&2
    exit 1
  fi
done
echo "smoke ok"
