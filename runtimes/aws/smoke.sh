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

# One real CLI call against a fake endpoint, as the runner runs it (uid 65532, read-only root):
# the CLI signs and sends a request with fake keys and parses a canned STS answer. No network is
# used but loopback, and nothing is written outside /work.
work="$(mktemp -d /work/smoke.XXXXXX)"
python3 - "$work/endpoint.port" <<'PY' &
import http.server, sys

BODY = (
    b'<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
    b"<GetCallerIdentityResult><Arn>arn:aws:iam::123456789012:user/smoke</Arn>"
    b"<UserId>AIDAEXAMPLE</UserId><Account>123456789012</Account></GetCallerIdentityResult>"
    b"<ResponseMetadata><RequestId>r</RequestId></ResponseMetadata></GetCallerIdentityResponse>"
)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
open(sys.argv[1], "w").write(str(server.server_port))
server.handle_request()
PY
fake=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$work/endpoint.port" ] && break; sleep 1; done
out="$(AWS_ENDPOINT_URL="http://127.0.0.1:$(cat "$work/endpoint.port")" \
  AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
  AWS_EC2_METADATA_DISABLED=true \
  aws sts get-caller-identity --region eu-west-1 --output json --no-cli-pager)" \
  || { echo "aws sts get-caller-identity against the fake endpoint failed" >&2; exit 1; }
wait "$fake"
echo "$out" | jq -e '.Account == "123456789012"' >/dev/null \
  || { echo "the fake endpoint's answer did not come back through the CLI" >&2; exit 1; }
rm -rf "$work"

# The install helpers and the tools the plugin does not rely on stay out.
for cli in unzip sam cdk eksctl; do
  if command -v "$cli" >/dev/null 2>&1; then
    echo "unexpected CLI in the image: $cli" >&2
    exit 1
  fi
done
echo "smoke ok"
