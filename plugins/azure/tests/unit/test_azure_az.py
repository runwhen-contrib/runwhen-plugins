"""The shared az runner (az.py): exit code first, then the ERROR lines of stderr."""

import subprocess

import pytest
from azure_base import az as az_mod
from azure_base.errors import RwError
from conftest import Replies

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"


class Ctx:
    def __init__(self, run, command_timeout="60s"):
        self.run = run
        self.settings = type("S", (), {"command_timeout": command_timeout})()


def run_az(rc, out="", err=""):
    ctx = Ctx(Replies({("account", "show"): (rc, out, err)}))
    return az_mod.az_json(ctx, ["account", "show"])


def failure(rc, err):
    with pytest.raises(RwError) as caught:
        run_az(rc, "", err)
    return caught.value


# Texts az prints. Several are captures from az 2.92.0 (marked); the others follow the wording
# Microsoft documents for the error code.
AUTHZ = (
    "ERROR: (AuthorizationFailed) The client '9d2c' with object id '9d2c' does not have authorization "
    "to perform action 'Microsoft.KeyVault/vaults/read' over scope '/subscriptions/x' or the scope is invalid."
)
CASES = [
    # permission_denied
    (1, AUTHZ + "\nCode: AuthorizationFailed\nMessage: ...", "permission_denied"),
    (
        1,
        "ERROR: (Forbidden) Caller is not authorized to perform action on resource.\nCode: Forbidden",
        "permission_denied",
    ),
    (
        1,
        'ERROR: Forbidden({"error":{"code":"AuthorizationFailed","message":"..."}})',
        "permission_denied",
    ),
    (1, "ERROR: Please run 'az login' to setup account.", "permission_denied"),  # capture, 2.92.0
    (
        1,
        "ERROR: AADSTS700016: Application with identifier 'x' was not found in the directory 'y'.",
        "permission_denied",
    ),  # capture
    (1, "ERROR: AADSTS7000215: Invalid client secret provided.", "permission_denied"),
    (
        1,
        "ERROR: (AuthorizationPermissionMismatch) This request is not authorized to perform this operation using this permission.",
        "permission_denied",
    ),
    # the Key Vault firewall and private link: the network path, not the role
    (
        1,
        'ERROR: (Forbidden) Client address is not authorized and caller is not a trusted service.\nInnerError: {"code": "ForbiddenByFirewall"}',
        "unreachable",
    ),
    (
        1,
        'ERROR: (Forbidden) Public network access is disabled and request is not from a trusted service nor via an approved private link.\nCode: Forbidden\nInnerError: {"code": "ForbiddenByConnection"}',
        "unreachable",
    ),
    # not_found
    (
        1,
        "ERROR: The Vault 'nope' not found within subscription.",
        "not_found",
    ),  # capture (source), 2.92.0
    (
        1,
        "ERROR: Subscription 'x' not found. Check the spelling and casing and try again.",
        "not_found",
    ),  # source, 2.92.0
    (
        1,
        "ERROR: (ResourceNotFound) The Resource 'Microsoft.KeyVault/vaults/x' under resource group 'rg' was not found.\nCode: ResourceNotFound",
        "not_found",
    ),
    (1, "ERROR: (ResourceGroupNotFound) Resource group 'rg' could not be found.", "not_found"),
    (1, 'ERROR: Not Found({"error":{"code":"ResourceNotFound"}})', "not_found"),
    # rate_limited
    (
        1,
        "ERROR: (TooManyRequests) The request is being throttled as the limit has been reached for operation type - 'Read'.",
        "rate_limited",
    ),
    (1, "ERROR: (429) Too many requests", "rate_limited"),
    (1, 'ERROR: Too Many Requests({"error":{"code":"ReadRequestsLimitExceeded"}})', "rate_limited"),
    # unreachable
    (
        1,
        "ERROR: The command failed with an unexpected error. Here is the traceback:\nERROR: HTTPConnectionPool(host='169.254.169.254', port=80): Max retries exceeded with url: /metadata/identity/oauth2/token (Caused by NewConnectionError(\"HTTPConnection(host='169.254.169.254', port=80): Failed to establish a new connection: [Errno 111] Connection refused\"))\nTraceback (most recent call last):\n  File \"x.py\", line 1\nrequests.exceptions.ConnectionError: HTTPConnectionPool",
        "unreachable",
    ),  # capture shape, 2.92.0
    (
        1,
        "ERROR: HTTPSConnectionPool(host='management.azure.com', port=443): Max retries exceeded with url: /x (Caused by NameResolutionError: [Errno -2] Name or service not known)",
        "unreachable",
    ),
    (
        1,
        "ERROR: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain",
        "unreachable",
    ),
    (
        1,
        "ERROR: HTTPSConnectionPool(host='m', port=443): Max retries exceeded (Caused by NewConnectionError: Failed to establish a new connection: [Errno 110] Connection timed out)",
        "unreachable",
    ),
    # timeout
    (
        1,
        "ERROR: HTTPSConnectionPool(host='m', port=443): Read timed out. (read timeout=300)",
        "timeout",
    ),
    (124, "", "timeout"),
    # dependency
    (127, "az: not found", "dependency"),
    (
        1,
        "ERROR: (MissingSubscriptionRegistration) The subscription is not registered to use namespace 'Microsoft.ResourceHealth'.",
        "dependency",
    ),
    (
        1,
        'ERROR: Bad Request({"error":{"code":"MissingSubscriptionRegistration","message":"The subscription is not registered to use namespace \'Microsoft.ResourceHealth\'. See https://aka.ms/rps-not-found"}})',
        "dependency",
    ),
    (
        2,
        "ERROR: 'bogus' is misspelled or not recognized by the system.\nIf the command is from an extension, please make sure the corresponding extension is installed.",
        "dependency",
    ),  # capture, 2.92.0
    # invalid_input
    (
        1,
        "ERROR: (InvalidSubscriptionId) The provided subscription identifier 'x' is malformed or invalid.",
        "invalid_input",
    ),
    # internal: a usage error is a bug in the skill, not a state of Azure
    (
        2,
        "ERROR: unrecognized arguments: --foo\n\nhttps://aka.ms/cli_ref",
        "internal",
    ),  # capture, 2.92.0
    (1, "ERROR: something nobody has seen before", "internal"),
    (3, "", "internal"),
]


@pytest.mark.parametrize("rc,err,code", CASES, ids=[f"{c[2]}-{i}" for i, c in enumerate(CASES)])
def test_a_failed_command_is_classified_from_its_error_lines(rc, err, code):
    exc = failure(rc, err)
    assert exc.code == code
    assert exc.message and len(exc.message) <= 1024


def test_the_traceback_after_the_error_lines_is_not_read():
    # the traceback names exception classes (ResourceNotFoundError, ...) that would match anything
    err = (
        "ERROR: (AuthorizationFailed) The client 'x' does not have authorization to perform action 'a' over scope 's'.\n"
        "Traceback (most recent call last):\n  azure.core.exceptions.ResourceNotFoundError: Operation returned an invalid status 'Not Found'\n"
    )
    assert failure(1, err).code == "permission_denied"


def test_the_message_is_the_error_lines_not_the_traceback():
    err = "ERROR: first\nERROR: second\nTraceback (most recent call last):\n  File x\n"
    message = failure(1, err).message
    assert message == "ERROR: first ERROR: second"


def test_a_long_message_is_cut_to_1024_characters():
    assert len(failure(1, "ERROR: " + "x" * 5000).message) == 1024


def test_exit_code_zero_is_a_success_whatever_stderr_holds():
    # warnings on a command that worked: an upgrade notice, a deprecation, even the words of an error
    warnings = (
        "WARNING: A new Azure CLI version is available. Run `az upgrade` to update.\n"
        "WARNING: Command group 'x' is in preview. Forbidden TooManyRequests AuthorizationFailed not found\n"
        "ERROR: (this is only a log line)\n"
    )
    assert run_az(0, '{"id": "x"}', warnings) == {"id": "x"}


def test_a_non_zero_exit_with_valid_json_on_stdout_is_still_a_failure():
    with pytest.raises(RwError) as caught:
        run_az(1, '{"id": "x"}', "ERROR: (Forbidden) nope")
    assert caught.value.code == "permission_denied"


def test_no_output_is_none_and_bad_json_is_internal():
    assert run_az(0, "", "") is None
    assert run_az(0, "  \n", "") is None
    with pytest.raises(RwError) as caught:
        run_az(0, "not json", "")
    assert caught.value.code == "internal" and "JSON" in caught.value.message


def test_the_command_is_run_with_json_output_the_setting_timeout_and_check_false():
    seen = {}

    def run(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        seen.update(argv=argv, timeout=timeout, check=check)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    az_mod.az_json(Ctx(run, "2m"), ["keyvault", "list", "--subscription", SUB])
    assert seen == {
        "argv": ["az", "keyvault", "list", "--subscription", SUB, "--output", "json"],
        "timeout": 120.0,
        "check": False,
    }


@pytest.mark.parametrize("bad", ["", "0s", "soon", "-5s", "10"])
def test_a_bad_command_timeout_is_invalid_input_before_any_call(bad):
    def run(*a, **k):
        raise AssertionError("no call may be made")

    with pytest.raises(RwError) as caught:
        az_mod.az_json(Ctx(run, bad), ["account", "show"])
    assert caught.value.code == "invalid_input" and "commandTimeout" in caught.value.message


def test_a_command_that_times_out_is_a_timeout():
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(RwError) as caught:
        az_mod.az_json(Ctx(run), ["monitor", "metrics", "list"])
    assert caught.value.code == "timeout" and "60s" in caught.value.message


def test_a_missing_az_is_a_dependency_error():
    def run(argv, **kwargs):
        raise FileNotFoundError("az")

    with pytest.raises(RwError) as caught:
        az_mod.az_json(Ctx(run), ["account", "show"])
    assert caught.value.code == "dependency"


def test_durations_accept_combinations():
    assert az_mod.parse_duration("1m30s") == 90.0
    assert az_mod.parse_duration("2h") == 7200.0
    assert az_mod.parse_duration("500ms") == 0.5
