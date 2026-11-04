# Smoke test for the aws@1 runtime image. Run it inside the image:
#   docker run --rm -i --entrypoint sh <image> -s < runtimes/aws/smoke.sh
set -eu

# Non-root, like the base image.
[ "$(id -u)" != "0" ] || { echo "image runs as root" >&2; exit 1; }

# The pinned CLI, v2 (a v1 would print aws-cli/1.x).
aws --version | grep -q '^aws-cli/2\.' || { echo "aws CLI is not v2" >&2; exit 1; }
aws --version
jq --version
curl --version | head -n 1
python3 --version
# The SDK and its RE2 dependency.
python3 -c "import runwhen_capability, re2"

# No pager, so a task never blocks on one.
[ -z "${AWS_PAGER-unset}" ] || { echo "AWS_PAGER is not empty" >&2; exit 1; }

# The install helpers and the tools the plugin does not rely on stay out.
for cli in unzip sam cdk eksctl; do
  if command -v "$cli" >/dev/null 2>&1; then
    echo "unexpected CLI in the image: $cli" >&2
    exit 1
  fi
done
echo "smoke ok"
