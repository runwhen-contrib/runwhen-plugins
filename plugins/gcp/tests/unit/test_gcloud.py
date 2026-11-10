"""gcloud_json / gcloud_text, the error classifier, the credential marker and target parsing,
exercised through gcp-base's copy (the others are tested to be identical in test_gcp_copies.py)."""

import json
import os
import stat
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
    """The smallest ctx: .run, .workdir and the target."""

    def __init__(self, result=None, raises=None, workdir=None, resource=None):
        self.calls = []
        self.result, self.raises = result, raises
        self.workdir = workdir
        self.resource = resource

    def run(self, argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        self.calls.append({"argv": argv, "timeout": timeout, "env": env, "check": check})
        if self.raises:
            raise self.raises
        return self.result


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


PROJECT_TARGET = {"type": "gcp/project", "path": "gcp/projects/shop-prod-1234"}


def ctx_for(tmp_path, result=None, resource=None, **kwargs):
    return Ctx(result, workdir=tmp_path, resource=resource, **kwargs)


def test_gcloud_json_builds_the_argv_and_returns_the_parsed_value(tmp_path):
    ctx = ctx_for(tmp_path, done('[{"name": "a"}]'))
    assert gcloud.gcloud_json(ctx, "storage", "buckets", "list", "--project=p", "--raw") == [
        {"name": "a"}
    ]
    (call,) = ctx.calls
    assert call["argv"] == [
        "gcloud", "storage", "buckets", "list", "--project=p", "--raw", "--format=json", "--quiet",
    ]  # fmt: skip
    assert call["check"] is False and call["timeout"] == 60.0


def test_every_call_is_billed_to_the_targets_project(tmp_path):
    ctx = ctx_for(tmp_path, done("[]"), resource=PROJECT_TARGET)
    gcloud.gcloud_json(ctx, "storage", "buckets", "list", "--project=shop-prod-1234", "--raw")
    gcloud.gcloud_text(ctx, "auth", "print-access-token")
    bucket = {"type": "gcp/bucket", "path": "gcp/projects/shop-prod-1234/buckets/b1x"}
    other = ctx_for(tmp_path, done("12  gs://b1x\n"), resource=bucket)
    gcloud.gcloud_text(other, "storage", "du", "--summarize", "gs://b1x")
    for call in ctx.calls + other.calls:
        assert "--billing-project=shop-prod-1234" in call["argv"]
        assert call["argv"][-1] == "--quiet"
    assert other.calls[0]["argv"] == [
        "gcloud", "storage", "du", "--summarize", "gs://b1x", "--billing-project=shop-prod-1234", "--quiet",
    ]  # fmt: skip


def test_gcloud_text_has_no_format_flag(tmp_path):
    ctx = ctx_for(tmp_path, done("12  gs://b/\n"))
    assert gcloud.gcloud_text(ctx, "storage", "du", "--summarize", "gs://b") == "12  gs://b/\n"
    assert ctx.calls[0]["argv"] == ["gcloud", "storage", "du", "--summarize", "gs://b", "--quiet"]


def test_a_non_zero_exit_raises_with_the_classified_code_and_the_stderr_line(tmp_path):
    ctx = ctx_for(tmp_path, done(stderr="ERROR: (gcloud.x) HTTPError 429: slow down", rc=1))
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx, "x")
    assert caught.value.code == "rate_limited" and "HTTPError 429" in caught.value.message


@pytest.mark.parametrize("stdout", ["", "  \n", "not json"])
def test_empty_or_unparsable_output_is_an_internal_error_never_an_empty_result(tmp_path, stdout):
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx_for(tmp_path, done(stdout)), "x")
    assert caught.value.code == "internal"


def test_timeouts_and_a_missing_gcloud_are_classified(tmp_path):
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(
            ctx_for(tmp_path, raises=subprocess.TimeoutExpired(["gcloud"], 5)), "x", timeout=5
        )
    assert caught.value.code == "timeout"
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx_for(tmp_path, raises=FileNotFoundError()), "x")
    assert caught.value.code == "dependency"


def test_durations_accept_the_usual_units_and_reject_the_rest():
    assert gcloud.parse_duration("1m30s") == 90 and gcloud.parse_duration("500ms") == 0.5
    for bad in ("", "0s", "fast", "10", "-5s"):
        with pytest.raises(RwError) as caught:
            gcloud.parse_duration(bad)
        assert caught.value.code == "invalid_input"


# ---- the credential and the ambient sources ---------------------------------------------------

BLANKED = {
    "CLOUDSDK_AUTH_ACCESS_TOKEN": "",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "",
    "CLOUDSDK_CORE_ACCOUNT": "",
}


def marker_file(tmp_path):
    marker = tmp_path / "adc.json"
    marker.write_text(json.dumps({"type": gcloud.MARKER_TYPE, "serviceAccountEmail": "a@b.c"}))
    return marker


def test_a_service_account_key_gets_the_ambient_sources_blanked_and_no_metadata_check(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "a@b.c"}))
    env = gcloud.gcloud_env(Ctx(workdir=tmp_path), {gcloud.CREDENTIAL_ENV: str(key)})
    config = env.pop("CLOUDSDK_CONFIG")
    assert env == {**BLANKED, "CLOUDSDK_CORE_CHECK_GCE_METADATA": "false"}
    assert gcloud.CREDENTIAL_ENV not in env, (
        "the key's own file override stays as the runner set it"
    )
    assert os.path.isdir(config) and os.listdir(config) == []
    assert os.path.dirname(config) == str(tmp_path), "a fresh directory under ctx.workdir"
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o700


@pytest.mark.parametrize("credential", ["missing", "unreadable-json", "none"])
def test_no_usable_credential_file_is_neutralised_like_a_key(tmp_path, credential):
    environ = {}
    if credential == "missing":
        environ = {gcloud.CREDENTIAL_ENV: str(tmp_path / "missing")}
    elif credential == "unreadable-json":
        (tmp_path / "bad").write_text("not json")
        environ = {gcloud.CREDENTIAL_ENV: str(tmp_path / "bad")}
    env = gcloud.gcloud_env(Ctx(workdir=tmp_path), environ)
    assert {k: env[k] for k in BLANKED} == BLANKED
    assert env["CLOUDSDK_CORE_CHECK_GCE_METADATA"] == "false"


def test_the_marker_blanks_the_file_names_the_account_and_leaves_the_metadata_server_on(tmp_path):
    env = gcloud.gcloud_env(
        Ctx(workdir=tmp_path), {gcloud.CREDENTIAL_ENV: str(marker_file(tmp_path))}
    )
    assert env.pop("CLOUDSDK_CONFIG").startswith(str(tmp_path))
    assert env == {
        **BLANKED,
        gcloud.CREDENTIAL_ENV: "",
        "CLOUDSDK_CORE_CHECK_GCE_METADATA": "true",  # the metadata server IS the credential
        # Named, or gcloud lists that identity under a fresh CLOUDSDK_CONFIG and leaves it INACTIVE:
        # every call then fails as "you do not currently have an active account selected".
        "CLOUDSDK_CORE_ACCOUNT": "a@b.c",
    }


def test_a_marker_without_an_email_keeps_the_account_blank(tmp_path):
    marker = tmp_path / "adc.json"
    marker.write_text(json.dumps({"type": gcloud.MARKER_TYPE}))
    env = gcloud.gcloud_env(Ctx(workdir=tmp_path), {gcloud.CREDENTIAL_ENV: str(marker)})
    assert env["CLOUDSDK_CORE_CHECK_GCE_METADATA"] == "true"
    assert env["CLOUDSDK_CORE_ACCOUNT"] == ""


def test_a_run_gets_one_fresh_config_dir_and_every_run_a_new_one(tmp_path):
    first, second = Ctx(workdir=tmp_path), Ctx(workdir=tmp_path)
    one = gcloud.gcloud_env(first, {})["CLOUDSDK_CONFIG"]
    assert gcloud.gcloud_env(first, {})["CLOUDSDK_CONFIG"] == one  # one per task run
    assert gcloud.gcloud_env(second, {})["CLOUDSDK_CONFIG"] != one  # a fresh one per task run


def test_every_gcloud_call_runs_with_the_neutralised_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(gcloud.CREDENTIAL_ENV, str(marker_file(tmp_path)))
    ctx = ctx_for(tmp_path, done("[]"))
    gcloud.gcloud_json(ctx, "x")
    gcloud.gcloud_text(ctx, "auth", "print-access-token")
    for call in ctx.calls:
        assert call["env"]["CLOUDSDK_CORE_CHECK_GCE_METADATA"] == "true"
        assert call["env"][gcloud.CREDENTIAL_ENV] == ""
        assert call["env"]["CLOUDSDK_CORE_ACCOUNT"] == "a@b.c"  # the marker's own service account
    monkeypatch.delenv(gcloud.CREDENTIAL_ENV)
    other = ctx_for(tmp_path, done("[]"))
    gcloud.gcloud_json(other, "x")
    assert other.calls[0]["env"]["CLOUDSDK_CORE_CHECK_GCE_METADATA"] == "false"
    assert other.calls[0]["env"]["CLOUDSDK_AUTH_ACCESS_TOKEN"] == ""


def test_a_service_account_key_is_neither_changed_nor_echoed(tmp_path, monkeypatch):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account", "private_key": "SECRET-KEY-BODY"}))
    monkeypatch.setenv(gcloud.CREDENTIAL_ENV, str(key))
    ctx = ctx_for(tmp_path, done(stderr="ERROR: (gcloud.x) HTTPError 403: nope", rc=1))
    with pytest.raises(RwError) as caught:
        gcloud.gcloud_json(ctx, "x")
    assert "SECRET-KEY-BODY" not in caught.value.message
    assert all("SECRET-KEY-BODY" not in " ".join(c["argv"]) for c in ctx.calls)
    assert all("SECRET-KEY-BODY" not in json.dumps(c["env"]) for c in ctx.calls)
    assert gcloud.CREDENTIAL_ENV not in ctx.calls[0]["env"]


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


def test_a_target_without_a_type_gets_it_from_its_path():
    assert gcloud.target_path({"path": "gcp/projects/shop-prod-1234"}) == ("shop-prod-1234", None)
    path = "gcp/projects/shop-prod-1234/buckets/b1x"
    assert gcloud.target_path({"path": path}) == ("shop-prod-1234", "b1x")
    for bad in (
        "kubernetes/clusters/k/namespaces/n",
        "gcp/projects/x",
        "gcp/projects/shop-prod-1234/x/y/z",
    ):
        with pytest.raises(RwError) as caught:
            gcloud.target_path({"path": bad})
        assert caught.value.code == "invalid_input"


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
