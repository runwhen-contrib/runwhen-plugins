"""gcloud_json / gcloud_text, the error classifier, the credential marker and target parsing,
exercised through gcp-base's copy (the others are tested to be identical in test_gcp_copies.py)."""

import json
import subprocess

import pytest
from gcp_base import gcloud
from gcp_base.errors import RwError

STDERR_CASES = [
    # gcloud storage, 403
    (
        "ERROR: (gcloud.storage.buckets.list) HTTPError 403: rw@p.iam.gserviceaccount.com does not "
        "have storage.buckets.list access to the Google Cloud project. Permission "
        "'storage.buckets.list' denied on resource (or it may not exist).",
        "permission_denied",
    ),
    (
        "ERROR: (gcloud.projects.describe) [rw@p.iam.gserviceaccount.com] does not have permission to "
        "access projects instance [p] (or it may not exist): The caller does not have permission",
        "permission_denied",
    ),
    (
        "ERROR: (gcloud.x) PERMISSION_DENIED: Permission denied on resource project p.",
        "permission_denied",
    ),
    # authentication: D64 has no code for it, so it is permission_denied
    (
        "ERROR: (gcloud.storage.buckets.list) There was a problem refreshing your current auth "
        "tokens: Reauthentication required.\nPlease run:\n\n  $ gcloud auth login\n",
        "permission_denied",
    ),
    (
        "ERROR: (gcloud.storage.buckets.list) You do not currently have an active account selected.",
        "permission_denied",
    ),
    (
        "ERROR: (gcloud.x) HTTPError 401: Request had invalid authentication credentials.",
        "permission_denied",
    ),
    (
        "ERROR: (gcloud.x) UNAUTHENTICATED: Request is missing required authentication credential",
        "permission_denied",
    ),
    ("ERROR: (gcloud.x) invalid_grant: Invalid JWT Signature.", "permission_denied"),
    # not found
    ("ERROR: (gcloud.storage.buckets.describe) gs://nope not found: 404.", "not_found"),
    (
        "ERROR: (gcloud.storage.buckets.describe) HTTPError 404: The specified bucket does not exist.",
        "not_found",
    ),
    ("ERROR: (gcloud.projects.describe) Project 'p' not found or deleted.", "not_found"),
    ("ERROR: (gcloud.x) NOT_FOUND: Requested entity was not found.", "not_found"),
    # throttling, including a 403 that says rateLimitExceeded
    (
        "ERROR: (gcloud.x) RESOURCE_EXHAUSTED: Quota exceeded for quota metric 'Read requests'",
        "rate_limited",
    ),
    (
        "ERROR: (gcloud.x) HTTPError 429: The rate of change requests per second for this bucket has been exceeded.",
        "rate_limited",
    ),
    ("ERROR: (gcloud.x) HTTPError 403: Rate Limit Exceeded rateLimitExceeded", "rate_limited"),
    # API disabled is a 403 too, but it is a missing dependency
    (
        "ERROR: (gcloud.x) HTTPError 403: Cloud Storage API has not been used in project 123 before or "
        "it is disabled. SERVICE_DISABLED",
        "dependency",
    ),
    # network
    (
        "ERROR: (gcloud.x) Failed to establish a new connection: [Errno -2] Name or service not known",
        "unreachable",
    ),
    ("ERROR: (gcloud.x) HTTPSConnectionPool: Max retries exceeded with url", "unreachable"),
    ("ERROR: (gcloud.x) HTTPError 503: Service Unavailable", "unreachable"),
    ("ERROR: (gcloud.x) SSLError: certificate verify failed", "unreachable"),
    # timeouts and bad input
    (
        "ERROR: (gcloud.x) DEADLINE_EXCEEDED: Deadline expired before operation could complete.",
        "timeout",
    ),
    ("ERROR: (gcloud.storage.buckets.list) HTTPError 400: Invalid argument.", "invalid_input"),
    ("ERROR: (gcloud.x) argument --project: Invalid choice", "invalid_input"),
    # nothing recognised
    ("ERROR: (gcloud.x) something unexpected happened", "internal"),
    ("", "internal"),
]


@pytest.mark.parametrize("stderr, code", STDERR_CASES)
def test_classify_error_maps_gcloud_stderr_to_the_d64_codes(stderr, code):
    assert gcloud.classify_error(stderr, 1) == code


def test_exit_codes_127_and_124_have_their_own_meaning():
    assert gcloud.classify_error("gcloud: command not found", 127) == "dependency"
    assert gcloud.classify_error("", 124) == "timeout"
    assert gcloud.classify_error("HTTPError 404", 124) == "not_found"  # the words win


def test_error_text_starts_at_the_error_line_collapses_whitespace_and_is_bounded():
    noisy = (
        "WARNING: python 3.9 is deprecated\n\nERROR: (gcloud.x) HTTPError 403:   no\n  good\n"
        + "x" * 2000
    )
    text = gcloud.error_text(noisy)
    assert text.startswith("ERROR: (gcloud.x) HTTPError 403: no good") and len(text) == 1024
    assert text.endswith("...") and "WARNING" not in text


@pytest.mark.parametrize(
    "leak",
    [
        "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxx",
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        '"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEv\\n-----END PRIVATE KEY-----\\n"',
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----",
        '"client_secret": "d-FL95Q19q7MQmFpd7hHD0Ty"',
        '"refresh_token": "1//0gabcdef"',
    ],
)
def test_a_secret_that_slips_into_stderr_is_scrubbed(leak):
    shown = gcloud.error_text(f"ERROR: (gcloud.x) HTTPError 403: bad {leak} here")
    assert "[redacted]" in shown
    for fragment in ("ya29", "abcdefghij", "MIIEv", "d-FL95", "1//0g"):
        assert fragment not in shown


class Ctx:
    """The smallest ctx: .run only."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result, self.raises = result, raises

    def run(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        self.calls.append({"argv": argv, "timeout": timeout, "env": env, "check": check})
        if self.raises:
            raise self.raises
        return self.result


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def test_gcloud_json_builds_the_argv_and_returns_the_parsed_value():
    ctx = Ctx(done('[{"name": "a"}]'))
    assert gcloud.gcloud_json(ctx, "storage", "buckets", "list", "--project=p", "--raw") == [
        {"name": "a"}
    ]
    (call,) = ctx.calls
    assert call["argv"] == [
        "gcloud", "storage", "buckets", "list", "--project=p", "--raw", "--format=json", "--quiet",
    ]  # fmt: skip
    assert call["check"] is False and call["timeout"] == 60.0


def test_gcloud_text_has_no_format_flag():
    ctx = Ctx(done("12  gs://b/\n"))
    assert gcloud.gcloud_text(ctx, "storage", "du", "--summarize", "gs://b") == "12  gs://b/\n"
    assert ctx.calls[0]["argv"] == ["gcloud", "storage", "du", "--summarize", "gs://b", "--quiet"]


def test_a_non_zero_exit_raises_with_the_classified_code_and_the_stderr_line():
    ctx = Ctx(done(stderr="ERROR: (gcloud.x) HTTPError 429: slow down", rc=1))
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx, "x")
    assert caught.value.code == "rate_limited" and "HTTPError 429" in caught.value.message


@pytest.mark.parametrize("stdout", ["", "  \n", "not json"])
def test_empty_or_unparsable_output_is_an_internal_error_never_an_empty_result(stdout):
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(Ctx(done(stdout)), "x")
    assert caught.value.code == "internal"


def test_timeouts_and_a_missing_gcloud_are_classified():
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(Ctx(raises=subprocess.TimeoutExpired(["gcloud"], 5)), "x", timeout=5)
    assert caught.value.code == "timeout"
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(Ctx(raises=FileNotFoundError()), "x")
    assert caught.value.code == "dependency"


def test_durations_accept_the_usual_units_and_reject_the_rest():
    assert gcloud.parse_duration("1m30s") == 90 and gcloud.parse_duration("500ms") == 0.5
    for bad in ("", "0s", "fast", "10", "-5s"):
        with pytest.raises(RwError) as caught:
            gcloud.parse_duration(bad)
        assert caught.value.code == "invalid_input"


# ---- the credential file ----------------------------------------------------------------------


def test_the_cloud_identity_marker_blanks_the_credential_variable(tmp_path):
    marker = tmp_path / "adc.json"
    marker.write_text(json.dumps({"type": gcloud.MARKER_TYPE, "serviceAccountEmail": "a@b.c"}))
    env = {gcloud.CREDENTIAL_ENV: str(marker)}
    assert gcloud.credential_env(env) == {gcloud.CREDENTIAL_ENV: ""}
    ctx = Ctx(done("[]"))
    import os

    os.environ[gcloud.CREDENTIAL_ENV] = str(marker)
    try:
        gcloud.gcloud_json(ctx, "x")
    finally:
        del os.environ[gcloud.CREDENTIAL_ENV]
    assert ctx.calls[0]["env"] == {gcloud.CREDENTIAL_ENV: ""}


def test_a_service_account_key_or_no_credential_changes_nothing(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "a@b.c"}))
    assert gcloud.credential_env({gcloud.CREDENTIAL_ENV: str(key)}) == {}
    assert gcloud.credential_env({}) == {}
    assert gcloud.credential_env({gcloud.CREDENTIAL_ENV: str(tmp_path / "missing")}) == {}
    key.write_text("not json")
    assert gcloud.credential_env({gcloud.CREDENTIAL_ENV: str(key)}) == {}


def test_the_credential_file_is_never_echoed(tmp_path, monkeypatch):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account", "private_key": "SECRET-KEY-BODY"}))
    monkeypatch.setenv(gcloud.CREDENTIAL_ENV, str(key))
    ctx = Ctx(done(stderr="ERROR: (gcloud.x) HTTPError 403: nope", rc=1))
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx, "x")
    assert "SECRET-KEY-BODY" not in caught.value.message
    assert all("SECRET-KEY-BODY" not in " ".join(c["argv"]) for c in ctx.calls)


# ---- the target -------------------------------------------------------------------------------


def target(kind, path, **extra):
    return {"type": kind, "path": path, **extra}


def test_target_path_reads_a_project_or_a_bucket():
    assert gcloud.target_path(target("gcp/project", "gcp/projects/shop-prod-1234")) == (
        "shop-prod-1234",
        None,
    )
    path = "gcp/projects/shop-prod-1234/buckets/shop.assets_1"
    assert gcloud.target_path(target("gcp/bucket", path)) == ("shop-prod-1234", "shop.assets_1")


@pytest.mark.parametrize(
    "resource",
    [
        None,
        {},
        target("kubernetes/namespace", "kubernetes/clusters/k/namespaces/n"),
        target("gcp/project", "gcp/projects/Shop-Prod"),  # upper case
        target("gcp/project", "gcp/projects/x"),  # too short
        target(
            "gcp/project", "gcp/projects/shop-prod-1234/buckets/b1"
        ),  # a project path of a bucket
        target("gcp/bucket", "gcp/projects/shop-prod-1234"),  # a bucket without a bucket
        target("gcp/bucket", "gcp/projects/shop-prod-1234/buckets/UPPER"),
        target("gcp/bucket", "gcp/projects/shop-prod-1234/buckets/a..b"),
        target("gcp/bucket", "gcp/projects/shop-prod-1234/buckets/x y"),
        target("gcp/bucket", "gcp/projects/shop-prod-1234/buckets/-bad"),
    ],
)
def test_any_other_target_is_invalid_input_before_gcloud_runs(resource):
    with pytest.raises(RwError) as caught:
        gcloud.target_path(resource)
    assert caught.value.code == "invalid_input"
