"""Auth script for the `gke` method of the kubernetes integration (D67, CONTRACTS F7, V4-1).

Runs in the executor, in the kubernetes runtime image, before the task script. It turns a GCP
credential and the connection's inputs into a kubeconfig for one GKE cluster, written to
$RW_AUTH_OUT_FILE (mode 0600).

Credential: $RW_AUTH_REF_DIR holds the service-account key (one file, whatever its name: `value`
for a Vault secret, `key.json` for a Secret) or nothing, which means cloud identity (the metadata
server, Workload Identity). Inputs: $RW_AUTH_INPUT_PROJECT, _LOCATION and _CLUSTER.

What it does: asks `gke-gcloud-auth-plugin` for an access token (to prove the credential works
and to read the cluster), reads the cluster's endpoint and CA from the GKE API, and writes a
kubeconfig whose user is an `exec` entry that runs the same plugin. The token is NOT written
into the kubeconfig: kubectl mints one on each use, and the exec entry points the plugin at the
key file in $RW_AUTH_REF_DIR, which the SDK keeps until the task ends.

On a failure it writes one `{code, message}` JSON line to $RW_AUTH_OUT_ERROR and exits 1. The
codes are the closed D64 set. It never prints the token, the key or the CA, and no message holds
them or the plugin's stderr (that is classified, never echoed).

$RW_GKE_API_BASE is for tests only: it replaces the GKE API origin, and is ignored unless it
points at 127.0.0.1 or localhost.
"""

import base64
import binascii
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PLUGIN = "gke-gcloud-auth-plugin"
# Without this the plugin runs `gcloud config config-helper`, and the runtime has no gcloud.
PLUGIN_ARGS = ["--use_application_default_credentials"]
KEY_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
EXEC_API_VERSION = "client.authentication.k8s.io/v1beta1"
API_BASE = "https://container.googleapis.com"
API_TEST_HOSTS = ("127.0.0.1", "localhost")
PLUGIN_TIMEOUT = 30
API_TIMEOUT = 30
MAX_KEY_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
LOCATION_RE = re.compile(r"[a-z0-9-]+")
CLUSTER_RE = re.compile(r"[a-z]([-a-z0-9]{0,38}[a-z0-9])?")
ENDPOINT_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")

# Plugin stderr phrases that mean the credential could not be exchanged because something was
# unreachable, not because it was refused. Matched, never echoed.
UNREACHABLE_HINTS = ("dial tcp", "no such host", "i/o timeout", "connection refused", "metadata")


class AuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _single_line(name, value):
    if any(c in value for c in "\r\n\x00"):
        raise AuthError("invalid_input", f"{name} must be a single line without NUL")
    return value


def read_inputs(env):
    """(project, location, cluster) from RW_AUTH_INPUT_*, validated."""

    def get(name, pattern, rule):
        value = env.get(f"RW_AUTH_INPUT_{name.upper()}")
        if value is None or value == "":
            raise AuthError("invalid_input", f"input {name} is required")
        _single_line(f"input {name}", value)
        if not pattern.fullmatch(value):
            raise AuthError("invalid_input", f"input {name} must be {rule}")
        return value

    return (
        get("project", PROJECT_RE, "a GCP project ID (6-30 lowercase letters, digits, hyphens)"),
        get("location", LOCATION_RE, "a GCP region or zone (lowercase letters, digits, hyphens)"),
        get("cluster", CLUSTER_RE, "a GKE cluster name (1-40 lowercase letters, digits, hyphens)"),
    )


def read_key_file(ref_dir):
    """The absolute path of the service-account key file, or None for cloud identity.

    Exactly one file may be there; it must parse as a service-account key. The key is checked,
    never printed.
    """
    if not ref_dir or not os.path.isdir(ref_dir):
        return None
    entries = sorted(os.listdir(ref_dir))
    if not entries:
        return None
    if len(entries) != 1:
        raise AuthError(
            "invalid_input",
            f"the credential reference must hold exactly one key file; it holds {len(entries)}",
        )
    path = os.path.abspath(os.path.join(ref_dir, entries[0]))
    if not os.path.isfile(path):
        raise AuthError("invalid_input", "the credential reference is not a regular file")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_KEY_BYTES + 1)
        if len(raw) > MAX_KEY_BYTES:
            raise AuthError("invalid_input", "the credential key file is too large")
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise AuthError(
            "invalid_input", "the credential key file is not a JSON service-account key"
        ) from None
    if not isinstance(doc, dict) or doc.get("type") != "service_account":
        raise AuthError(
            "invalid_input",
            'the credential key file is not a service-account key (type != "service_account")',
        )
    return path


def api_base(env):
    """The GKE API origin; $RW_GKE_API_BASE only counts when it is a loopback address (tests)."""
    override = env.get("RW_GKE_API_BASE")
    if override:
        try:
            parsed = urllib.parse.urlsplit(override)
            if parsed.scheme in ("http", "https") and parsed.hostname in API_TEST_HOSTS:
                if parsed.username is None and parsed.password is None:
                    return override.rstrip("/")
        except ValueError:
            pass
    return API_BASE


def mint_token(key_path):
    """An access token from the plugin's ExecCredential. The plugin's stderr is classified only."""
    plugin_env = dict(os.environ)
    if key_path:
        plugin_env[KEY_ENV] = key_path
    try:
        done = subprocess.run(
            [PLUGIN, *PLUGIN_ARGS],
            env=plugin_env,
            capture_output=True,
            text=True,
            timeout=PLUGIN_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise AuthError("dependency", f"{PLUGIN} is not installed in this runtime") from None
    except subprocess.TimeoutExpired:
        raise AuthError("timeout", f"{PLUGIN} did not answer within {PLUGIN_TIMEOUT}s") from None
    except OSError:
        raise AuthError("dependency", f"{PLUGIN} cannot be run in this runtime") from None
    if done.returncode != 0:
        first_line = (done.stderr or "").strip().splitlines()[:1]
        stderr = (first_line[0] if first_line else "").lower()
        if any(hint in stderr for hint in UNREACHABLE_HINTS):
            raise AuthError("unreachable", f"{PLUGIN} could not reach Google to get a token")
        raise AuthError(
            "permission_denied", f"{PLUGIN} could not get an access token from the credential"
        )
    try:
        token = json.loads(done.stdout)["status"]["token"]
    except (ValueError, KeyError, TypeError):
        token = None
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n\x00 "):
        raise AuthError("dependency", f"{PLUGIN} did not return a token")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_cluster(base, project, location, cluster, token):
    """The GKE cluster object. Errors map to the closed D64 codes; the token is never echoed."""
    url = (
        f"{base}/v1/projects/{urllib.parse.quote(project, safe='')}"
        f"/locations/{urllib.parse.quote(location, safe='')}"
        f"/clusters/{urllib.parse.quote(cluster, safe='')}"
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=API_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (401, 403):
            raise AuthError(
                "permission_denied",
                f"the credential may not read cluster {cluster} (needs container.clusters.get "
                f"on project {project}; HTTP {status})",
            ) from None
        if status == 404:
            raise AuthError(
                "not_found", f"cluster {cluster} was not found in {location} of project {project}"
            ) from None
        if status == 429:
            raise AuthError(
                "rate_limited", "the GKE API is rate limiting this credential"
            ) from None
        raise AuthError("dependency", f"the GKE API answered HTTP {status}") from None
    except TimeoutError:
        raise AuthError("timeout", f"the GKE API did not answer within {API_TIMEOUT}s") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise AuthError(
                "timeout", f"the GKE API did not answer within {API_TIMEOUT}s"
            ) from None
        raise AuthError("unreachable", "the GKE API could not be reached") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise AuthError("dependency", "the GKE API answer is too large")
    try:
        doc = json.loads(body)
    except ValueError:
        raise AuthError("dependency", "the GKE API answer is not JSON") from None
    return endpoint_and_ca(doc)


def endpoint_and_ca(doc):
    """(endpoint, base64 CA) from the cluster object, each validated so it is safe to write."""
    endpoint = doc.get("endpoint") if isinstance(doc, dict) else None
    master = doc.get("masterAuth") if isinstance(doc, dict) else None
    ca = master.get("clusterCaCertificate") if isinstance(master, dict) else None
    if not isinstance(endpoint, str) or not ENDPOINT_RE.fullmatch(endpoint):
        raise AuthError("dependency", "the GKE API answer has no usable cluster endpoint")
    if not isinstance(ca, str) or not ca:
        raise AuthError("dependency", "the GKE API answer has no cluster CA certificate")
    try:
        base64.b64decode(ca, validate=True)
    except (binascii.Error, ValueError):
        raise AuthError("dependency", "the cluster CA certificate is not valid base64") from None
    return endpoint, ca


def build_kubeconfig(project, location, cluster, endpoint, ca, key_path):
    """The kubeconfig as a dict (JSON is a valid kubeconfig). No token goes in."""
    name = f"gke_{project}_{location}_{cluster}"
    exec_entry = {
        "apiVersion": EXEC_API_VERSION,
        "command": PLUGIN,
        "args": list(PLUGIN_ARGS),
        "provideClusterInfo": True,
        "interactiveMode": "Never",
    }
    if key_path:
        exec_entry["env"] = [{"name": KEY_ENV, "value": key_path}]
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": name,
                "cluster": {"server": f"https://{endpoint}", "certificate-authority-data": ca},
            }
        ],
        "users": [{"name": name, "user": {"exec": exec_entry}}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
        "current-context": name,
    }


def write_private(path, text):
    """Write text to path, mode 0600 from the first byte (and for a file that already existed)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(text)
    finally:
        if fd is not None:
            os.close(fd)


def build(env):
    project, location, cluster = read_inputs(env)
    key_path = read_key_file(env.get("RW_AUTH_REF_DIR"))
    token = mint_token(key_path)
    endpoint, ca = get_cluster(api_base(env), project, location, cluster, token)
    return build_kubeconfig(project, location, cluster, endpoint, ca, key_path)


def main(env=None):
    env = os.environ if env is None else env
    try:
        config = build(env)
        out = env.get("RW_AUTH_OUT_FILE")
        if not out:
            raise AuthError("internal", "RW_AUTH_OUT_FILE is not set")
        write_private(out, json.dumps(config, indent=2) + "\n")
    except AuthError as exc:
        target = env.get("RW_AUTH_OUT_ERROR")
        if target:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"code": exc.code, "message": exc.message}) + "\n")
        print(f"auth/gke.py: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
