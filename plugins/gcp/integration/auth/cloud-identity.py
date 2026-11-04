"""Auth script for the `cloud-identity` method of the gcp integration (D67, CONTRACTS F7).

Runs in the executor, in the gcp runtime image, before the task script. The credential is the
identity the runner pod runs as (GKE Workload Identity, or a GCE service account) and lives in the
metadata server; nothing is stored. gcloud cannot be handed that identity through a credential
file, but it finds the metadata server on its own when it is given no file. The `gcp/adc` type is
a file, so the script writes a small marker to $RW_AUTH_OUT_FILE:

    {"type": "runwhen_cloud_identity", "serviceAccountEmail": "<email>"}

and gcp-base's gcloud helper (`gcp_base/gcloud.py`) sees that marker, blanks
CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE for the gcloud child process and leaves the metadata check
on. The marker holds no secret, and no project id or region: the project of a task is its
`ctx.resource`, never an environment variable this script produces.

What the script does before writing it:
1. validates the `project` input (and `serviceAccount`, when given);
2. asks the metadata server for the default service account's e-mail. The server is
   $GCE_METADATA_ROOT, else $GCE_METADATA_HOST, else metadata.google.internal (the variables
   gcloud and Google's libraries read); it is asked without any HTTP proxy (the same
   ProxyHandler({}) gcloud uses), so a proxy in the pod's environment cannot answer for it;
3. if `serviceAccount` is set, compares it to that e-mail: a connection that names the identity
   it expects fails here, with permission_denied, when the pod runs as another one;
4. asks for a token (.../service-accounts/default/token) and throws it away: it proves that
   minting works, so a pod whose identity exists but cannot mint fails the task run here, with a
   code, instead of as a gcloud error in the middle of a task. The token is never logged,
   written or returned.

Reference: nothing. $RW_AUTH_REF_DIR is not read (a cloud-identity reference carries no material).
The only RW_* names read are the ones the executor hands an auth script: RW_AUTH_INPUT_<NAME>
(camelCase to SCREAMING_SNAKE: `serviceAccount` is RW_AUTH_INPUT_SERVICE_ACCOUNT),
RW_AUTH_OUT_FILE and RW_AUTH_OUT_ERROR. On a failure it writes one `{code, message}` JSON line to
$RW_AUTH_OUT_ERROR and exits 1. No message contains more than the code, the host and the HTTP
status.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

MARKER_TYPE = "runwhen_cloud_identity"
DEFAULT_METADATA_HOST = "metadata.google.internal"
EMAIL_PATH = "/computeMetadata/v1/instance/service-accounts/default/email"
TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"
TIMEOUT = 5
MAX_EMAIL = 254
MAX_TOKEN_REPLY = 16 * 1024
# 6 to 30 characters: lowercase letters, digits and hyphens, starting with a letter, not ending
# with a hyphen (https://cloud.google.com/resource-manager/docs/creating-managing-projects).
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
HOST_RE = re.compile(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?")


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_project(env):
    value = env.get("RW_AUTH_INPUT_PROJECT")
    if value is None or value == "":
        raise AuthError("invalid_input", "input project is required")
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", "input project must be a single line without NUL")
    if not PROJECT_RE.fullmatch(value):
        raise AuthError(
            "invalid_input", "input project must be a project id such as shop-prod-1234"
        )
    return value


def read_service_account(env):
    """The `serviceAccount` input (the identity the connection expects), or None when not given."""
    value = env.get("RW_AUTH_INPUT_SERVICE_ACCOUNT")
    if value is None or value == "":
        return None
    if (
        any(c in value for c in "\r\n\x00")
        or len(value) > MAX_EMAIL
        or not EMAIL_RE.fullmatch(value)
    ):
        raise AuthError("invalid_input", "input serviceAccount must be a service account e-mail")
    return value


def metadata_host(env):
    host = env.get("GCE_METADATA_ROOT") or env.get("GCE_METADATA_HOST") or DEFAULT_METADATA_HOST
    if not HOST_RE.fullmatch(host):
        raise AuthError(
            "invalid_input",
            "GCE_METADATA_ROOT / GCE_METADATA_HOST must be a host name or host:port",
        )
    return host


def default_opener():
    """An opener that uses no proxy (a metadata server is never behind one) and follows no
    redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open


def get(host, path, opener=None, limit=MAX_EMAIL + 2):
    """The first `limit` bytes of the metadata server's answer to GET `path`; AuthError otherwise."""
    request = urllib.request.Request(f"http://{host}{path}", headers={"Metadata-Flavor": "Google"})
    open_ = opener or default_opener()
    try:
        with open_(request, timeout=TIMEOUT) as response:
            flavor = response.headers.get("Metadata-Flavor")
            body = response.read(limit)
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status == 404:
            raise AuthError(
                "not_found", f"the metadata server at {host} has no default service account"
            ) from None
        if status in (401, 403):
            raise AuthError(
                "permission_denied", f"the metadata server at {host} refused the request ({status})"
            ) from None
        raise AuthError("internal", f"the metadata server at {host} answered {status}") from None
    except (urllib.error.URLError, OSError):
        raise AuthError(
            "unreachable",
            f"no metadata server answers at {host}: the runner is not on GKE with Workload "
            "Identity or on GCE with a service account",
        ) from None
    if flavor != "Google":
        raise AuthError("unreachable", f"{host} answered, but it is not a Google metadata server")
    return body


def read_service_account_email(host, opener=None):
    """The default service account's e-mail from the metadata server; AuthError otherwise."""
    body = get(host, EMAIL_PATH, opener)
    try:
        email = body.decode("ascii").strip()
    except UnicodeDecodeError:
        email = ""
    if len(email) > MAX_EMAIL or not EMAIL_RE.fullmatch(email):
        raise AuthError("internal", "the metadata server returned no service account e-mail")
    return email


def probe_token(host, opener=None):
    """Proves the default service account can mint an access token. The token is read only to
    check its shape and is dropped; nothing of it is returned or printed."""
    body = get(host, TOKEN_PATH, opener, limit=MAX_TOKEN_REPLY)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        doc = None
    token = doc.get("access_token") if isinstance(doc, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise AuthError(
            "internal", f"the metadata server at {host} returned no access token for the identity"
        )


def marker(email):
    return json.dumps({"type": MARKER_TYPE, "serviceAccountEmail": email}) + "\n"


def main(env=None, opener=None):
    env = os.environ if env is None else env
    try:
        read_project(env)
        expected = read_service_account(env)
        host = metadata_host(env)
        email = read_service_account_email(host, opener)
        if expected is not None and expected.lower() != email.lower():
            raise AuthError(
                "permission_denied",
                "the pod's identity is not the service account the connection expects "
                "(serviceAccount)",
            )
        probe_token(host, opener)
        out = env.get("RW_AUTH_OUT_FILE")
        if not out:
            raise AuthError("internal", "RW_AUTH_OUT_FILE is not set")
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(marker(email))
    except AuthError as exc:
        target = env.get("RW_AUTH_OUT_ERROR")
        if target:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"code": exc.code, "message": exc.message}) + "\n")
        print(f"auth/cloud-identity.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
